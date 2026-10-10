import copy
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.engines.stream import parse_event
from narwhal.engines.validation import validation_pairs
from narwhal.runtime.lifecycle import identity, readmission
from narwhal.runtime.lifecycle.records import DrainRecord, LifecycleError, ValidationOutcome
from narwhal.runtime.monitoring import readmit
from narwhal.serving.app import create_app
from narwhal.types import Role
from tests.fixtures import fleet


class LifecycleValidationTests(unittest.IsolatedAsyncioTestCase):
    launch: ClassVar[dict | None] = None

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.manager = self.router.lifecycle
        self.manager.process_starts = {"e0": 100, "e3": 100}
        self.starts = {"e0": 101, "e3": 100}
        self.responses = {}
        self.requests = []
        self.identities = AsyncMock(side_effect=self.identity)
        self.router.engines.healthy = AsyncMock(return_value=True)
        self.router.engines.prefill = AsyncMock(return_value="descriptor")
        self.router.engines.decode = self.decode
        self.document = AttestationDocument(
            self.cfg.engine_contract,
            dict.fromkeys(self.cfg.engine_contract.fields(), "fixture"),
            launch=self.launch,
        )
        self.bind_profiles()
        self.transport = httpx.MockTransport(self.http)
        self.router.lifecycle_transport = self.transport
        self.manager.records["e0"] = DrainRecord("e0", "validating", 1, 2, old_process_start=100)
        self.router.scheduler.drain("e0")

    def bind_profiles(self):
        for iid, start in self.starts.items():
            payload = make_attestation(
                self.document, EngineIdentity(self.cfg.engine_contract.engine_version, start)
            )
            digest = payload.get("launch_digest") or payload["attestation_digest"]
            self.router.profiles.put(
                replace(self.router.profiles.get(iid), generation_digest=digest)
            )

    async def identity(self, url, **kwargs):
        iid = next(spec.iid for spec in self.cfg.engines if spec.url == url)
        return EngineIdentity(self.cfg.engine_contract.engine_version, self.starts[iid])

    async def decode(self, *args, **kwargs):
        yield [parse_event('data: {"choices":[{"token_ids":[1,2],"text":"ok"}]}')]

    def http(self, request):
        for spec in self.cfg.engines:
            if str(request.url) == spec.attestation_url:
                iid, route = spec.iid, "attestation"
                default = make_attestation(
                    self.document,
                    EngineIdentity(self.cfg.engine_contract.engine_version, self.starts[iid]),
                )
                break
            if str(request.url).startswith(spec.url + "/"):
                iid, route = spec.iid, request.url.path
                default = (
                    {"data": [{"id": self.cfg.model}]}
                    if route == "/v1/models"
                    else {"choices": [{"text": "ok"}]}
                )
                break
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        self.requests.append((iid, route))
        response = self.responses.get((iid, route), default)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    async def validate(self, *, wave=False, engines=None):
        with patch.object(readmission, "fetch_engine_identity", self.identities):
            return await readmission.validate_readmission(
                self.router, engines or ["e0"], wave=wave, transport=self.transport
            )

    def test_restore_rejects_malformed_records_before_changing_holds(self):
        base = self.manager.handoff()
        original = copy.deepcopy(self.manager.records)
        for name, value in (
            ("iid", "unknown"),
            ("state", "unknown"),
            ("surprise", 1),
            ("requested_at", True),
            ("deadline_at", "1"),
            ("deadline_at", float("inf")),
            ("old_process_start", False),
            ("new_process_start", []),
            ("new_process_start", float("nan")),
            ("wave_id", 1),
            ("error", []),
            ("restart_required", 1),
            ("checks", "health"),
            ("checks", [1]),
        ):
            candidate = copy.deepcopy(base)
            candidate["records"][0][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(LifecycleError):
                self.manager.restore(candidate)
            self.assertEqual(self.manager.records, original)
        for records in (None, [None], [{}]):
            with self.subTest(records=records), self.assertRaises(LifecycleError):
                self.manager.restore({**base, "records": records})

    def test_restore_requires_coherent_wave_membership_and_process_identity(self):
        base = self.manager.handoff()
        for starts in (
            [],
            {"unknown": 1},
            {"e0": True},
            {"e0": 0},
            {"e0": "1"},
            {"e0": float("inf")},
        ):
            with (
                self.subTest(starts=starts),
                self.assertRaisesRegex(LifecycleError, "process_starts"),
            ):
                self.manager.restore({**base, "process_starts": starts})
        for wave in (1, "missing-wave"):
            with self.subTest(wave=wave), self.assertRaises(LifecycleError):
                self.manager.restore({**base, "wave_id": wave})
        orphan = copy.deepcopy(base)
        orphan["records"][0]["wave_id"] = "orphan"
        with self.assertRaisesRegex(LifecycleError, "without an active wave"):
            self.manager.restore(orphan)
        with self.assertRaisesRegex(LifecycleError, "must be an object"):
            self.manager.restore([])

    def test_restore_interrupted_validation_holds_engines_and_bounds_events(self):
        base = self.manager.handoff()
        base["events"] = [None, *({"n": n} for n in range(205))]
        base["records"].append(asdict(DrainRecord("e3", "active", 1, 2)))
        self.manager.restore(base)
        self.assertEqual(self.manager.records["e0"].state, "blocked")
        self.assertIn("interrupted", self.manager.records["e0"].error)
        self.assertEqual(self.router.scheduler.draining, {"e0"})
        self.assertEqual(self.manager.events[0], {"n": 5})
        self.assertEqual(len(self.manager.events), 200)
        self.manager.restore({**base, "events": None})
        self.assertEqual(self.manager.events, [])

    def test_recovery_requires_ejection_and_exact_selection(self):
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))
        for engines, wave in (([], False), (["e0", "e3"], False), (["e0"], True)):
            with self.subTest(engines=engines), self.assertRaises(LifecycleError):
                self.manager.start_recovery_validation(engines, wave=wave)
        self.router.scheduler.eject("e0", "liveness")
        self.assertTrue(self.manager.start_recovery_validation(["e0"]))
        self.assertFalse(self.manager.records["e0"].restart_required)
        self.assertEqual(self.manager.records["e0"].state, "validating")
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))

    def test_failed_automatic_recovery_holds_only_its_own_engine(self):
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.router.scheduler.eject("e0", "liveness")
        self.assertTrue(self.manager.start_recovery_validation(["e0"]))
        self.manager.validation_failed(ValidationOutcome(failures={"e0": ["sidecar down"]}))
        self.assertEqual(self.manager.records["e0"].state, "blocked")
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))
        self.router.scheduler.eject("e3", "liveness")
        self.assertTrue(self.manager.start_recovery_validation(["e3"]))
        self.assertEqual(self.manager.records["e3"].state, "validating")
        for held in ("draining", "wave"):
            with self.subTest(held=held):
                self.manager.records["e3"].state = "active"
                self.router.scheduler.finish_drain("e3")
                self.router.scheduler.eject("e3", "liveness")
                record = self.manager.records["e0"]
                record.restart_required = held == "draining"
                record.wave_id = "wave-held" if held == "wave" else ""
                self.assertFalse(self.manager.start_recovery_validation(["e3"]))

    def test_wave_recovery_and_validation_failure_preserve_all_holds(self):
        self.manager.records.clear()
        for iid in ("e0", "e3"):
            self.router.scheduler.eject(iid, "liveness")
        self.assertTrue(self.manager.start_recovery_validation(["e0", "e3"], wave=True))
        self.assertTrue(self.router.lifecycle_blocked)
        outcome = ValidationOutcome(checks={"e0": ["health"]}, failures={"e0": ["bad identity"]})
        self.manager.validation_failed(outcome)
        self.assertEqual({r.state for r in self.manager.records.values()}, {"blocked"})
        self.assertEqual(self.manager.records["e0"].checks, ["health"])
        self.assertEqual(self.manager.records["e0"].error, "bad identity")
        self.assertIn("another engine", self.manager.records["e3"].error)
        self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})

    def test_managed_wave_recovery_requires_a_new_operator_restart(self):
        self.cfg.engine_restart_policy = "whole_wave"
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))
        first = self.manager.wave_id
        self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})
        self.manager.require_restart_wave("repeat")
        self.assertEqual(self.manager.wave_id, first)
        self.manager.require_restart_wave("new failure", reset=True)
        self.assertNotEqual(self.manager.wave_id, first)

    def test_validation_transition_requires_drained_state_and_old_identity(self):
        for iid in ("e3", "e0"):
            with self.subTest(iid=iid), self.assertRaisesRegex(LifecycleError, "expected drained"):
                self.manager.mark_validating([iid])
        record = self.manager.records["e0"]
        record.state, record.old_process_start = "blocked", None
        with self.assertRaisesRegex(LifecycleError, "pre-restart"):
            self.manager.mark_validating(["e0"])
        record.old_process_start, record.error, record.checks = 100, "old failure", ["old check"]
        self.manager.mark_validating(["e0"])
        self.assertEqual((record.state, record.error, record.checks), ("validating", "", []))

    def test_resident_deadline_retains_hold_until_the_last_request_drains(self):
        record = self.manager.records["e0"]
        record.state = "draining"
        instance = self.router.monitor.instances["e0"]
        instance.prefill["resident"] = object()
        self.manager.refresh()
        self.assertEqual(record.state, "deadline_exceeded")
        count = len(self.manager.events)
        self.manager.refresh()
        self.assertEqual(len(self.manager.events), count)
        instance.prefill.clear()
        self.manager.refresh()
        self.assertEqual(record.state, "drained")
        self.assertIn("e0", self.router.scheduler.draining)

    async def test_successful_readmission_checks_both_fabric_directions(self):
        outcome = await self.validate()
        self.assertTrue(outcome.passed, outcome.failures)
        self.assertEqual(outcome.starts, {"e0": 101})
        self.assertEqual(self.router.engines.prefill.await_count, 2)
        self.assertIn("e0", self.router.scheduler.draining)
        self.assertIn("final health", outcome.checks["e0"])
        self.assertIn("profile generation", outcome.checks["e0"])
        self.manager.readmitted(["e0"], outcome)
        self.assertEqual(self.manager.records["e0"].state, "active")
        self.assertNotIn("e0", self.router.scheduler.draining)

    async def test_stale_missing_and_unbound_loaded_profiles_reject_readmission(self):
        original = self.router.profiles.get("e0")
        for value, detail in (
            (None, "has no loaded profile"),
            (replace(original, generation_digest=None), "has no generation evidence"),
            (replace(original, generation_digest="sha256:" + "0" * 64), "differs from the live"),
        ):
            with self.subTest(detail=detail):
                if value is None:
                    self.router.profiles._by_id.pop("e0")
                else:
                    self.router.profiles.put(value)
                outcome = await self.validate()
                self.assertIn("e0", outcome.failures)
                self.assertIn(detail, " ".join(outcome.failures["e0"]))
                self.assertIn("e0", self.router.scheduler.draining)
                self.router.engines.prefill.assert_not_awaited()
        self.router.profiles.put(original)
        self.assertTrue((await self.validate()).passed)

    async def test_inactive_profile_variant_must_match_the_verified_generation(self):
        original = self.router.profiles.get("e0")
        variant = replace(
            original,
            generation_digest="sha256:" + "0" * 64,
            colocated_group="gpu-0",
            colocated_prefill_engines=1,
            colocated_decode_engines=1,
            colocated_target_role="prefill",
            colocated_prefill_rps=1.0,
            colocated_decode_rps=1.0,
        )
        self.router.profiles.put(variant)
        outcome = await self.validate()
        self.assertIn("profile generation differs", " ".join(outcome.failures["e0"]))
        self.router.engines.prefill.assert_not_awaited()
        self.router.profiles.put(replace(variant, generation_digest=original.generation_digest))
        self.assertTrue((await self.validate()).passed)

    async def test_one_stale_wave_member_prevents_every_member_from_returning(self):
        self.cfg.engine_restart_policy = "whole_wave"
        self.starts["e3"] = 102
        self.bind_profiles()
        self.manager.wave_id = "profile-wave"
        self.router.lifecycle_blocked = "whole-wave drain profile-wave"
        for iid in self.starts:
            self.router.scheduler.drain(iid)
            self.manager.records[iid] = DrainRecord(
                iid, "validating", 1, 2, wave_id="profile-wave", old_process_start=100
            )
        self.router.profiles.put(replace(self.router.profiles.get("e3"), generation_digest=None))
        outcome = await self.validate(wave=True, engines=["e0", "e3"])
        self.assertEqual(set(outcome.failures), {"e3"})
        self.manager.validation_failed(outcome)
        self.assertEqual({row.state for row in self.manager.records.values()}, {"blocked"})
        self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})
        self.assertFalse(self.manager.view()["router"]["ready"])
        self.assertIn("another engine", self.manager.records["e0"].error)
        self.bind_profiles()
        self.manager.mark_validating(["e0", "e3"])
        outcome = await self.validate(wave=True, engines=["e0", "e3"])
        self.assertTrue(outcome.passed, outcome.failures)
        self.manager.readmitted(["e0", "e3"], outcome)
        self.assertEqual(self.router.scheduler.draining, set())
        self.assertFalse(self.manager.wave_id)

    async def test_final_attestation_change_invalidates_profile_binding(self):
        original_health = self.router.engines.healthy

        async def healthy(url):
            if original_health.await_count == 3:
                self.document = AttestationDocument(
                    self.cfg.engine_contract,
                    dict.fromkeys(self.cfg.engine_contract.fields(), "changed source evidence"),
                    launch=None if self.launch is None else {"args": ["--changed"]},
                )
            return True

        original_health.side_effect = healthy
        outcome = await self.validate()
        self.assertFalse(outcome.passed)
        self.assertIn("profile generation differs", " ".join(outcome.failures["e0"]))
        self.assertEqual(self.router.engines.prefill.await_count, 2)
        self.assertIn("e0", self.router.scheduler.draining)

    async def test_unchanged_generation_automatic_validation_retains_measured_profile(self):
        self.manager.records["e0"].restart_required = False
        self.starts["e0"] = 100
        self.bind_profiles()
        outcome = await self.validate()
        self.assertTrue(outcome.passed, outcome.failures)
        self.assertIn("profile generation", outcome.checks["e0"])

    async def test_contracted_automatic_readmission_blocks_stale_loaded_measurements(self):
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.router.scheduler.eject("e0", "liveness")
        self.router.profiles.put(
            replace(self.router.profiles.get("e0"), generation_digest="sha256:" + "0" * 64)
        )
        with patch.object(readmission, "fetch_engine_identity", self.identities):
            self.assertEqual(await readmit(self.router, 0), [])
        self.assertIn("e0", self.router.scheduler.ejected)
        self.assertIn("e0", self.router.scheduler.draining)
        self.assertEqual(self.manager.records["e0"].state, "blocked")
        self.assertIn("profile generation differs", self.manager.records["e0"].error)

    async def test_contracted_automatic_readmission_accepts_unchanged_measured_generation(self):
        self.starts["e0"] = 100
        self.bind_profiles()
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.router.scheduler.eject("e0", "liveness")
        with patch.object(readmission, "fetch_engine_identity", self.identities):
            self.assertEqual(await readmit(self.router, 0), ["e0"])
        self.assertEqual(self.manager.records["e0"].state, "active")
        self.assertIn("profile generation", self.manager.records["e0"].checks)

    async def test_identity_sweep_rejects_stale_profiles_even_when_accepted_identity_matches(self):
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.manager.process_starts["e0"] = 101
        self.router.profiles.put(replace(self.router.profiles.get("e0"), generation_digest=None))
        with patch.object(identity, "fetch_engine_identity", self.identities):
            self.assertEqual(await identity.check_process_identities(self.router), ["e0"])
        self.assertIn("e0", self.router.scheduler.ejected)
        self.assertIn("has no generation evidence", self.manager.events[-1]["reason"])

    async def test_readmission_rejects_gate_failures_before_fabric_dispatch(self):
        for route, response, expected in (
            ("attestation", httpx.Response(503), "attestation unreadable"),
            ("attestation", {}, "attestation:"),
            ("/v1/models", httpx.Response(503), "model list unreadable"),
            ("/v1/models", {"data": [{"id": "other"}]}, "expected test-model"),
            ("/v1/completions", {"choices": []}, "generation failed"),
        ):
            with self.subTest(route=route, expected=expected):
                self.responses = {("e0", route): response}
                outcome = await self.validate()
                self.assertFalse(outcome.passed)
                self.assertIn(expected, " ".join(outcome.failures["e0"]))
                self.router.engines.prefill.assert_not_awaited()

    async def test_readmission_rejects_unchanged_target_and_changed_peer(self):
        self.starts["e0"] = 100
        outcome = await self.validate()
        self.assertIn("did not restart", " ".join(outcome.failures["e0"]))
        self.starts.update(e0=101, e3=102)
        outcome = await self.validate()
        self.assertIn("peer process changed", " ".join(outcome.failures["e3"]))
        self.assertIn("e3", self.router.scheduler.ejected)
        self.router.engines.prefill.assert_not_awaited()

    async def test_readmission_health_identity_and_missing_attestation_fail_closed(self):
        self.router.engines.healthy.return_value = False
        outcome = await self.validate()
        self.assertIn("health did not answer 200", outcome.failures["e0"])
        self.router.engines.healthy.return_value = True
        self.identities.side_effect = httpx.ReadTimeout("identity")
        outcome = await self.validate()
        self.assertIn("process identity unreadable", " ".join(outcome.failures["e0"]))
        self.identities.side_effect = self.identity
        self.cfg.engines[0].attestation_url = ""
        outcome = await self.validate()
        self.assertIn("attestation_url is not configured", outcome.failures["e0"])
        self.router.engines.prefill.assert_not_awaited()

    async def test_mid_validation_identity_change_ejects_before_decode(self):

        async def restart(*args, **kwargs):
            self.starts["e0"] += 1
            return "descriptor"

        self.router.engines.prefill.side_effect = restart
        with patch.object(self.router.engines, "decode") as decode:
            outcome = await self.validate()
        self.assertFalse(outcome.passed)
        self.assertIn("during validation", " ".join(outcome.failures["e0"]))
        self.assertIn("e0", self.router.scheduler.ejected)
        decode.assert_not_called()

    async def test_fabric_empty_stream_and_transport_failure_block_both_ends(self):

        async def empty(*args, **kwargs):
            yield [parse_event('data: {"choices": []}')]

        self.router.engines.decode = empty
        outcome = await self.validate()
        self.assertEqual(set(outcome.failures), {"e0", "e3"})
        self.assertIn("no tokens", " ".join(outcome.failures["e0"]))
        self.router.engines.prefill.side_effect = httpx.ReadTimeout("fabric")
        outcome = await self.validate()
        self.assertEqual(set(outcome.failures), {"e0", "e3"})
        self.assertIn("ReadTimeout", " ".join(outcome.failures["e0"]))

    async def test_final_health_failure_retains_target_hold(self):
        self.router.engines.healthy.side_effect = [True, True, False]
        outcome = await self.validate()
        self.assertIn("final health failed after fabric validation", outcome.failures["e0"])
        self.assertIn("e0", self.router.scheduler.draining)

    async def test_readmission_requires_complete_contract_and_eligible_peers(self):
        contract = self.cfg.engine_contract
        self.cfg.engine_contract = None
        outcome = await self.validate()
        self.assertIn("complete engine_contract", " ".join(outcome.failures["e0"]))
        self.cfg.engine_contract = contract
        self.router.scheduler.eject("e3", "liveness")
        outcome = await self.validate()
        self.assertIn("eligible fabric", " ".join(outcome.failures["e0"]))
        self.cfg.engine_restart_policy = "whole_wave"
        outcome = await self.validate()
        self.assertIn("whole-wave readmission", " ".join(outcome.failures["e0"]))
        self.manager.records["e3"] = DrainRecord("e3", "validating", 1, 2)
        outcome = await self.validate(wave=True, engines=["e0", "e3"])
        self.assertIn("pre-restart identities", " ".join(outcome.failures["e0"]))
        self.router.engines.healthy.assert_not_awaited()

    def test_pinned_topologies_require_compatible_transfer_peers(self):
        a, b = self.cfg.engines
        producer = replace(a, pin=True, role=Role.PREFILL)
        consumer = replace(b, pin=True, role=Role.DECODE)
        self.assertEqual(readmission._single_pairs(producer, [consumer]), [(a.iid, b.iid)])
        self.assertEqual(readmission._single_pairs(consumer, [producer]), [(a.iid, b.iid)])
        for target, peer in ((producer, producer), (consumer, consumer)):
            with self.subTest(role=target.role), self.assertRaises(LifecycleError):
                readmission._single_pairs(target, [peer])
        self.assertEqual(validation_pairs([producer, consumer]), [(a.iid, b.iid)])
        self.assertEqual(validation_pairs([producer]), [])
        third = replace(consumer, iid="extra")
        self.assertEqual(
            set(validation_pairs([producer, consumer, third])),
            {(a.iid, b.iid), (a.iid, "extra")},
        )

    async def test_capture_identity_reports_each_unreadable_engine(self):
        with patch.object(
            identity,
            "fetch_engine_identity",
            side_effect=[
                EngineIdentity(self.cfg.engine_contract.engine_version, 100),
                httpx.ReadTimeout("identity"),
            ],
        ):
            starts, failures = await identity.capture_process_identities(self.cfg, ["e0", "e3"])
        self.assertEqual(starts, {"e0": 100})
        self.assertIn("ReadTimeout", failures["e3"])
        self.cfg.engine_contract = None
        starts, failures = await identity.capture_process_identities(self.cfg, ["e0", "e3"])
        self.assertEqual(starts, {})
        self.assertEqual(set(failures), {"e0", "e3"})

    async def test_identity_monitor_ejects_unverified_engines_and_honours_fencing(self):
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.starts["e0"] = 100
        self.bind_profiles()
        with patch.object(identity, "fetch_engine_identity", self.identities):
            self.assertEqual(await identity.check_process_identities(self.router), [])
            self.starts["e0"] = 101
            self.assertEqual(await identity.check_process_identities(self.router), ["e0"])
            self.assertIn("e0", self.router.scheduler.ejected)
            self.router.standby = True
            self.identities.reset_mock()
            self.assertEqual(await identity.check_process_identities(self.router), [])
            self.identities.assert_not_awaited()


class LaunchBoundLifecycleValidationTests(LifecycleValidationTests):
    launch: ClassVar[dict | None] = {"args": ["--max-num-seqs", "64"]}
