import asyncio
import io
import json
import re
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load as load_backend
from narwhal.config import FleetConfig
from narwhal.diagnostics.check import preflight
from narwhal.diagnostics.check.engines import gate_contract
from narwhal.diagnostics.check.report import Report
from narwhal.engines.client import EngineClient, InferenceProbe, ProbeLeg
from narwhal.observability.metrics.render import render
from narwhal.profiling.generation import binding_digest
from narwhal.profiling.store import ProfileStore
from narwhal.runtime.release import PeerRelease, release_peers
from narwhal.serving.app import create_app
from tests.characterization.golden import assert_golden
from tests.characterization.vllm_fake import ENGINES, FakeVllm, routed, untimed
from tests.fixtures import ROOT, fleet, profile
from tests.wire import EngineWire, engine_transports, vllm_engine

SECONDS = re.compile(r"[0-9]+\.[0-9]+s\b")


class PreflightCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.engine = FakeVllm(ENGINES, self.cfg.engine_contract)

    async def test_preflight_requests_report_and_evidence(self):
        digest = binding_digest(self.engine.attestation())
        store = ProfileStore(self.cfg.profiles_path, load=False)
        for spec in self.cfg.engines:
            store.put(profile(spec.iid, generation_digest=digest))
        fleet_path = self.root / "fleet.json"
        self.cfg.save(fleet_path)
        evidence_path = self.root / "kv-evidence.json"
        report = Report()
        output = io.StringIO()
        with (
            patch.object(
                preflight,
                "EngineClient",
                side_effect=lambda **kwargs: EngineClient(
                    **kwargs, **engine_transports(self.engine)
                ),
            ),
            patch("httpx.AsyncClient", routed(self.engine)),
            redirect_stdout(output),
        ):
            code = await preflight.run(
                self.cfg,
                mesh=True,
                skip_kv=False,
                report=report,
                evidence_out=evidence_path,
                fleet_path=fleet_path,
            )
        document = json.loads(evidence_path.read_text())
        document.update(
            fleet_sha256="<sha256>",
            profile_sha256="<sha256>",
        )
        assert_golden(
            self,
            "preflight_run",
            {
                "exit_code": code,
                "output": SECONDS.sub(
                    "<seconds>", output.getvalue().replace(str(self.root), "<root>")
                ).splitlines(),
                "evidence": untimed(document),
                "requests": self.engine.requests,
            },
        )

    async def test_mixed_versions_fail_before_transfer(self):
        self.cfg.engine_contract = None
        versions = {"e0": "1.0.0", "e3": "1.0.1"}

        def respond(request):
            response = self.engine(request)
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": versions[self.engine.iid(request)]})
            return response

        report = Report()
        output = io.StringIO()
        with redirect_stdout(output):
            unsafe = await gate_contract(
                self.cfg, {"e0", "e3"}, report, transport=httpx.MockTransport(respond)
            )
        assert_golden(
            self,
            "preflight_mixed_versions",
            {
                "unsafe": sorted(unsafe),
                "failed": report.failed,
                "skipped": report.skipped,
                "output": output.getvalue().splitlines(),
                "requests": self.engine.requests,
            },
        )


class FabricCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def router(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.engine_contract = None
        cfg.liveness_every = 0
        cfg.state_path = self.root / "handoff.json"
        cfg.profiles_path = self.root / "profiles.json"
        router = create_app(cfg).state.router
        for spec in cfg.engines:
            router.profiles.put(profile(spec.iid))
        self.addAsyncCleanup(router.engines.aclose)
        return router

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    async def test_peer_release_rounds(self):
        router = self.router()
        now = [1000.0]
        router.peer_release = PeerRelease(lambda: now[0])
        urls = {spec.url: spec.iid for spec in router.cfg.engines}
        calls = []
        missed = {"e1"}

        async def probe(url, *, prefill_url=None, deadline_s=None, producer=None):
            consumer = urls[url]
            calls.append([urls[prefill_url], consumer])
            if consumer in missed:
                missed.discard(consumer)
                return InferenceProbe(prefill=ProbeLeg(inconclusive=True), decode=ProbeLeg())
            return InferenceProbe(prefill=ProbeLeg(), decode=ProbeLeg())

        router.engines.probe_inference = AsyncMock(side_effect=probe)
        router.scheduler.eject("e5", "liveness")
        rounds = []
        for second in range(4000):
            now[0] = 1000.0 + second
            release_peers(router)
            if router.peer_release.tasks:
                await asyncio.gather(*router.peer_release.tasks)
                rounds.append({"after_s": second, "probes": calls})
                calls = []
        assert_golden(
            self,
            "preflight_peer_release",
            {"rounds": rounds, "state": router.state()["peer_release"]},
        )

    async def test_engine_bind_check(self):
        outcomes = {}
        for name, nixl in (("plain", False), ("nixl", True)):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            outcomes[f"{name}_free"] = self.bind(port, nixl)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
                listener.listen(1)
                outcomes[f"{name}_listening"] = self.bind(port, nixl)
            with socket.socket() as bound:
                bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                bound.bind(("127.0.0.1", port))
                outcomes[f"{name}_bound_reusable"] = self.bind(port, nixl)
        assert_golden(self, "preflight_engine_bind", outcomes)

    @staticmethod
    def bind(port, nixl):
        try:
            load_backend("vllm").fabric.check_engine_bind("127.0.0.1", port, fabric=nixl)
        except OSError as exc:
            return type(exc).__name__
        return "ok"

    async def test_keep_alive_expiry(self):
        engine = FakeVllm(ENGINES)
        wire = EngineWire(engine)
        client = EngineClient(**vllm_engine(), dial=wire.dial)
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(wire.aclose)
        loop = asyncio.get_running_loop()
        clock = loop.time
        offset = [0.0]
        loop.time = lambda: clock() + offset[0]
        self.addCleanup(delattr, loop, "time")
        # Debug mode would report each clock jump as a slow callback.
        loop.slow_callback_duration = float("inf")
        body = {"model": "test-model", "prompt": "a b", "max_tokens": 1}
        connections = {}
        await client.prefill("http://engine-0.invalid:8000", "/v1/completions", body, {})
        for idle in (3.9, 4.0):
            offset[0] += idle
            await client.prefill("http://engine-0.invalid:8000", "/v1/completions", body, {})
            connections[f"after {idle}s idle"] = len(wire.dials)
        assert_golden(self, "preflight_keep_alive", connections)

    async def test_lease_metrics_and_dashboard_series(self):
        router = self.router()
        transfer = {
            "kv_connector": "NixlConnector",
            "kv_connector_extra_config": {"kv_lease_duration": 30},
        }
        router.attested("e0", {"launch": {"args": ["--kv-transfer-config", json.dumps(transfer)]}})
        text = render(router.state(), router.ttft, router.tpot, router.seat, router.queue_wait)
        lines = [
            line
            for line in text.splitlines()
            if line.startswith(("narwhal_kv_lease_seconds", "narwhal_handoff_bound_seconds"))
        ]
        dashboard = (ROOT / "tools/observability/grafana-narwhal.json").read_text()
        series = sorted(set(re.findall(r"vllm:[A-Za-z0-9_:]+", dashboard)))
        assert_golden(
            self, "preflight_observability", {"lease_metrics": lines, "dashboard_series": series}
        )
