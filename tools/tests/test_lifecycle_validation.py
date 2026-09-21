"""Check persisted drain state and the gates that return engines to service."""

import copy
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.engines.validation import validation_pairs
from narwhal.runtime import lifecycle
from narwhal.runtime.lifecycle import DrainRecord, LifecycleError, ValidationOutcome
from narwhal.serving.app import create_app
from narwhal.types import Role
from tools.tests.fixtures import fleet


class LifecycleValidationTests(unittest.IsolatedAsyncioTestCase):
    """Real lifecycle and scheduler state use local identity and engine responses."""

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
            self.cfg.engine_contract, dict.fromkeys(self.cfg.engine_contract.fields(), "fixture")
        )
        self.transport = httpx.MockTransport(self.http)
        self.router.lifecycle_transport = self.transport
        self.manager.records["e0"] = DrainRecord("e0", "validating", 1, 2, old_process_start=100)
        self.router.scheduler.drain("e0")

    async def identity(self, url, **kwargs):
        """Read an engine's current process identity from the test's explicit state."""
        iid = next(spec.iid for spec in self.cfg.engines if spec.url == url)
        return EngineIdentity(self.cfg.engine_contract.vllm_version, self.starts[iid])

    async def decode(self, *args, **kwargs):
        """Return token evidence for the requested fabric pair."""
        yield 'data: {"choices":[{"token_ids":[1,2],"text":"ok"}]}'

    def http(self, request):
        """Serve attestation, model and local generation gates with per-engine overrides."""
        for spec in self.cfg.engines:
            if str(request.url) == spec.attestation_url:
                iid, route = spec.iid, "attestation"
                default = make_attestation(
                    self.document,
                    EngineIdentity(self.cfg.engine_contract.vllm_version, self.starts[iid]),
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
        """Run the complete readmission pipeline under a controlled identity source."""
        with patch.object(lifecycle, "fetch_engine_identity", self.identities):
            return await lifecycle.validate_readmission(
                self.router, engines or ["e0"], wave=wave, transport=self.transport
            )

    def test_restore_rejects_malformed_records_before_changing_holds(self):
        """Invalid record fields preserve the manager's existing hold set."""
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
        """Restored wave membership covers the configured fleet and valid process times."""
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
        """Restoring interrupted validation blocks the engine and loads the latest 200 events."""
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
        """Recovery validation takes ownership only after an engine has been ejected."""
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))
        for engines, wave in (([], False), (["e0", "e3"], False), (["e0"], True)):
            with self.subTest(engines=engines), self.assertRaises(LifecycleError):
                self.manager.start_recovery_validation(engines, wave=wave)
        self.router.scheduler.eject("e0")
        self.assertTrue(self.manager.start_recovery_validation(["e0"]))
        self.assertFalse(self.manager.records["e0"].restart_required)
        self.assertEqual(self.manager.records["e0"].state, "validating")
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))

    def test_wave_recovery_and_validation_failure_preserve_all_holds(self):
        """One engine's validation failure keeps the entire restart wave blocked."""
        self.manager.records.clear()
        for iid in ("e0", "e3"):
            self.router.scheduler.eject(iid)
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
        """Whole-wave policy converts automatic recovery into a fleet-wide restart hold."""
        self.cfg.engine_restart_policy = "whole_wave"
        self.assertFalse(self.manager.start_recovery_validation(["e0"]))
        first = self.manager.wave_id
        self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})
        self.manager.require_restart_wave("repeat")
        self.assertEqual(self.manager.wave_id, first)
        self.manager.require_restart_wave("new failure", reset=True)
        self.assertNotEqual(self.manager.wave_id, first)

    def test_validation_transition_requires_drained_state_and_old_identity(self):
        """Validation starts from a recorded drain and clears stale check results."""
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
        """Resident work keeps the drain at deadline_exceeded until the last request finishes."""
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
        """Validation probes both transfer directions before the engine returns to service."""
        outcome = await self.validate()
        self.assertTrue(outcome.passed, outcome.failures)
        self.assertEqual(outcome.starts, {"e0": 101})
        self.assertEqual(self.router.engines.prefill.await_count, 2)
        self.assertIn("e0", self.router.scheduler.draining)
        self.assertIn("final health", outcome.checks["e0"])
        self.manager.readmitted(["e0"], outcome)
        self.assertEqual(self.manager.records["e0"].state, "active")
        self.assertNotIn("e0", self.router.scheduler.draining)

    async def test_readmission_rejects_gate_failures_before_fabric_dispatch(self):
        """Failed identity, model or generation evidence stops fabric probes."""
        for route, response, expected in (
            ("attestation", httpx.Response(503), "attestation unreadable"),
            ("attestation", {}, "attestation:"),
            ("/v1/models", httpx.Response(503), "model list unreadable"),
            ("/v1/models", {"data": [{"id": "other"}]}, "expected stub"),
            ("/v1/completions", {"choices": []}, "generation failed"),
        ):
            with self.subTest(route=route, expected=expected):
                self.responses = {("e0", route): response}
                outcome = await self.validate()
                self.assertFalse(outcome.passed)
                self.assertIn(expected, " ".join(outcome.failures["e0"]))
                self.router.engines.prefill.assert_not_awaited()

    async def test_readmission_rejects_unchanged_target_and_changed_peer(self):
        """A restart must advance the target identity and preserve its peer's identity."""
        self.starts["e0"] = 100
        outcome = await self.validate()
        self.assertIn("did not restart", " ".join(outcome.failures["e0"]))
        self.starts.update(e0=101, e3=102)
        outcome = await self.validate()
        self.assertIn("peer process changed", " ".join(outcome.failures["e3"]))
        self.assertIn("e3", self.router.scheduler.ejected)
        self.router.engines.prefill.assert_not_awaited()

    async def test_readmission_health_identity_and_missing_attestation_fail_closed(self):
        """Unavailable identity evidence leaves the target held out of scheduling."""
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
        """A producer restart after prefill invalidates its descriptor before decode."""

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
        """Each failed transfer identifies both its producer and consumer."""

        async def empty(*args, **kwargs):
            yield 'data: {"choices": []}'

        self.router.engines.decode = empty
        outcome = await self.validate()
        self.assertEqual(set(outcome.failures), {"e0", "e3"})
        self.assertIn("no tokens", " ".join(outcome.failures["e0"]))
        self.router.engines.prefill.side_effect = httpx.ReadTimeout("fabric")
        outcome = await self.validate()
        self.assertEqual(set(outcome.failures), {"e0", "e3"})
        self.assertIn("ReadTimeout", " ".join(outcome.failures["e0"]))

    async def test_final_health_failure_retains_target_hold(self):
        """The target must remain healthy after successful fabric transfers."""
        self.router.engines.healthy.side_effect = [True, True, False]
        outcome = await self.validate()
        self.assertIn("final health failed after fabric validation", outcome.failures["e0"])
        self.assertIn("e0", self.router.scheduler.draining)

    async def test_readmission_requires_complete_contract_and_eligible_peers(self):
        """Policy and topology rejection precede engine health probes."""
        contract = self.cfg.engine_contract
        self.cfg.engine_contract = None
        outcome = await self.validate()
        self.assertIn("complete engine_contract", " ".join(outcome.failures["e0"]))
        self.cfg.engine_contract = contract
        self.router.scheduler.eject("e3")
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
        """Fabric pairs respect producer and consumer pins and cover every eligible consumer."""
        a, b = self.cfg.engines
        producer = replace(a, pin=True, role=Role.PREFILL)
        consumer = replace(b, pin=True, role=Role.DECODE)
        self.assertEqual(lifecycle._single_pairs(producer, [consumer]), [(a.iid, b.iid)])
        self.assertEqual(lifecycle._single_pairs(consumer, [producer]), [(a.iid, b.iid)])
        for target, peer in ((producer, producer), (consumer, consumer)):
            with self.subTest(role=target.role), self.assertRaises(LifecycleError):
                lifecycle._single_pairs(target, [peer])
        self.assertEqual(validation_pairs([producer, consumer]), [(a.iid, b.iid)])
        self.assertEqual(validation_pairs([producer]), [])
        third = replace(consumer, iid="extra")
        self.assertEqual(
            set(validation_pairs([producer, consumer, third])),
            {(a.iid, b.iid), (a.iid, "extra")},
        )

    async def test_capture_identity_reports_each_unreadable_engine(self):
        """Identity capture reports each engine's process start time or connection error."""
        with patch.object(
            lifecycle,
            "fetch_engine_identity",
            side_effect=[
                EngineIdentity(self.cfg.engine_contract.vllm_version, 100),
                httpx.ReadTimeout("identity"),
            ],
        ):
            starts, failures = await lifecycle.capture_process_identities(self.cfg, ["e0", "e3"])
        self.assertEqual(starts, {"e0": 100})
        self.assertIn("ReadTimeout", failures["e3"])
        self.cfg.engine_contract = None
        starts, failures = await lifecycle.capture_process_identities(self.cfg, ["e0", "e3"])
        self.assertEqual(starts, {})
        self.assertEqual(set(failures), {"e0", "e3"})

    async def test_identity_monitor_ejects_unverified_engines_and_honours_fencing(self):
        """Identity monitoring mutates holds only while this router owns fleet control."""
        self.manager.records.clear()
        self.router.scheduler.finish_drain("e0")
        self.starts["e0"] = 100
        with patch.object(lifecycle, "fetch_engine_identity", self.identities):
            self.assertEqual(await lifecycle.check_process_identities(self.router), [])
            self.starts["e0"] = 101
            self.assertEqual(await lifecycle.check_process_identities(self.router), ["e0"])
            self.assertIn("e0", self.router.scheduler.ejected)
            self.router.standby = True
            self.identities.reset_mock()
            self.assertEqual(await lifecycle.check_process_identities(self.router), [])
            self.identities.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
