"""Keep saved cost curves tied to the live engine process and contract."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from narwhal.diagnostics.check import Report, gate_profile_generation
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.profiling.generation import read_generation
from narwhal.profiling.store import ProfileStore
from narwhal.serving.app import create_app
from tests.fixtures import fleet, profile


class ProfileGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.starts = {"e0": 100.0, "e3": 100.0}
        self.by_host = {spec.url.split(":")[-1]: spec.iid for spec in self.cfg.engines}
        contract = self.cfg.engine_contract
        self.document = AttestationDocument(contract, dict.fromkeys(contract.fields(), "fixture"))
        self.transport = httpx.MockTransport(self.respond)
        store = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            generation = await read_generation(
                spec, contract, timeout_s=1, transport=self.transport
            )
            store.put(replace(profile(spec.iid), generation_digest=generation.digest))

    def respond(self, request):
        iid = self.by_host[str(request.url.port)]
        identity = EngineIdentity(self.cfg.engine_contract.vllm_version, self.starts[iid])
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": identity.vllm_version})
        if request.url.path == "/metrics":
            return httpx.Response(
                200, text=f"process_start_time_seconds {identity.process_start_time_seconds}\n"
            )
        if request.url.path == "/v1/attestation":
            return httpx.Response(200, json=make_attestation(self.document, identity))
        raise AssertionError(request.url.path)

    async def test_preflight_accepts_current_generation_and_refreshed_profile(self):
        store = ProfileStore(self.cfg.profiles_path)
        report = Report()
        unsafe = await gate_profile_generation(
            self.cfg, store, {"e0", "e3"}, report, self.transport
        )
        self.assertEqual(unsafe, set())
        self.assertEqual(report.failed, [])

        app = create_app(self.cfg, lifecycle_transport=self.transport)
        async with app.router.lifespan_context(app):
            self.assertFalse(app.state.router.standby)

        self.starts["e3"] = 101.0
        report = Report()
        unsafe = await gate_profile_generation(
            self.cfg, store, {"e0", "e3"}, report, self.transport
        )
        self.assertEqual(unsafe, {"e3"})
        self.assertEqual(
            report.failed,
            ["e3 profile generation differs from the live engine; reprofile before admission"],
        )

        generation = await read_generation(
            self.cfg.engines[1], self.cfg.engine_contract, timeout_s=1, transport=self.transport
        )
        store.put(replace(profile("e3"), generation_digest=generation.digest))
        report = Report()
        self.assertEqual(
            await gate_profile_generation(self.cfg, store, {"e0", "e3"}, report, self.transport),
            set(),
        )
        self.assertEqual(report.failed, [])

    async def test_router_startup_rejects_changed_and_legacy_generations(self):
        self.starts["e3"] = 101.0
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        with self.assertRaisesRegex(RuntimeError, "e3 profile generation differs.*reprofile"):
            async with app.router.lifespan_context(app):
                pass
        await app.state.router.engines.aclose()

        document = json.loads(self.cfg.profiles_path.read_text())
        for row in document["profiles"]:
            if row["iid"] == "e3":
                row.pop("generation_digest")
        self.cfg.profiles_path.write_text(json.dumps(document))
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        with self.assertRaisesRegex(
            RuntimeError, "e3 profile has no generation evidence.*reprofile"
        ):
            async with app.router.lifespan_context(app):
                pass
        await app.state.router.engines.aclose()

    async def test_verified_attestation_rejects_stale_sidecar(self):
        self.starts["e0"] = 101.0
        stale = make_attestation(
            self.document, EngineIdentity(self.cfg.engine_contract.vllm_version, 100.0)
        )

        def stale_response(request):
            if request.url.path == "/v1/attestation":
                return httpx.Response(200, json=stale)
            return self.respond(request)

        with self.assertRaisesRegex(ValueError, "e0: attestation: engine process started"):
            await read_generation(
                self.cfg.engines[0],
                self.cfg.engine_contract,
                timeout_s=1,
                transport=httpx.MockTransport(stale_response),
            )
