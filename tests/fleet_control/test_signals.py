"""Fleet signals for the console status strip and the service's Prometheus metrics."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tools.fleet_control.app import create_app
from tools.fleet_control.config import ConfigError, ControlConfig, Hook, RouterEndpoint, load_config
from tools.fleet_control.jobs import Job
from tools.fleet_control.service import ControlService
from tools.fleet_control.signals import ALERTS_QUERY, FleetSignals, metrics, signal_routes

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "tests/data/fleet.json"
TOKEN = "fake-control-token-" + "0" * 24
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
ROUTER = "http://router.invalid:8000"
PROMETHEUS = "http://prometheus.invalid:9090"
STAMP = "2026-01-02T03:04:05+00:00"
STAMP_MS = 1767323045000


class Waiting:
    """A load-job runner whose jobs run until stopped."""

    def validate(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return params

    async def run(self, job: Job) -> Mapping[str, Any]:
        await asyncio.Event().wait()
        return {}


class FakeBackends:
    """Answer the router's readiness route and Prometheus's instant query route."""

    def __init__(self) -> None:
        self.ready = 200
        self.alerts: Any = {"status": "success", "data": {"resultType": "vector", "result": []}}
        self.queries: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "router.invalid" and request.url.path == "/ready":
            reason = "ready" if self.ready == 200 else "draining"
            return httpx.Response(self.ready, json={"reason": reason})
        if request.url.host == "prometheus.invalid" and request.url.path == "/api/v1/query":
            self.queries.append(request.url.params["query"])
            if isinstance(self.alerts, int):
                return httpx.Response(self.alerts, text="unavailable")
            return httpx.Response(200, json=self.alerts)
        return httpx.Response(404)


class SignalCase(unittest.IsolatedAsyncioTestCase):
    prometheus: str | None = None

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = ControlConfig(
            FLEET,
            {"restore": Hook("restore", ("true",))},
            runs_dir=Path(folder.name) / "runs",
            router=RouterEndpoint(ROUTER, 5.0),
            prometheus_url=self.prometheus,
        )
        self.service = ControlService(
            config, runner=Waiting(), env={"PATH": "/usr/bin:/bin"}, now=lambda: NOW
        )
        self.backends = FakeBackends()
        signals = FleetSignals(self.service, transport=httpx.MockTransport(self.backends.handle))
        app = create_app(self.service, TOKEN, routers=[signal_routes(signals)])
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c")
        self.addAsyncCleanup(self.client.aclose)
        self.auth = {"authorization": f"Bearer {TOKEN}"}

    async def fleet(self) -> dict[str, Any]:
        response = await self.client.get("/api/fleet", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body: dict[str, Any] = response.json()
        return body


class FleetSignalTests(SignalCase):
    async def test_the_router_readiness_is_reported_without_prometheus(self) -> None:
        self.assertEqual(
            await self.fleet(),
            {
                "router": {"path": "/ready", "ready": True, "status_code": 200, "reason": "ready"},
                "alerts": None,
            },
        )
        self.backends.ready = 503
        router = (await self.fleet())["router"]
        self.assertFalse(router["ready"])
        self.assertEqual((router["status_code"], router["reason"]), (503, "draining"))

    async def test_both_routes_require_the_token(self) -> None:
        for path in ("/api/fleet", "/metrics"):
            with self.subTest(path=path):
                self.assertEqual((await self.client.get(path)).status_code, 401)


class AlertSignalTests(SignalCase):
    prometheus = PROMETHEUS

    async def test_firing_alerts_are_listed_with_their_distinguishing_labels(self) -> None:
        self.backends.alerts["data"]["result"] = [
            {
                "metric": {
                    "__name__": "ALERTS",
                    "alertname": "NarwhalUnservedRising",
                    "alertstate": "firing",
                    "severity": "warn",
                },
                "value": [1767323045, "1"],
            },
            {
                "metric": {
                    "__name__": "ALERTS",
                    "alertname": "NarwhalEngineDown",
                    "alertstate": "firing",
                    "iid": "e1",
                    "instance": "10.0.0.1:8001",
                    "job": "engines",
                    "severity": "page",
                },
                "value": [1767323045, "1"],
            },
        ]
        alerts = (await self.fleet())["alerts"]
        self.assertEqual(self.backends.queries, [ALERTS_QUERY])
        self.assertEqual(
            alerts,
            {
                "firing": [
                    {"alertname": "NarwhalEngineDown", "labels": {"iid": "e1", "severity": "page"}},
                    {"alertname": "NarwhalUnservedRising", "labels": {"severity": "warn"}},
                ],
                "error": None,
            },
        )

    async def test_an_unavailable_prometheus_is_reported_and_not_counted(self) -> None:
        for answer in (503, {"status": "success"}):
            with self.subTest(answer=answer):
                self.backends.alerts = answer
                alerts = (await self.fleet())["alerts"]
                self.assertIsNone(alerts["firing"])
                self.assertTrue(alerts["error"])


class MetricsTests(SignalCase):
    async def scrape(self) -> str:
        response = await self.client.get("/metrics", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain; version=0.0.4"))
        return response.text

    async def test_without_a_session_only_the_session_gauge_has_a_sample(self) -> None:
        samples = [line for line in (await self.scrape()).splitlines() if line[:1] != "#"]
        self.assertEqual(samples, ["narwhal_control_session_active 0"])

    async def test_attempted_actions_become_annotation_samples(self) -> None:
        await self.service.start_session()
        session = self.service.session
        assert session is not None
        record = self.service.store.record
        record(session, "engine.drain", {"engine": "e1", "deadline_s": 30}, STAMP, STAMP, "ok")
        record(session, "engine.stop", {"engine": "e2"}, STAMP, STAMP, "refused")
        overlay = {"overlay": {"serving": {}, "slo": {}}}
        record(session, "config.overlay", overlay, STAMP, STAMP, "failed")
        record(session, "job.complete", {"job": "job-001"}, STAMP, STAMP, "ok")
        record(session, "engine.pause", {"engine": 'e"1\\'}, STAMP, STAMP, "ok")
        lines = (await self.scrape()).splitlines()
        actions = [line for line in lines if line.startswith("narwhal_control_action_started_ms")]
        self.assertIn("narwhal_control_session_active 1", lines)
        self.assertEqual(
            actions,
            [
                f'narwhal_control_action_started_ms{{session="{session.id}",seq="1",'
                f'action="session.start",target="",outcome="ok",title="session start"}} {STAMP_MS}',
                f'narwhal_control_action_started_ms{{session="{session.id}",seq="2",'
                f'action="engine.drain",target="e1",outcome="ok",title="drain e1"}} {STAMP_MS}',
                f'narwhal_control_action_started_ms{{session="{session.id}",seq="4",'
                f'action="config.overlay",target="serving,slo",outcome="failed",'
                f'title="overlay serving,slo"}} {STAMP_MS}',
                f'narwhal_control_action_started_ms{{session="{session.id}",seq="6",'
                f'action="engine.pause",target="e\\"1\\\\",outcome="ok",'
                f'title="pause e\\"1\\\\"}} {STAMP_MS}',
            ],
        )

    async def test_a_running_load_job_is_a_region_sample(self) -> None:
        await self.service.start_session()
        await self.service.start_job({"workload": "short", "duration_s": 60})
        self.assertIn(
            'narwhal_control_load_job_running{job="job-001",workload="short"} 1',
            (await self.scrape()).splitlines(),
        )
        await self.service.stop_job()
        self.assertNotIn("narwhal_control_load_job_running{", await self.scrape())
        self.assertEqual(metrics(self.service).count("# TYPE"), 3)


class PrometheusConfigTests(unittest.TestCase):
    def load(self, extra: dict[str, Any]) -> ControlConfig:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            raw = {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["true"]}}, **extra}
            path.write_text(json.dumps(raw))
            return load_config(path, {})

    def test_prometheus_is_optional(self) -> None:
        self.assertIsNone(self.load({}).prometheus_url)
        url = self.load({"prometheus_url": "http://127.0.0.1:9090/"}).prometheus_url
        self.assertEqual(url, "http://127.0.0.1:9090")

    def test_an_invalid_prometheus_url_is_reported(self) -> None:
        for value in ("127.0.0.1:9090", 9090, "ftp://host"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "prometheus_url"):
                self.load({"prometheus_url": value})
