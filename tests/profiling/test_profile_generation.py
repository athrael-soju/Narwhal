"""Keep saved cost curves tied to the live engine process and contract."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from narwhal.diagnostics.profile_gates import gate_profile_generation
from narwhal.diagnostics.report import Report
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.profiling.calibration import verify_calibration
from narwhal.profiling.generation import read_generation
from narwhal.profiling.store import ProfileStore
from narwhal.serving.app import create_app
from tests.fixtures import calibration_document, fleet, profile

LAUNCH = {"image_id": "sha256:" + "a" * 64, "args": ["--max-num-seqs", "64"]}


class ProfileGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.starts = {"e0": 100.0, "e3": 100.0}
        self.version = self.cfg.engine_contract.vllm_version
        self.by_host = {httpx.URL(spec.url).host: spec.iid for spec in self.cfg.engines}
        contract = self.cfg.engine_contract
        document = AttestationDocument(contract, dict.fromkeys(contract.fields(), "fixture"))
        self.documents = {spec.iid: document for spec in self.cfg.engines}
        self.transport = httpx.MockTransport(self.respond)
        await self._store_profiles()

    async def _store_profiles(self):
        store = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            generation = await read_generation(
                spec, self.cfg.engine_contract, timeout_s=1, transport=self.transport
            )
            store.put(replace(profile(spec.iid), generation_digest=generation.digest))

    def respond(self, request):
        iid = self.by_host[request.url.host]
        identity = EngineIdentity(self.version, self.starts[iid])
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": identity.vllm_version})
        if request.url.path == "/metrics":
            return httpx.Response(
                200, text=f"process_start_time_seconds {identity.process_start_time_seconds}\n"
            )
        if request.url.path == "/v1/attestation":
            return httpx.Response(200, json=make_attestation(self.documents[iid], identity))
        raise AssertionError(request.url.path)

    async def _serve_launch(self):
        self.documents = {
            iid: replace(document, launch=LAUNCH) for iid, document in self.documents.items()
        }
        await self._store_profiles()

    async def _calibrate(self):
        live = {
            spec.iid: await read_generation(
                spec, self.cfg.engine_contract, timeout_s=1, transport=self.transport
            )
            for spec in self.cfg.engines
        }
        document = calibration_document(
            self.cfg,
            {iid: generation.digest for iid, generation in live.items()},
            {iid: generation.process_start_time_seconds for iid, generation in live.items()},
        )
        path = self.root / "calibration.json"
        path.write_text(json.dumps(document))
        self.cfg.first_token_calibration_path = path

    async def test_identical_relaunch_reuses_a_launch_bound_calibration(self):
        await self._serve_launch()
        await self._calibrate()
        self.starts["e3"] = 101.0
        check = await verify_calibration(self.cfg, transport=self.transport)
        self.assertEqual((check.status, check.problems), ("reused", ()))
        self.assertEqual(check.engines, {"e0": "measured", "e3": "reused"})

    async def test_unchanged_processes_report_a_measured_calibration(self):
        await self._serve_launch()
        await self._calibrate()
        check = await verify_calibration(self.cfg, transport=self.transport)
        self.assertEqual((check.status, check.problems), ("measured", ()))
        self.assertEqual(check.engines, {"e0": "measured", "e3": "measured"})
        self.assertEqual(check.process_starts, {"e0": 100.0, "e3": 100.0})
        self.assertEqual(check.path, self.cfg.first_token_calibration_path)

    async def test_contract_free_fleet_rejects_calibration_after_relaunch(self):
        self.cfg.engine_contract = None
        await self._calibrate()
        self.starts["e3"] = 101.0
        check = await verify_calibration(self.cfg, transport=self.transport)
        self.assertEqual(check.status, "rejected")
        self.assertEqual(check.problems, ("e3 process differs from first-token calibration",))

    async def test_changed_engine_argument_rejects_calibration(self):
        await self._serve_launch()
        await self._calibrate()
        self.documents["e3"] = replace(
            self.documents["e3"], launch={**LAUNCH, "args": ["--max-num-seqs", "32"]}
        )
        self.starts["e3"] = 101.0
        check = await verify_calibration(self.cfg, transport=self.transport)
        self.assertEqual(check.status, "rejected")
        self.assertEqual(check.problems, ("e3 process differs from first-token calibration",))

    async def test_changed_image_rejects_calibration(self):
        await self._serve_launch()
        await self._calibrate()
        contract = replace(self.cfg.engine_contract, image_digest="sha256:" + "1" * 64)
        self.cfg.engine_contract = contract
        for iid in self.documents:
            self.documents[iid] = AttestationDocument(
                contract,
                dict.fromkeys(contract.fields(), "fixture"),
                {**LAUNCH, "image_id": "sha256:" + "b" * 64},
            )
            self.starts[iid] = 101.0
        check = await verify_calibration(self.cfg, transport=self.transport)
        self.assertEqual(check.status, "rejected")
        self.assertEqual(
            check.problems,
            (
                "first-token calibration engine contract differs from the fleet",
                "e0 process differs from first-token calibration",
                "e3 process differs from first-token calibration",
            ),
        )

    async def test_router_starts_with_a_calibration_reused_after_an_identical_relaunch(self):
        await self._serve_launch()
        await self._calibrate()
        self.starts["e3"] = 101.0
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        router = app.state.router
        with self.assertLogs("narwhal.app", "INFO") as logs:
            async with app.router.lifespan_context(app):
                state = router.state()
        self.assertEqual(
            state["first_token_calibration"],
            {
                "status": "reused",
                "captured_at_unix": 1000.0,
                "candidate_deadline_s": 0.8,
                "engines": {"e0": "measured", "e3": "reused"},
            },
        )
        self.assertIn(
            "first-token calibration reused for e3: launch unchanged since capture; "
            "candidate 0.800s, deadline 2.5s",
            "\n".join(logs.output),
        )

    async def test_router_start_rejects_a_calibration_after_a_changed_launch(self):
        await self._serve_launch()
        await self._calibrate()
        self.documents["e3"] = replace(
            self.documents["e3"], launch={**LAUNCH, "args": ["--max-num-seqs", "32"]}
        )
        self.starts["e3"] = 101.0
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        with self.assertRaisesRegex(
            RuntimeError, "^e3 process differs from first-token calibration$"
        ):
            async with app.router.lifespan_context(app):
                pass
        await app.state.router.engines.aclose()

    async def test_state_marks_an_engine_relaunched_inside_the_router_as_reused(self):
        await self._serve_launch()
        await self._calibrate()
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        router = app.state.router
        async with app.router.lifespan_context(app):
            measured = router.state()["first_token_calibration"]
            router.lifecycle.process_starts["e3"] = 101.0
            relaunched = router.state()["first_token_calibration"]
        self.assertEqual(measured["status"], "measured")
        self.assertEqual(measured["engines"], {"e0": "measured", "e3": "measured"})
        self.assertEqual(relaunched["status"], "reused")
        self.assertEqual(relaunched["engines"], {"e0": "measured", "e3": "reused"})

    async def test_state_reports_uncalibrated_without_a_calibration_path(self):
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        router = app.state.router
        uncalibrated = {
            "status": "uncalibrated",
            "captured_at_unix": None,
            "candidate_deadline_s": None,
            "engines": {},
        }
        self.assertEqual(router.state()["first_token_calibration"], uncalibrated)
        with self.assertLogs("narwhal.app", "WARNING") as logs:
            async with app.router.lifespan_context(app):
                state = router.state()
        self.assertEqual(state["first_token_calibration"], uncalibrated)
        self.assertIn("first-token deadline is uncalibrated", "\n".join(logs.output))

    async def test_state_route_returns_the_first_token_calibration(self):
        await self._serve_launch()
        await self._calibrate()
        self.starts["e3"] = 101.0
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://router"
            ) as client,
        ):
            response = await client.get("/narwhal/state")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body["first_token_calibration"],
            {
                "status": "reused",
                "captured_at_unix": 1000.0,
                "candidate_deadline_s": 0.8,
                "engines": {"e0": "measured", "e3": "reused"},
            },
        )
        keys = list(body)
        self.assertEqual(
            keys.index("first_token_calibration"), keys.index("first_token_timeout_s") + 1
        )

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

    async def test_an_identical_relaunch_keeps_its_profile_and_a_changed_launch_does_not(self):
        self.documents["e3"] = replace(
            self.documents["e3"], launch={"args": ["--max-num-seqs", "64"]}
        )
        spec = self.cfg.engines[1]
        contract = self.cfg.engine_contract
        before = await read_generation(spec, contract, timeout_s=1, transport=self.transport)
        store = ProfileStore(self.cfg.profiles_path)
        store.put(replace(profile("e3"), generation_digest=before.digest))
        self.starts["e3"] = 101.0
        after = await read_generation(spec, contract, timeout_s=1, transport=self.transport)
        self.assertEqual(after.digest, before.digest)
        self.assertNotEqual(after.process_digest, before.process_digest)
        report = Report()
        self.assertEqual(
            await gate_profile_generation(self.cfg, store, {"e3"}, report, self.transport), set()
        )
        self.documents["e3"] = replace(
            self.documents["e3"], launch={"args": ["--max-num-seqs", "32"]}
        )
        report = Report()
        self.assertEqual(
            await gate_profile_generation(self.cfg, store, {"e3"}, report, self.transport), {"e3"}
        )

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
        stale = make_attestation(self.documents["e0"], EngineIdentity(self.version, 100.0))

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
