"""Join an engine to a running fleet through the readmission checks."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from narwhal.config import EngineSpec
from narwhal.config.model import SharedDeviceAllocation
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.engines.stream import parse_event
from narwhal.profiling import generation
from narwhal.runtime.lifecycle import readmission
from narwhal.runtime.lifecycle.join import join_engine
from narwhal.runtime.lifecycle.records import DrainRecord
from narwhal.runtime.role_switch import RoleSwitcher
from narwhal.serving.app import create_app
from narwhal.types import Role, RoleModel
from tests.fixtures import calibration_document, fleet, profile

NEW = EngineSpec(
    "e6",
    "http://engine-6.invalid:8000",
    role=Role.DECODE,
    attestation_url="http://engine-6.invalid:8000/v1/attestation",
)


class JoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.manager = self.router.lifecycle
        self.starts = {"e0": 100.0, "e3": 100.0, "e6": 200.0}
        self.manager.process_starts = {"e0": 100.0, "e3": 100.0}
        self.responses = {}
        self.requests = []
        self.router.engines.healthy = AsyncMock(return_value=True)
        self.router.engines.prefill = AsyncMock(return_value="descriptor")
        self.router.engines.decode = self.decode
        self.document = AttestationDocument(
            self.cfg.engine_contract, dict.fromkeys(self.cfg.engine_contract.fields(), "fixture")
        )
        for iid in self.starts:
            self.bind_profile(iid)
        self.transport = httpx.MockTransport(self.http)
        self.router.lifecycle_transport = self.transport
        identities = AsyncMock(side_effect=self.identity)
        for module in (readmission, generation):
            patcher = patch.object(module, "fetch_spec_identity", identities)
            patcher.start()
            self.addCleanup(patcher.stop)

    def attestation(self, iid, document=None):
        identity = EngineIdentity(self.cfg.engine_contract.engine_version, self.starts[iid])
        return make_attestation(document or self.document, identity)

    def bind_profile(self, iid):
        payload = self.attestation(iid)
        digest = payload.get("launch_digest") or payload["attestation_digest"]
        self.router.profiles.put(profile(iid, generation_digest=digest))

    async def identity(self, cfg, spec, **kwargs):
        return EngineIdentity(self.cfg.engine_contract.engine_version, self.starts[spec.iid])

    async def decode(self, *args, **kwargs):
        yield [parse_event('data: {"choices":[{"token_ids":[1,2],"text":"ok"}]}')]

    def http(self, request):
        for spec in (*self.cfg.engines, NEW):
            if str(request.url) == spec.attestation_url:
                iid, route, default = spec.iid, "attestation", self.attestation(spec.iid)
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
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    async def join(self, spec=NEW):
        async with self.manager.lock:
            return await join_engine(self.router, spec, transport=self.transport)

    def assert_refused(self, outcome, detail):
        self.assertFalse(outcome.joined)
        self.assertIn(detail, outcome.error)
        self.assertEqual(self.router.membership.generation, 0)
        self.assertNotIn("e6", self.router.monitor.instances)
        self.assertNotIn("e6", self.router.launches)
        refused = [event for event in self.manager.events if event["action"] == "join_refused"]
        self.assertEqual(refused[-1]["iid"], outcome.iid)
        self.assertEqual(refused[-1]["generation"], 0)

    async def test_a_validated_engine_joins_in_its_spec_role(self):
        outcome = await self.join()
        self.assertTrue(outcome.joined, outcome.error)
        self.assertEqual(outcome.generation, 1)
        self.assertEqual(self.router.membership.generation, 1)
        self.assertIn("e6", self.router.membership)
        self.assertIs(self.router.monitor.instances["e6"].role, Role.DECODE)
        self.assertIn("e6", {inst.iid for inst in self.router.scheduler.live_instances()})
        self.assertEqual(self.manager.process_starts["e6"], 200.0)
        self.assertIsNotNone(self.router.profiles.get("e6"))
        self.assertIn("e6", self.router.launches)
        for check in ("attestation", "profile generation", "model", "final health"):
            self.assertTrue(any(check in item for item in outcome.checks), check)
        self.assertTrue(any(item.startswith("fabric") for item in outcome.checks))
        actions = [(e["action"], e.get("generation")) for e in self.manager.events]
        self.assertEqual(actions[-2:], [("join_started", 0), ("joined", 1)])
        self.assertGreater(self.router.engines.prefill.await_count, 0)

    async def test_an_engine_contract_mismatch_is_refused(self):
        fields = {**self.cfg.engine_contract.fields(), "engine_version": "other"}
        contract = replace(self.cfg.engine_contract, engine_version="other")
        other = AttestationDocument(contract, dict.fromkeys(fields, "fixture"))
        self.responses[("e6", "attestation")] = self.attestation("e6", other)
        outcome = await self.join()
        self.assert_refused(outcome, "attestation:")
        self.router.engines.prefill.assert_not_awaited()

    async def test_a_failed_attestation_is_refused(self):
        self.responses[("e6", "attestation")] = httpx.Response(503)
        outcome = await self.join()
        # The profile generation check reads the attestation first.
        self.assert_refused(outcome, "e6 profile generation unreadable")
        self.assertIn("503", outcome.error)
        self.router.engines.prefill.assert_not_awaited()

    async def test_an_attestation_that_fails_during_validation_is_refused(self):
        self.responses[("e6", "attestation")] = [self.attestation("e6"), httpx.Response(503)]
        outcome = await self.join()
        self.assert_refused(outcome, "attestation unreadable")
        self.router.engines.prefill.assert_not_awaited()

    async def test_a_failed_kv_pair_is_refused(self):
        self.router.engines.prefill.side_effect = httpx.ReadTimeout("fabric")
        outcome = await self.join()
        self.assert_refused(outcome, "fabric")
        self.assertIn("ReadTimeout", outcome.error)
        # The peer that took part stays in service.
        self.assertNotIn("e0", self.router.scheduler.ejected)
        self.assertNotIn("e3", self.router.scheduler.ejected)

    async def test_a_missing_or_stale_profile_is_refused_before_any_engine_check(self):
        self.router.profiles._by_id.pop("e6")
        store = self.router.profiles
        store.put(profile("e0", generation_digest=store.get("e0").generation_digest))
        outcome = await self.join()
        self.assert_refused(outcome, "no profile for e6")
        self.router.engines.healthy.assert_not_awaited()
        self.bind_profile("e6")
        self.router.profiles.put(profile("e6", generation_digest="sha256:" + "0" * 64))
        outcome = await self.join()
        self.assert_refused(outcome, "e6 profile generation differs from the live engine")

    async def test_calibration_must_cover_the_new_engine(self):
        path = self.cfg.profiles_path.parent / "calibration.json"
        self.cfg.first_token_calibration_path = path
        generations = {}
        for spec in self.cfg.engines:
            evidence = await generation.read_generation(self.cfg, spec, transport=self.transport)
            generations[spec.iid] = evidence.digest
        starts = {iid: self.starts[iid] for iid in generations}
        path.write_text(json.dumps(calibration_document(self.cfg, generations, starts)))
        outcome = await self.join()
        self.assert_refused(outcome, "first-token calibration has no samples for e6")
        self.router.engines.healthy.assert_not_awaited()

    async def test_fixed_roles_require_pin(self):
        switcher = Mock(spec=RoleSwitcher)
        switcher.model = RoleModel.FIXED
        self.router.role_switch = switcher
        self.assert_refused(await self.join(), "fixed roles must set pin")

    async def test_preconditions_refuse_without_contacting_an_engine(self):
        shared = replace(NEW, shared_device=SharedDeviceAllocation("g", "GPU-x", 0.5, 0.4))
        for spec, detail in (
            (replace(NEW, iid="e3"), "already a member"),
            (replace(NEW, url=self.cfg.engines[0].url + "/"), "already serves"),
            (shared, "shared-device group"),
            (replace(NEW, attestation_url=""), "attestation_url"),
        ):
            with self.subTest(detail=detail):
                outcome = await self.join(spec)
                self.assertFalse(outcome.joined)
                self.assertIn(detail, outcome.refused)
        self.manager.records["e0"] = DrainRecord("e0", "blocked", 1, 2)
        self.assertIn("lifecycle action active for e0", (await self.join()).refused)
        del self.manager.records["e0"]
        self.cfg.engine_restart_policy = "whole_wave"
        self.assertIn("whole_wave", (await self.join()).refused)
        self.cfg.engine_restart_policy = "individual"
        self.cfg.engine_contract = None
        self.assertIn("complete engine_contract", (await self.join()).refused)
        self.router.engines.healthy.assert_not_awaited()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.router.membership.generation, 0)


if __name__ == "__main__":
    unittest.main()
