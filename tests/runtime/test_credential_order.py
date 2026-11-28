"""The router sends the engine credential only to engines whose attestation verified."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.config import EngineSpec
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.engines.stream import parse_event
from narwhal.engines.trust import EngineTrust
from narwhal.observability.journal import RunJournal
from narwhal.profiling.generation import AttestationMismatch, read_generation
from narwhal.runtime.lifecycle.identity import check_process_identities
from narwhal.runtime.lifecycle.join import join_engine
from narwhal.runtime.lifecycle.readmission import validate_readmission
from narwhal.runtime.monitoring import sweep_liveness
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Role
from tests.fixtures import fleet, profile

KEY_VARIABLE = "NARWHAL_TEST_ENGINE_KEY"
KEY = "stub-engine-credential"
CANDIDATE = EngineSpec(
    "e6",
    "http://candidate.invalid:8000",
    role=Role.DECODE,
    attestation_url="http://candidate.invalid:8001/v1/attestation",
)


class StubEngine:
    """One engine and its sidecar; records every request with its generation and credential."""

    def __init__(self, spec, contract, start, *, honest=True, guarded=False):
        self.spec = spec
        self.contract = contract
        self.start = start
        # An honest engine attests the fleet's contract; another attests a different engine.
        self.honest = honest
        # A guarded engine answers its version route only with the credential, as SGLang does.
        self.guarded = guarded
        self.down = False
        # (path, keyed, generation) for every request, in order.
        self.log = []
        # Whether the attestation each generation served verifies.
        self.honesty = {}

    def owns(self, url):
        return any(
            str(url).startswith(base.rstrip("/").rsplit("/v1/", 1)[0] + "/")
            for base in (self.spec.url, self.spec.attestation_url)
        )

    def attestation(self):
        declared = self.contract if self.honest else replace(self.contract, engine_version="9.9")
        document = AttestationDocument(declared, dict.fromkeys(declared.fields(), "stub"))
        return make_attestation(document, EngineIdentity(declared.engine_version, self.start))

    def answer(self, request):
        keyed = request.headers.get("authorization") == f"Bearer {KEY}"
        path = request.url.path
        self.log.append((path, keyed, self.start))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        version = self.attestation()["engine"]["version"]
        if path == "/v1/attestation":
            self.honesty[self.start] = self.honest
            return httpx.Response(200, json=self.attestation())
        if path == "/v1/process":
            return httpx.Response(200, json={"process_start_time_seconds": self.start})
        if path in ("/version", "/server_info"):
            if self.guarded and not keyed:
                return httpx.Response(401, json={"error": "Unauthorized"})
            return httpx.Response(200, json={"version": version})
        if path == "/metrics":
            return httpx.Response(200, text=f"process_start_time_seconds {self.start}\n")
        if path == "/health":
            return httpx.Response(200)
        if not keyed:
            return httpx.Response(401, json={"error": "Unauthorized"})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return httpx.Response(200, json={"choices": [{"text": "ok"}]})

    def keyed(self):
        return [entry for entry in self.log if entry[1]]

    def attested_generations_before(self, index):
        return {start for path, _, start in self.log[:index] if path == "/v1/attestation"}


class Fleet:
    """Stub engines behind one transport."""

    def __init__(self, engines):
        self.engines = {engine.spec.iid: engine for engine in engines}
        self.transport = httpx.MockTransport(self.route)

    def route(self, request):
        for engine in self.engines.values():
            if engine.owns(request.url):
                return engine.answer(request)
        raise AssertionError(f"unexpected request: {request.url}")


class CredentialOrderHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {KEY_VARIABLE: KEY})
        environment.start()
        self.addCleanup(environment.stop)
        self.cfg = fleet(self.root)
        self.cfg.engine_api_key_env = KEY_VARIABLE
        self.cfg.state_path = self.root / "handoff.json"
        contract = self.cfg.engine_contract
        self.stubs = Fleet(
            [StubEngine(spec, contract, 100.0) for spec in self.cfg.engines]
            + [StubEngine(CANDIDATE, contract, 200.0)]
        )
        self.router = NarwhalRouter(
            self.cfg, RunJournal(path=self.root / "journal.jsonl"), transport=self.stubs.transport
        )
        self.addAsyncCleanup(self.router.engines.aclose)
        self.router.lifecycle.process_starts = {"e0": 100.0, "e3": 100.0}
        # The fabric legs run on the data connections; record whether each carried the key.
        self.legs = []
        self.router.engines.prefill = AsyncMock(side_effect=self.prefill)
        self.router.engines.decode = self.decode
        for iid in ("e0", "e3", "e6"):
            self.bind_profile(iid)

    def stub(self, iid):
        return self.stubs.engines[iid]

    def bind_profile(self, iid):
        payload = self.stub(iid).attestation()
        digest = payload.get("launch_digest") or payload["attestation_digest"]
        self.router.profiles.put(profile(iid, generation_digest=digest))

    def leg(self, url):
        engine = next(e for e in self.stubs.engines.values() if e.owns(url + "/"))
        keyed = "authorization" in self.router.engines._auth({}, url)
        engine.log.append(("leg", keyed, engine.start))

    async def prefill(self, url, *args, **kwargs):
        self.leg(url)
        return "descriptor"

    async def decode(self, url, *args, **kwargs):
        self.leg(url)
        yield [parse_event('data: {"choices":[{"token_ids":[1,2],"text":"ok"}]}')]

    def assert_key_follows_verification(self, iid):
        """Every request with the key came after the attestation of the generation it reached,
        and that attestation verifies."""
        engine = self.stub(iid)
        for index, (path, keyed, start) in enumerate(engine.log):
            if not keyed:
                continue
            self.assertTrue(
                engine.honesty.get(start), f"{iid} {path} got the key with a failed attestation"
            )
            self.assertIn(
                start,
                engine.attested_generations_before(index),
                f"{iid} {path} got the key before generation {start} attested",
            )

    def assert_never_keyed(self, iid):
        self.assertEqual(self.stub(iid).keyed(), [], self.stub(iid).log)

    async def verify_members(self):
        self.assertEqual(await check_process_identities(self.router), [])


class TrustTests(unittest.TestCase):
    def test_an_attested_endpoint_is_trusted_for_one_process(self):
        trust = EngineTrust(attested=True, configured=["http://e0"])
        self.assertFalse(trust.allows("http://e0"))
        trust.grant("http://e0/", 100.0)
        self.assertTrue(trust.allows("http://e0"))
        self.assertTrue(trust.observe("http://e0", 100.0))
        self.assertFalse(trust.observe("http://e0", 101.0))
        self.assertFalse(trust.allows("http://e0"))

    def test_a_contract_free_fleet_trusts_its_configured_members(self):
        trust = EngineTrust(attested=False, configured=["http://e0"])
        self.assertTrue(trust.allows("http://e0/"))
        trust.revoke("http://e0")
        self.assertTrue(trust.allows("http://e0"))
        self.assertFalse(trust.allows("http://elsewhere"))
        trust.forget("http://e0")
        self.assertFalse(trust.allows("http://e0"))


class JoinCredentialTests(CredentialOrderHarness):
    async def join(self, spec=CANDIDATE):
        async with self.router.lifecycle.lock:
            return await join_engine(self.router, spec, transport=self.stubs.transport)

    async def test_a_candidate_whose_attestation_fails_never_receives_the_key(self):
        await self.verify_members()
        self.stub("e6").honest = False
        outcome = await self.join()
        self.assertFalse(outcome.joined)
        self.assertIn("attestation does not match", outcome.error)
        self.assertIn(("/v1/attestation", False, 200.0), self.stub("e6").log)
        self.assert_never_keyed("e6")
        self.assertFalse(self.router.trust.allows(CANDIDATE.url))

    async def test_a_candidate_whose_sidecar_does_not_answer_never_receives_the_key(self):
        await self.verify_members()
        self.stub("e6").down = True
        outcome = await self.join()
        self.assertFalse(outcome.joined)
        self.assert_never_keyed("e6")

    async def test_a_candidate_receives_the_key_only_after_its_attestation_verifies(self):
        await self.verify_members()
        outcome = await self.join()
        self.assertTrue(outcome.joined, outcome.error)
        # The candidate's model list, generation check and fabric leg carry the key.
        self.assertTrue(self.stub("e6").keyed(), self.stub("e6").log)
        for iid in ("e0", "e3", "e6"):
            self.assert_key_follows_verification(iid)
        self.assertEqual(self.router.trust.start(CANDIDATE.url), 200.0)

    async def test_a_joined_engine_that_restarts_gets_no_key_until_it_attests_again(self):
        await self.verify_members()
        self.assertTrue((await self.join()).joined)
        engine = self.stub("e6")
        # The engine stops: the liveness sweep finds nothing listening and drops its trust.
        engine.down = True
        await sweep_liveness(self.router)
        self.assertFalse(self.router.trust.allows(CANDIDATE.url))
        # A new process starts on the same address, attesting another engine.
        engine.down, engine.start, engine.honest = False, 300.0, False
        self.assertNotIn("authorization", self.router.engines._auth({}, CANDIDATE.url))
        self.assertIn("e6", await sweep_liveness(self.router))
        self.assertFalse(self.router.trust.allows(CANDIDATE.url))
        self.assertEqual([entry for entry in engine.keyed() if entry[2] == 300.0], [])
        self.assert_key_follows_verification("e6")

    async def test_a_restart_the_router_never_saw_go_down_loses_the_key_at_the_next_check(self):
        await self.verify_members()
        self.assertTrue((await self.join()).joined)
        engine = self.stub("e6")
        engine.start, engine.honest = 300.0, False
        self.assertIn("e6", await check_process_identities(self.router))
        self.assertFalse(self.router.trust.allows(CANDIDATE.url))
        self.assertEqual([entry for entry in engine.keyed() if entry[2] == 300.0], [])


class ReadmissionCredentialTests(CredentialOrderHarness):
    async def readmit(self, iid):
        self.router.scheduler.eject(iid, "liveness")
        return await validate_readmission(
            self.router, [iid], wave=False, transport=self.stubs.transport
        )

    async def test_a_replacement_whose_attestation_fails_never_receives_the_key(self):
        await self.verify_members()
        engine = self.stub("e3")
        engine.start, engine.honest = 150.0, False
        before = len(engine.log)
        outcome = await self.readmit("e3")
        self.assertFalse(outcome.passed)
        self.assertIn("attestation:", " ".join(outcome.failures["e3"]))
        self.assertEqual([entry for entry in engine.log[before:] if entry[1]], [])
        self.assertFalse(self.router.trust.allows(engine.spec.url))

    async def test_a_replacement_receives_the_key_only_after_its_new_attestation(self):
        await self.verify_members()
        engine = self.stub("e3")
        engine.start = 150.0
        self.bind_profile("e3")
        outcome = await self.readmit("e3")
        self.assertTrue(outcome.passed, outcome.failures)
        self.assertTrue([entry for entry in engine.keyed() if entry[2] == 150.0])
        self.assert_key_follows_verification("e3")
        self.assertEqual(self.router.trust.start(engine.spec.url), 150.0)


class ProbeAndHealthCredentialTests(CredentialOrderHarness):
    async def test_health_checks_never_carry_the_key(self):
        await self.verify_members()
        for spec in self.cfg.engines:
            self.assertTrue(await self.router.engines.healthy(spec.url))
        self.assertTrue(await self.router.engines.healthy(CANDIDATE.url))
        for iid in ("e0", "e3", "e6"):
            self.assertNotIn(("/health", True), [entry[:2] for entry in self.stub(iid).log])

    async def test_an_inference_probe_carries_the_key_only_to_a_verified_engine(self):
        probe = self.router.engines.probe_inference
        await probe(CANDIDATE.url, deadline_s=1.0)
        self.assert_never_keyed("e6")
        await self.verify_members()
        member = self.cfg.engines[0].url
        await probe(member, deadline_s=1.0)
        self.assertTrue(self.stub("e0").keyed())
        self.assert_key_follows_verification("e0")

    async def test_the_identity_sweep_of_a_router_taking_over_keys_only_verified_engines(self):
        # A router that takes over trusts no engine until its own sweep verifies it.
        self.assertFalse(self.router.trust.allows(self.cfg.engines[0].url))
        self.stub("e3").honest = False
        self.assertEqual(await check_process_identities(self.router), ["e3"])
        self.assertTrue(self.router.trust.allows(self.cfg.engines[0].url))
        self.assertFalse(self.router.trust.allows(self.cfg.engines[1].url))
        self.assert_never_keyed("e3")
        self.assert_key_follows_verification("e0")


class GuardedIdentityTests(unittest.IsolatedAsyncioTestCase):
    """An engine whose version route needs the key gets it only after its attestation."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {KEY_VARIABLE: KEY})
        environment.start()
        self.addCleanup(environment.stop)
        self.cfg = fleet(Path(folder.name))
        self.cfg.backend = "sglang"
        self.cfg.engine_api_key_env = KEY_VARIABLE
        self.spec = self.cfg.engines[0]
        self.engine = StubEngine(self.spec, self.cfg.engine_contract, 100.0, guarded=True)
        self.transport = httpx.MockTransport(self.engine.answer)

    async def test_a_failed_attestation_ends_the_profile_read_before_the_key(self):
        self.engine.honest = False
        with self.assertRaises(AttestationMismatch):
            await read_generation(self.cfg, self.spec, transport=self.transport)
        self.assertEqual(self.engine.keyed(), [])
        self.assertNotIn("/server_info", [path for path, _, _ in self.engine.log])

    async def test_the_version_read_carries_the_key_after_the_attestation(self):
        generation = await read_generation(self.cfg, self.spec, transport=self.transport)
        self.assertEqual(generation.process_start_time_seconds, 100.0)
        paths = [path for path, _, _ in self.engine.log]
        self.assertLess(paths.index("/v1/attestation"), paths.index("/server_info"))
        self.assertEqual(self.engine.keyed(), [("/server_info", True, 100.0)])


if __name__ == "__main__":
    unittest.main()
