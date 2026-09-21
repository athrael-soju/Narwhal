"""Check the release's HTTP and telemetry names against shipped consumers."""

import json
import re
import tempfile
import unittest
from pathlib import Path

import httpx

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.contracts import METRICS, current
from narwhal.scheduling.scheduler import GlobalScheduler
from narwhal.serving.app import create_app
from narwhal.serving.router import NarwhalRouter
from narwhal.types import Instance, Request, Role

ROOT = Path(__file__).resolve().parents[2]


class PublicNamespaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(
            FleetConfig(
                model="stub",
                slo=SLO(ttft_s=1.0, tpot_s=0.02),
                profiles_path=Path(self.temp.name) / "profiles.json",
                engines=[
                    EngineSpec("p", "http://prefill", Role.PREFILL),
                    EngineSpec("d", "http://decode", Role.DECODE),
                ],
            )
        )
        self.router = self.app.state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://router"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_control_documents_use_narwhal_routes(self):
        self.assertIsInstance(self.router, NarwhalRouter)
        for endpoint, schema in (
            ("state", "narwhal.state"),
            ("handoff", "narwhal.handoff"),
            ("lifecycle", "narwhal.lifecycle"),
        ):
            with self.subTest(endpoint=endpoint):
                response = await self.client.get(f"/narwhal/{endpoint}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["schema"], schema)

    async def test_openapi_includes_control_actions_and_completion_routes(self):
        response = await self.client.get("/openapi.json")
        paths = response.json()["paths"]
        for path, method in (
            ("/narwhal/state", "get"),
            ("/narwhal/handoff", "get"),
            ("/narwhal/lifecycle", "get"),
            ("/narwhal/lifecycle/drain", "post"),
            ("/narwhal/lifecycle/readmit", "post"),
            ("/v1/completions", "post"),
            ("/v1/chat/completions", "post"),
            ("/health", "get"),
            ("/ready", "get"),
            ("/metrics", "get"),
        ):
            with self.subTest(path=path):
                self.assertIn(method, paths[path])
        self.assertFalse(any(path.startswith("/arrow/") for path in paths))

    async def test_retired_control_routes_return_404(self):
        for path in ("state", "handoff", "lifecycle", "lifecycle/drain", "lifecycle/readmit"):
            with self.subTest(path=path):
                method = self.client.post if "/" in path else self.client.get
                response = await method(f"/arrow/{path}")
                self.assertEqual(response.status_code, 404)

    async def test_metrics_use_narwhal_prefix_and_contract_version_one(self):
        self.router.served = 7
        response = await self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(current(METRICS), 1)
        self.assertIn('narwhal_contract_info{contract="metrics",version="1"} 1', response.text)
        self.assertIn("narwhal_served_total 7", response.text)
        self.assertIn('narwhal_instance_role{iid="p",role="prefill"} 1', response.text)
        self.assertIn('narwhal_slo_seconds{metric="ttft"} 1.0', response.text)
        self.assertIn('narwhal_slo_seconds{metric="tpot"} 0.02', response.text)
        self.assertIn("narwhal_event_loop_lag_seconds 0.0", response.text)
        self.assertIn("narwhal_event_loop_lag_high_water_seconds 0.0", response.text)
        names = re.findall(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)", response.text, re.M)
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("narwhal_") for name in names))

    async def test_state_and_metrics_share_event_loop_lag(self):
        self.router.monitoring.observe_event_loop_lag(0.03)
        self.router.monitoring.observe_event_loop_lag(0.01)

        state_response = await self.client.get("/narwhal/state")
        metrics_response = await self.client.get("/metrics")

        self.assertEqual(state_response.status_code, 200, state_response.text)
        monitoring = state_response.json()["monitoring"]
        self.assertEqual(monitoring["event_loop_lag_s"], 0.01)
        self.assertEqual(monitoring["event_loop_lag_high_water_s"], 0.03)
        self.assertIn("narwhal_event_loop_lag_seconds 0.01", metrics_response.text)
        self.assertIn("narwhal_event_loop_lag_high_water_seconds 0.03", metrics_response.text)

    async def test_state_retains_documented_controller_decision_fields(self):
        details = {
            "eligibility_rule": "projected_ttft_recovery",
            "confirmations": 1,
            "required_confirmations": 1,
            "trigger_rid": "request-1",
            "projected_ttft_s": 1.4,
            "ttft_slo_s": 1.0,
            "trigger_projected_ttft_ratio": 1.4,
            "resident_prefill_s": 0.8,
            "queued_prefill_s": 0.6,
            "waiting_prefill": 2,
            "initial_projected_ttft_s": 1.6,
            "urgent_signals": 2,
            "event_to_evaluation_s": 0.2,
            "candidate_projected_ttft_s": 0.9,
            "candidate_projected_ttft_ratio": 0.9,
            "projected_ttft_improvement_s": 0.5,
            "decode_capacity_safe": True,
            "role_floors_safe": True,
            "source_pressure_safe": True,
            "recovery_prefill_ratio": 1.2,
            "evidence_span_s": 60.0,
            "evidence_arrivals": 12,
            "evidence_required_span_s": 60.0,
            "evidence_required_arrivals": 10,
            "evidence_max_span_s": 120.0,
            "evidence_closed": True,
            "risk_kind": "first_token_timeout",
            "risk_age_s": 70.0,
            "evidence_short_decode_engines": 1.25,
            "evidence_long_decode_engines": 1.0,
            "evidence_trend_ratio": 1.25,
            "evidence_envelope_decode_engines": 1.25,
            "evidence_blocked_gate": "none",
        }
        self.router.scheduler.record_decision(
            prefill=1,
            decode=1,
            by="reactive",
            reason="projected TTFT recovered",
            result="applied",
            details=details,
        )

        response = await self.client.get("/narwhal/state")

        self.assertEqual(response.status_code, 200, response.text)
        decision = response.json()["control"]["last_decision"]
        for field, value in details.items():
            with self.subTest(field=field):
                self.assertEqual(decision[field], value)

    async def test_dashboard_and_alerts_use_router_metric_names(self):
        dashboard_path = ROOT / "tools/grafana-narwhal.json"
        dashboard = json.loads(dashboard_path.read_text())
        self.assertEqual(dashboard["metadata"]["name"], "narwhal-router")
        layout = dashboard["spec"]["layout"]
        self.assertEqual(layout["kind"], "GridLayout")
        self.assertEqual(len(layout["spec"]["items"]), 15)
        engine_panel = dashboard["spec"]["elements"]["panel-7"]["spec"]
        queries = engine_panel["data"]["spec"]["queries"]
        self.assertEqual([query["spec"]["refId"] for query in queries], list("ABCDEFGHI"))
        self.assertEqual(
            engine_panel["data"]["spec"]["transformations"][0]["spec"]["options"]["byField"],
            "iid",
        )
        self.assertNotIn("gpu-telemetry", dashboard_path.read_text())
        response = await self.client.get("/metrics")
        required = {
            "tools/grafana-narwhal.json": ("narwhal_failed_total", "narwhal_served_total"),
            "tools/prometheus-alerts.yml": (
                "narwhal_failed_total",
                "narwhal_pool_instances",
                "narwhal_unserved_total",
            ),
        }
        for relative, names in required.items():
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text()
                self.assertNotIn("arrow_", text)
                self.assertNotIn("/arrow/", text)
                for name in names:
                    self.assertIn(name, text)
                    self.assertIn(name, response.text)

    async def test_dashboard_scopes_latency_quantiles_to_the_selected_router(self):
        dashboard = json.loads((ROOT / "tools/grafana-narwhal.json").read_text())
        elements = dashboard["spec"]["elements"]
        panels = {
            elements[name]["spec"]["title"]: elements[name]["spec"]
            for name in ("panel-50", "panel-51", "panel-52")
        }
        self.assertEqual(
            set(panels),
            {"Time to first token", "Time per output token", "Request waiting time"},
        )
        for title, panel in panels.items():
            with self.subTest(title=title):
                expressions = [
                    query["spec"]["query"]["spec"]["expr"]
                    for query in panel["data"]["spec"]["queries"]
                ]
                self.assertTrue(all('instance=~"$router"' in expr for expr in expressions))
                self.assertTrue(all("$iid" not in expr for expr in expressions))
                for expression in expressions:
                    if "histogram_quantile" in expression:
                        self.assertIn("sum by (instance, le)", expression)
                        self.assertIn("[$__rate_interval]", expression)

        ttft_queries = panels["Time to first token"]["data"]["spec"]["queries"]
        tpot_queries = panels["Time per output token"]["data"]["spec"]["queries"]
        self.assertEqual(
            [query["spec"]["query"]["spec"]["legendFormat"] for query in ttft_queries],
            ["p50", "p95", "p99", "SLO"],
        )
        self.assertIn('metric="ttft"', ttft_queries[-1]["spec"]["query"]["spec"]["expr"])
        self.assertIn('metric="tpot"', tpot_queries[-1]["spec"]["query"]["spec"]["expr"])
        waiting = " ".join(
            query["spec"]["query"]["spec"]["expr"]
            for query in panels["Request waiting time"]["data"]["spec"]["queries"]
        )
        self.assertIn("narwhal_queue_wait_seconds_bucket", waiting)
        self.assertIn("narwhal_seat_seconds_bucket", waiting)
        exceptions = elements["panel-12"]["spec"]["data"]["spec"]["queries"]
        self.assertIn(
            "narwhal_retry_attempts_total",
            " ".join(query["spec"]["query"]["spec"]["expr"] for query in exceptions),
        )

    async def test_flip_metrics_outlive_history_and_reset_with_scheduler(self):
        scheduler = self.router.scheduler
        scheduler._flip_history = 2
        self.router.monitor.add(Instance("p2", "http://prefill2", Role.PREFILL))
        self.router.monitor.add(Instance("d2", "http://decode2", Role.DECODE))
        engine = self.router.monitor.instances["p"]
        engine.prefill["r1"] = Request("r1", 10)
        engine.decode["r2"] = Request("r2", 20)
        engine.decode["r3"] = Request("r3", 30)
        for index in range(12):
            target = Role.DECODE if index % 2 == 0 else Role.PREFILL
            self.assertIs(
                scheduler.flip(
                    target,
                    by="test:controller",
                    candidate=engine,
                    bypass_cooldown=True,
                    bypass_dwell=True,
                ),
                engine,
            )
            response = await self.client.get("/metrics")
            self.assertIn(f"narwhal_flip_reversals_total {index}", response.text)
            self.assertIn(
                f'narwhal_flip_inflight_total{{phase="decode"}} {2 * (index + 1)}\n',
                response.text,
            )
        self.assertEqual(len(scheduler.flips), 2)
        for target in ("prefill", "decode"):
            self.assertIn(
                f'narwhal_flips_total{{to="{target}",by="test:controller"}} 6\n',
                response.text,
            )
        state = (await self.client.get("/narwhal/state")).json()
        self.assertEqual(state["control"]["flip_reversals"], 11)
        self.assertEqual(state["control"]["flip_inflight"], {"prefill": 12, "decode": 24})
        self.router.scheduler = GlobalScheduler(
            self.router.monitor, self.router.profiles, scheduler.slo
        )
        response = await self.client.get("/metrics")
        self.assertIn("narwhal_flip_reversals_total 0\n", response.text)
        self.assertNotIn("narwhal_flips_total{to=", response.text)
        self.assertIn('narwhal_flip_inflight_total{phase="decode"} 0\n', response.text)

    async def test_flip_refusals_outlive_the_twenty_record_state_window(self):
        for count in range(1, 31):
            # Only one decode engine remains, so the source floor refuses the move.
            self.assertIsNone(self.router.scheduler.flip(Role.PREFILL))
            response = await self.client.get("/metrics")
            self.assertIn(f"narwhal_flips_refused_total {count}\n", response.text)
        state = (await self.client.get("/narwhal/state")).json()
        self.assertEqual(len(state["flips_refused"]), 20)
        self.assertEqual(state["control"]["flips_refused"], 30)
        self.router.scheduler.flips_refused.clear()
        response = await self.client.get("/metrics")
        self.assertIn("narwhal_flips_refused_total 30\n", response.text)

    async def test_each_refusal_path_counts_once(self):
        self.router.monitor.add(Instance("p2", "http://prefill2", Role.PREFILL))
        self.router.monitor.add(Instance("d2", "http://decode2", Role.DECODE))
        scheduler = self.router.scheduler
        # Opening cooldown.
        self.assertIsNone(scheduler.flip(Role.DECODE))
        self.assertEqual(scheduler.control_snapshot()["flips_refused"], 1)
        # The nested decode-floor guard must not be double-counted by flip().
        scheduler.min_decode = 2
        self.assertIsNone(scheduler.flip(Role.PREFILL))
        self.assertEqual(scheduler.control_snapshot()["flips_refused"], 2)
        scheduler.min_decode = 1
        scheduler.pinned = frozenset({"d", "d2"})
        self.assertIsNone(scheduler.flip(Role.PREFILL))
        self.assertEqual(scheduler.control_snapshot()["flips_refused"], 3)
        scheduler.pinned = frozenset()
        scheduler.th.dwell_s = 60
        scheduler._last_flip = {iid: scheduler._clock() for iid in ("d", "d2")}
        self.assertIsNone(scheduler.flip(Role.PREFILL))
        self.assertEqual(scheduler.control_snapshot()["flips_refused"], 4)
        scheduler.advisory = True
        self.assertIsNone(scheduler.flip(Role.PREFILL, bypass_dwell=True))
        state = scheduler.control_snapshot()
        self.assertEqual(state["flips_refused"], 5)
        self.assertEqual(state["flips"], {})
        self.assertEqual(state["last_decision"]["result"], "advisory")

    async def test_haproxy_health_route_remains_available(self):
        config = (ROOT / "deploy/ha/haproxy.cfg").read_text()
        path = re.search(r"http-check send meth GET uri (\S+)", config)[1]
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
