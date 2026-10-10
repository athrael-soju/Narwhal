import json
import math
import re
import tempfile
import unittest
from pathlib import Path

import httpx

from narwhal.backends import load as load_backend
from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.contracts import METRICS, current
from narwhal.runtime.lifecycle.records import DrainRecord
from narwhal.runtime.release import PeerRelease
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.serving.app import create_app
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Instance, Request, Role
from tools.observability.start import readiness as observe_readiness

ROOT = Path(__file__).resolve().parents[2]

# Colour key in docs/observability/05-Dashboard.md.
DASHBOARD_PALETTE = {
    "prefill p50": "#1A847E",
    "prefill": "#299E97",
    "prefill p99": "#49B7B0",
    "decode p50": "#21537E",
    "decode": "#336B9E",
    "decode p99": "#69A2D8",
    "colocated": "#8C84CE",
    "good": "#56A64B",
    "state warning": "#C8963E",
    "state serious": "#E0752D",
    "state severe": "#A11D1D",
    "state pending": "#9E4AA4",
    "neutral": "#8E9196",
    "offered": "#5794F2",
    "cancelled": "#FFEE52",
    "warning": "#F2CC0C",
    "serious": "#FF9830",
    "invalid": "#FF7383",
    "critical": "#D44A3A",
    "severe": "#C4162A",
    "router delay": "#A352CC",
    "router occupancy": "#CA95E5",
    "retry": "#B877D9",
    "unsized": "#8AB8FF",
}


def dashboard():
    return json.loads((ROOT / "tools/observability/grafana-narwhal.json").read_text())


def panel_queries(element):
    return [
        query["spec"]["query"]["spec"]["expr"]
        for query in element["spec"]["data"]["spec"]["queries"]
    ]


def titled(title):
    spec = dashboard()["spec"]
    [name] = [name for name, item in spec["elements"].items() if item["spec"]["title"] == title]
    [grid] = [
        item["spec"]
        for item in spec["layout"]["spec"]["items"]
        if item["spec"]["element"]["name"] == name
    ]
    return spec["elements"][name]["spec"], grid


def legend_queries(panel):
    return {
        query["spec"]["query"]["spec"]["legendFormat"]: query["spec"]["query"]["spec"]["expr"]
        for query in panel["data"]["spec"]["queries"]
    }


def override_colors(panel):
    return {
        override["matcher"]["options"]: prop["value"]["fixedColor"]
        for override in panel["vizConfig"]["spec"]["fieldConfig"]["overrides"]
        for prop in override["properties"]
        if prop["id"] == "color"
    }


def alert_rules():
    text = (ROOT / "tools/observability/prometheus-alerts.yml").read_text()
    rules = {}
    for block in re.split(r"\n\s*- alert: ", text)[1:]:
        name, _, body = block.partition("\n")
        rules[name.strip()] = dict(re.findall(r"^\s+(expr|for|severity): (.+)$", body, re.M))
    return rules


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

    async def test_metrics_use_narwhal_prefix_and_contract_version_two(self):
        self.router.served = 7
        response = await self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(current(METRICS), 2)
        self.assertIn('narwhal_contract_info{contract="metrics",version="2"} 1', response.text)
        self.assertIn("narwhal_served_total 7", response.text)
        self.assertIn('narwhal_instance_role{iid="p",role="prefill"} 1', response.text)
        self.assertIn('narwhal_slo_seconds{metric="ttft",slo="1"} 1.0', response.text)
        self.assertIn('narwhal_slo_seconds{metric="tpot",slo="0.02"} 0.02', response.text)
        # Latency histograms carry the target that scaled their bucket edges.
        self.assertIn('narwhal_ttft_seconds_bucket{slo="1",le="0.025"}', response.text)
        self.assertIn('narwhal_tpot_seconds_count{slo="0.02"}', response.text)
        self.assertIn("narwhal_event_loop_lag_seconds 0.0", response.text)
        self.assertIn("narwhal_event_loop_lag_high_water_seconds 0.0", response.text)
        self.assertIn("narwhal_event_loop_busy_seconds_total 0.0", response.text)
        names = re.findall(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)", response.text, re.M)
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("narwhal_") for name in names))

    async def test_flip_counters_start_at_zero_for_every_caller(self):
        response = await self.client.get("/metrics")
        for by, to in (
            ("reactive", "prefill"),
            ("reactive", "decode"),
            ("decode_floor", "decode"),
            ("floor_recovery", "prefill"),
        ):
            self.assertIn(f'narwhal_flips_total{{to="{to}",by="{by}"}} 0', response.text)

    async def test_lifecycle_state_metric_reports_each_engine(self):
        self.router.lifecycle.records["p"] = DrainRecord(
            iid="p", state="blocked", requested_at=0.0, deadline_at=0.0, restart_required=False
        )
        response = await self.client.get("/metrics")
        self.assertIn('narwhal_engine_lifecycle_state{iid="p",state="blocked"} 1', response.text)
        self.assertIn('narwhal_engine_lifecycle_state{iid="d",state="active"} 1', response.text)

    async def test_quarantine_metric_reports_each_held_engine(self):
        self.router.scheduler.quarantined["p"] = math.inf
        self.router.scheduler.quarantined["d"] = self.router._clock() - 1.0
        response = await self.client.get("/metrics")
        self.assertIn('narwhal_engine_quarantined{iid="p"} 1', response.text)
        self.assertNotIn('narwhal_engine_quarantined{iid="d"}', response.text)

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

    async def test_state_and_metrics_share_event_loop_busy(self):
        self.router.monitoring.observe_event_loop_busy(1.5)

        state_response = await self.client.get("/narwhal/state")
        metrics_response = await self.client.get("/metrics")

        self.assertEqual(state_response.status_code, 200, state_response.text)
        self.assertEqual(state_response.json()["monitoring"]["event_loop_busy_s"], 1.5)
        self.assertIn("# TYPE narwhal_event_loop_busy_seconds_total counter", metrics_response.text)
        self.assertIn("narwhal_event_loop_busy_seconds_total 1.5", metrics_response.text)

    async def test_state_response_keeps_residency_snapshot(self):
        view = self.router.residency.views["p"]
        view.known, view.reason, view.epoch, view.sequence = True, "", "e1", 4
        view.block_size, view.resyncs = 16, 2
        view.groups = {"g0": ("full", 16, {b"a", b"b"})}

        response = await self.client.get("/narwhal/state")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["residency"], self.router.residency.snapshot())
        self.assertEqual(response.json()["residency"]["p"]["resident_blocks"], {"g0": 2})

    async def test_state_response_keeps_slo_met_and_peer_release_rounds(self):
        now = [100.0]
        self.router.peer_release = PeerRelease(lambda: now[0], load_backend("vllm").fabric)
        self.router.peer_release.track({"p": "ejected"})
        now[0] = 170.0
        self.assertEqual(self.router.peer_release.due(), ["p"])
        self.router.slo_met = 5

        response = await self.client.get("/narwhal/state")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), set(self.router.state()))
        self.assertEqual(body["slo_met"], 5)
        self.assertEqual(body["peer_release"], {"p": {"rounds": 1, "next_round_s": 55.0}})

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
            "recovery_decode_ratio": 0.5,
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
            "demand_horizon_s": 120.0,
            "steady_horizon_s": 15.0,
            "steady_prefill_work": 4.5,
            "steady_decode_work": 0.45,
            "steady_demand_s": 75.0,
            "departure_age_s": 25.0,
            "departure_reverses": True,
            "arrivals_beyond_profile": 3,
            "unsized_offers": 2,
        }
        self.router.scheduler.roles.record_decision(
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
        self.assertEqual(dashboard()["metadata"]["name"], "narwhal-router")
        response = await self.client.get("/metrics")
        required = {
            "tools/observability/grafana-narwhal.json": (
                "narwhal_failed_total",
                "narwhal_served_total",
                "narwhal_engine_lifecycle_state",
                "narwhal_slo_met_total",
                "narwhal_event_loop_busy_seconds_total",
            ),
            "tools/observability/prometheus-alerts.yml": (
                "narwhal_failed_total",
                "narwhal_pool_instances",
                "narwhal_unserved_total",
            ),
        }
        for relative, names in required.items():
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text()
                for name in names:
                    self.assertIn(name, text)
                    self.assertIn(name, response.text)

    async def test_dashboard_and_alert_queries_name_exported_metrics(self):
        response = await self.client.get("/metrics")
        exported = set()
        for name, kind in re.findall(r"^# TYPE (\S+) (\w+)$", response.text, re.M):
            exported.add(name)
            if kind == "histogram":
                exported.update(f"{name}_{suffix}" for suffix in ("bucket", "sum", "count"))
        expressions = [
            *observe_readiness._expressions(dashboard()["spec"]),
            *(rule["expr"] for rule in alert_rules().values()),
        ]
        queried = {name for expr in expressions for name in re.findall(r"\bnarwhal_\w+", expr)}
        self.assertLessEqual(queried, exported)
        # The backend's dashboard mapping is the single source for the engine series.
        series = set(load_backend("vllm").metrics.dashboard_series.values())
        readme = (ROOT / "tools/observability/README.md").read_text()
        engine = {name for expr in expressions for name in re.findall(r"vllm:\w+", expr)}
        self.assertEqual(engine, series)
        for name in series:
            self.assertIn(f"`{name}`", readme)

    def test_dashboard_charts_admission_queues_and_retries(self):
        scope = '{job="narwhal-router",instance=~"$router"}'
        expected = {
            "Queue depth": {
                "admission": "narwhal_queued",
                "prefill": "narwhal_waiting_prefill",
                "decode": "narwhal_waiting_decode",
            },
            "Admission in-flight": {
                "in-flight": "narwhal_admission_inflight",
                "limit": "narwhal_admission_inflight_limit",
            },
            "Retries and early exits": {
                "retry attempts": "narwhal_retry_attempts_total",
                "retries denied": "narwhal_retry_denied_total",
                "completed after retry": "narwhal_served_after_retry_total",
                "ended before sizing": "narwhal_unsized_offered_total",
            },
            "Retry credits": {"available": "narwhal_retry_credits"},
        }
        for title, series in expected.items():
            with self.subTest(title=title):
                panel, _ = titled(title)
                queries = legend_queries(panel)
                self.assertEqual(set(queries), set(series))
                for legend, metric in series.items():
                    self.assertIn(f"{metric}{scope}", queries[legend])
        queues, queue_grid = titled("Queue depth")
        colors = override_colors(queues)
        self.assertEqual(
            (colors["admission"], colors["prefill"], colors["decode"]),
            (
                DASHBOARD_PALETTE["router delay"],
                DASHBOARD_PALETTE["prefill"],
                DASHBOARD_PALETTE["decode"],
            ),
        )
        inflight, inflight_grid = titled("Admission in-flight")
        limit = next(
            override["properties"]
            for override in inflight["vizConfig"]["spec"]["fieldConfig"]["overrides"]
            if override["matcher"]["options"] == "limit"
        )
        self.assertIn(
            {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}, limit
        )
        self.assertEqual(override_colors(inflight)["limit"], DASHBOARD_PALETTE["warning"])
        # Queue wait is a quantile per stage, beside seat time.
        waiting, waiting_grid = titled("Request waiting time")
        waits = legend_queries(waiting)
        for stage, legend in (
            ("admission", "admission wait p95"),
            ("prefill", "prefill seat wait p95"),
            ("decode", "decode seat wait p95"),
        ):
            self.assertIn("narwhal_queue_wait_seconds_bucket", waits[legend])
            self.assertIn(f'stage="{stage}"', waits[legend])
        self.assertIn("narwhal_seat_seconds_bucket", waits["seat time p95"])
        # Admission and queue panels sit beside Pool pressure, retries beside waiting time.
        _, pressure_grid = titled("Pool pressure")
        _, retries_grid = titled("Retries and early exits")
        _, credits_grid = titled("Retry credits")
        for row in (
            (pressure_grid, inflight_grid, queue_grid),
            (waiting_grid, retries_grid, credits_grid),
        ):
            self.assertEqual({grid["y"] for grid in row}, {row[0]["y"]})
            self.assertEqual([grid["x"] for grid in row], [0, 8, 16])
        self.assertEqual(waiting_grid["y"], pressure_grid["y"] + pressure_grid["height"])

    def test_dashboard_attributes_drops_and_failed_attempts_by_reason(self):
        outcomes, outcome_grid = titled("Request outcomes")
        outcome_colors = override_colors(outcomes)
        dropped, dropped_grid = titled("Dropped requests by reason")
        queries = legend_queries(dropped)
        # Bar legend: (outcome, label, the outcome's series on Request outcomes).
        bars = {
            "refused: {{cause}}": ("refused", "cause", "refused (predictive)"),
            "rejected: {{reason}}": ("rejected", "reason", "rejected (capacity)"),
            "failed: {{reason}}": ("failed", "reason", "failed"),
            "expired: {{reason}}": ("expired", "reason", "expired"),
        }
        self.assertEqual(set(queries), set(bars))
        colors = override_colors(dropped)
        for legend, (outcome, label, series) in bars.items():
            with self.subTest(outcome=outcome):
                expr = queries[legend]
                self.assertTrue(
                    expr.startswith(
                        f"round(sum by({label}) (increase(narwhal_{outcome}_total"
                        '{job="narwhal-router",instance=~"$router"}[$__range]))) > 0'
                    ),
                    expr,
                )
                self.assertEqual(colors[f"^{outcome}: "], outcome_colors[series])
        for query in dropped["data"]["spec"]["queries"]:
            self.assertTrue(query["spec"]["query"]["spec"]["instant"])
        attempts, attempts_grid = titled("Failed attempts by reason")
        [expr] = panel_queries({"spec": attempts})
        self.assertIn("sum by(phase, reason) (increase(narwhal_attempt_failures_total{", expr)
        colors = override_colors(attempts)
        self.assertEqual(colors["^prefill: "], DASHBOARD_PALETTE["prefill"])
        self.assertEqual(colors["^decode: "], DASHBOARD_PALETTE["decode"])
        self.assertEqual(colors["^(admission|queue): "], DASHBOARD_PALETTE["router delay"])
        for panel in (dropped, attempts):
            self.assertEqual(panel["vizConfig"]["kind"], "bargauge")
        # Request, attempt and producer engine read left to right under Request outcomes.
        _, kv_grid = titled("Expired KV by producer")
        row = (dropped_grid, attempts_grid, kv_grid)
        self.assertEqual({grid["y"] for grid in row}, {outcome_grid["y"] + outcome_grid["height"]})
        self.assertEqual([grid["x"] for grid in row], [0, 8, 16])

    def test_dashboard_shows_expired_kv_per_producer_engine(self):
        panel, _ = titled("Expired KV by producer")
        self.assertEqual(
            legend_queries(panel),
            {
                "{{iid}}": 'sum by(iid) (rate(vllm:nixl_num_kv_expired_reqs_total{job="engines",'
                'iid=~"$iid"}[$__rate_interval]))'
            },
        )
        color = panel["vizConfig"]["spec"]["fieldConfig"]["defaults"]["color"]
        self.assertEqual(color, {"mode": "fixed", "fixedColor": DASHBOARD_PALETTE["severe"]})

    def test_router_event_loop_stays_the_last_row(self):
        spec = dashboard()["spec"]
        _, loop = titled("Router event loop")
        self.assertEqual(
            loop["y"], max(item["spec"]["y"] for item in spec["layout"]["spec"]["items"])
        )

    def test_warning_alerts_cover_rejected_expired_and_denied_retry_shares(self):
        rules = alert_rules()
        for name, numerator in (
            ("NarwhalRejectedRising", "sum without (reason) (rate(narwhal_rejected_total[5m]))"),
            ("NarwhalExpiredRising", "sum without (reason) (rate(narwhal_expired_total[5m]))"),
            ("NarwhalRetryDeniedRising", "rate(narwhal_retry_denied_total[5m])"),
        ):
            with self.subTest(alert=name):
                self.assertEqual(
                    rules[name],
                    {
                        "expr": f"{numerator} / rate(narwhal_offered_total[5m]) > 0.01",
                        "for": "5m",
                        "severity": "warn",
                    },
                )
        # Reason-labelled counters are summed so a threshold applies to the whole outcome.
        for name, rule in rules.items():
            for metric in re.findall(r"narwhal_(?:failed|rejected|expired)_total", rule["expr"]):
                with self.subTest(alert=name):
                    self.assertIn(f"sum without (reason) (rate({metric}[", rule["expr"])
        # The monitoring guide lists every shipped alert.
        guide = (ROOT / "docs/operate/02-Monitor.md").read_text()
        for name in rules:
            self.assertIn(f"| `{name}` |", guide)

    async def test_dashboard_scopes_latency_quantiles_to_the_selected_router(self):
        elements = dashboard()["spec"]["elements"]
        panels = {
            elements[name]["spec"]["title"]: elements[name]
            for name in ("panel-50", "panel-51", "panel-52")
        }
        self.assertEqual(
            set(panels),
            {"Time to first token", "Time per output token", "Request waiting time"},
        )
        for title, panel in panels.items():
            with self.subTest(title=title):
                expressions = panel_queries(panel)
                self.assertTrue(all('instance=~"$router"' in expr for expr in expressions))
                self.assertTrue(all("$iid" not in expr for expr in expressions))
                for expression in expressions:
                    if "histogram_quantile" in expression:
                        self.assertIn("sum by (instance, slo, le)", expression)
                        self.assertIn("[$__rate_interval]", expression)

        ttft_queries = panels["Time to first token"]["spec"]["data"]["spec"]["queries"]
        self.assertEqual(
            [query["spec"]["query"]["spec"]["legendFormat"] for query in ttft_queries],
            ["p50", "p95", "p99", "SLO"],
        )
        self.assertIn('metric="ttft"', panel_queries(panels["Time to first token"])[-1])
        self.assertIn('metric="tpot"', panel_queries(panels["Time per output token"])[-1])
        waiting = " ".join(panel_queries(panels["Request waiting time"]))
        self.assertIn("narwhal_queue_wait_seconds_bucket", waiting)
        self.assertIn("narwhal_seat_seconds_bucket", waiting)
        self.assertIn("narwhal_retry_attempts_total", " ".join(panel_queries(elements["panel-12"])))

    def test_dashboard_plots_event_loop_busy_share_and_lag(self):
        [panel] = [
            element["spec"]
            for element in dashboard()["spec"]["elements"].values()
            if element["spec"]["title"] == "Router event loop"
        ]
        queries = [query["spec"]["query"]["spec"] for query in panel["data"]["spec"]["queries"]]
        self.assertEqual([query["legendFormat"] for query in queries], ["busy", "lag"])
        scope = '{job="narwhal-router",instance=~"$router"}'
        self.assertIn(
            f"rate(narwhal_event_loop_busy_seconds_total{scope}[$__rate_interval])",
            queries[0]["expr"],
        )
        self.assertIn(f"narwhal_event_loop_lag_seconds{scope}", queries[1]["expr"])

    def test_engine_views_mark_held_and_unreachable_engines(self):
        elements = dashboard()["spec"]["elements"]
        table = elements["panel-7"]["spec"]
        for query in table["data"]["spec"]["queries"]:
            self.assertEqual(query["spec"]["query"]["spec"].get("format"), "table")
        state = next(
            query["spec"]["query"]["spec"]["expr"]
            for query in table["data"]["spec"]["queries"]
            if query["spec"]["refId"] == "S"
        )
        for source in (
            'up{job="engines"',
            'narwhal_engine_lifecycle_state{job="narwhal-router",instance=~"$router",iid=~"$iid",state="blocked"}',
            'state="validating"',
            "narwhal_ejected",
            "narwhal_engine_draining",
            "narwhal_engine_breaker_verifying",
            'narwhal_engine_quarantined{job="narwhal-router",instance=~"$router",iid=~"$iid"}',
            "narwhal_probation_instances",
            "vllm:num_requests_waiting",
            'narwhal_resident_requests{job="narwhal-router",instance=~"$router",iid=~"$iid",phase="prefill"}',
        ):
            self.assertIn(source, state)
        # An aggregated engine holds both roles and serves both phases.
        self.assertIn("unless on(iid) (count by(iid) (count by(iid, role) (", state)
        overrides = {
            override["matcher"]["options"]: override["properties"]
            for override in table["vizConfig"]["spec"]["fieldConfig"]["overrides"]
        }
        for column in ("Resident", "vLLM running"):
            self.assertIn({"id": "fieldMinMax", "value": True}, overrides[column])
        mappings = next(
            override["properties"][0]["value"][0]["options"]
            for override in table["vizConfig"]["spec"]["fieldConfig"]["overrides"]
            if override["matcher"]["options"] == "State"
        )
        self.assertEqual(
            [mappings[str(code)]["text"] for code in range(len(mappings))],
            [
                "Serving",
                "Switching",
                "Backlogged",
                "Probation",
                "Verifying",
                "Quarantined",
                "Draining",
                "Ejected",
                "Validating",
                "Blocked",
                "Unreachable",
                "Restarting",
            ],
        )
        quarantined = next(
            code for code, entry in mappings.items() if entry["text"] == "Quarantined"
        )
        self.assertIn(
            f"label_replace({quarantined} * max by(iid) (narwhal_engine_quarantined{{", state
        )
        history = elements["panel-8"]["spec"]
        expr = panel_queries(elements["panel-8"])[0]
        for source in (
            "narwhal_engine_quarantined",
            "narwhal_engine_draining",
            "narwhal_ejected",
            'state="validating"',
            'state="blocked"',
            'up{job="engines"',
        ):
            self.assertIn(source, expr)
        roles = history["vizConfig"]["spec"]["fieldConfig"]["defaults"]["mappings"][0]["options"]
        table_colors = {
            name: entry["color"]
            for name, entry in ((entry["text"], entry) for entry in mappings.values())
        }
        held = (
            "Quarantined",
            "Draining",
            "Ejected",
            "Validating",
            "Blocked",
            "Unreachable",
            "Restarting",
        )
        for code, name in enumerate(held, start=4):
            self.assertEqual(roles[str(code)]["text"], name)
            self.assertEqual(roles[str(code)]["color"], table_colors[name])
        self.assertIn("label_replace(4 * max by(iid) (narwhal_engine_quarantined{", expr)

    def test_headline_row_reports_requests_and_latency_against_the_slo(self):
        spec = dashboard()["spec"]
        elements = spec["elements"]
        row = sorted(
            (item["spec"]["x"], item["spec"]["element"]["name"])
            for item in spec["layout"]["spec"]["items"]
            if item["spec"]["y"] == 4
        )
        titles = [elements[name]["spec"]["title"] for _, name in row]
        self.assertEqual(titles, ["Requests", "Latency", "Router"])

        def columns(name):
            panel = elements[name]["spec"]
            self.assertEqual(panel["vizConfig"]["kind"], "table")
            organize = next(
                step["spec"]["options"]
                for step in panel["data"]["spec"]["transformations"]
                if step["kind"] == "organize"
            )
            names = organize["renameByName"]
            order = sorted(organize["indexByName"], key=organize["indexByName"].get)
            exprs = {
                names[f"Value #{query['spec']['refId']}"]: query["spec"]["query"]["spec"]["expr"]
                for query in panel["data"]["spec"]["queries"]
            }
            overrides = {
                o["matcher"]["options"]: {prop["id"]: prop["value"] for prop in o["properties"]}
                for o in panel["vizConfig"]["spec"]["fieldConfig"]["overrides"]
            }
            return [names[field] for field in order], exprs, overrides

        request_columns, requests, request_styles = columns(row[0][1])
        latency_columns, latency, latency_styles = columns(row[1][1])
        self.assertEqual(
            request_columns,
            [
                "Within SLO",
                "Offered",
                "Completed",
                "Cancelled",
                "Dropped",
                "Refused",
                "Rejected",
                "Failed",
                "Expired",
            ],
        )
        self.assertEqual(latency_columns, ["TTFT / SLO", "TPOT / SLO", "TTFT p95", "TPOT p95"])
        met, ended = requests["Within SLO"].split(" / ", 1)
        self.assertIn("narwhal_slo_met_total", met)
        missed, dropped_ended = requests["Dropped"].split(" / ", 1)
        for outcome in ("refused", "rejected", "failed", "expired"):
            self.assertIn(f"narwhal_{outcome}_total", missed)
        for total in (ended, dropped_ended):
            for outcome in ("served", "failed", "refused", "rejected", "expired", "cancelled"):
                self.assertIn(f"narwhal_{outcome}_total", total)
            self.assertNotIn("narwhal_offered_total", total)
        counts = {
            "Offered": "offered",
            "Completed": "served",
            "Cancelled": "cancelled",
            "Refused": "refused",
            "Rejected": "rejected",
            "Failed": "failed",
            "Expired": "expired",
        }
        for name, metric in counts.items():
            self.assertTrue(
                requests[name].startswith(f"round(sum(increase(narwhal_{metric}_total{{"),
                requests[name],
            )
        # Every column summarises the displayed interval.
        for expr in [*requests.values(), *latency.values()]:
            self.assertIn("[$__range]", expr)
            self.assertNotIn("$__rate_interval", expr)
        for metric in ("ttft", "tpot"):
            share = latency[f"{metric.upper()} / SLO"]
            self.assertIn(f"narwhal_{metric}_seconds_bucket", share)
            self.assertIn(f'metric="{metric}"', share)
        self.assertTrue(latency["TPOT p95"].endswith(" * 1000"))
        # Each router configuration's p95 is divided by that configuration's SLO.
        for metric in ("ttft", "tpot"):
            share = latency[f"{metric.upper()} / SLO"]
            self.assertIn("sum by (instance, slo, le)", share)
            self.assertIn("/ on(instance, slo)", share)
            self.assertIn("max_over_time(narwhal_slo_seconds{", share)
        # Status values colour their cells, as the engine table colours Role and State.
        for styles, name in (
            (request_styles, "Within SLO"),
            (request_styles, "Dropped"),
            (latency_styles, "TTFT / SLO"),
            (latency_styles, "TPOT / SLO"),
        ):
            self.assertEqual(styles[name]["unit"], "percentunit")
            self.assertEqual(styles[name]["custom.cellOptions"]["type"], "color-background")
        self.assertEqual(
            (latency_styles["TTFT p95"]["unit"], latency_styles["TTFT p95"]["decimals"]),
            ("suffix: s", 1),
        )
        self.assertEqual(
            (latency_styles["TPOT p95"]["unit"], latency_styles["TPOT p95"]["decimals"]),
            ("suffix: ms", 0),
        )

    def test_prompt_token_rates_exclude_kv_transfer(self):
        elements = dashboard()["spec"]["elements"]
        throughput = next(
            query["spec"]["query"]["spec"]["expr"]
            for query in elements["panel-49"]["spec"]["data"]["spec"]["queries"]
            if query["spec"]["query"]["spec"]["legendFormat"] == "Prefill/s"
        )
        tokens = next(
            query["spec"]["query"]["spec"]["expr"]
            for query in elements["panel-7"]["spec"]["data"]["spec"]["queries"]
            if query["spec"]["refId"] == "T"
        )
        for expr in (throughput, tokens):
            local, _ = expr.split(" or sum by(iid) (rate(vllm:prompt_tokens_total{", 1)
            self.assertIn('source=~"local_compute|local_cache_hit"', local)
            self.assertNotIn("external_kv_transfer", expr)
        decode, prefill = tokens.split(" or ((", 1)
        self.assertIn("vllm:generation_tokens_total", decode)
        self.assertIn('role="decode"', decode)
        self.assertIn('role="prefill"', prefill)

    def test_alert_history_timeline_and_outcome_markers(self):
        spec = dashboard()["spec"]
        events = spec["elements"]["panel-37"]["spec"]
        self.assertEqual(events["vizConfig"]["kind"], "state-timeline")
        query = events["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
        self.assertFalse(query.get("instant"))
        self.assertIn('severity="page"', query["expr"])
        self.assertIn('severity="warn"', query["expr"])
        outcomes = spec["elements"]["panel-11"]["spec"]["id"]
        # Fleet events sits beside Request outcomes at a quarter of the row, and the
        # role history sits beside the engine table.
        grid = {
            item["spec"]["element"]["name"]: item["spec"]
            for item in spec["layout"]["spec"]["items"]
        }
        self.assertEqual(grid["panel-37"]["y"], grid["panel-11"]["y"])
        self.assertEqual(
            (grid["panel-11"]["x"], grid["panel-11"]["width"], grid["panel-37"]["x"]), (0, 18, 18)
        )
        self.assertEqual(grid["panel-37"]["width"], 6)
        self.assertEqual(grid["panel-8"]["y"], grid["panel-7"]["y"])
        self.assertEqual(
            (grid["panel-7"]["x"], grid["panel-7"]["width"], grid["panel-8"]["x"]), (0, 16, 16)
        )
        self.assertEqual(grid["panel-8"]["width"], 8)
        table = spec["elements"]["panel-7"]["spec"]["vizConfig"]["spec"]["options"]
        self.assertEqual(table["sortBy"], [{"displayName": "Engine", "desc": False}])
        markers = {
            annotation["spec"]["name"]: annotation["spec"]
            for annotation in spec["annotations"]
            if annotation["spec"]["name"].startswith("Narwhal")
        }
        self.assertEqual(set(markers), {"Narwhal warnings", "Narwhal pages"})
        for name, severity in (("Narwhal warnings", "warn"), ("Narwhal pages", "page")):
            expr = markers[name]["query"]["spec"]["expr"]
            self.assertEqual(markers[name]["filter"], {"exclude": False, "ids": [outcomes]})
            self.assertIn(f'severity="{severity}"', expr)
            self.assertIn("unless", expr)

    def test_request_outcomes_plot_every_terminal_counter_from_zero(self):
        panel = dashboard()["spec"]["elements"]["panel-11"]["spec"]
        expressions = {
            query["spec"]["query"]["spec"]["legendFormat"]: query["spec"]["query"]["spec"]["expr"]
            for query in panel["data"]["spec"]["queries"]
        }
        terminals = {
            re.search(r"(narwhal_\w+_total)", expr).group(1)
            for legend, expr in expressions.items()
            if legend != "offered"
        }
        self.assertEqual(
            terminals,
            {
                f"narwhal_{name}_total"
                for name in (
                    "served",
                    "failed",
                    "refused",
                    "rejected",
                    "expired",
                    "cancelled",
                    "invalid_requests",
                )
            },
        )
        self.assertIn("narwhal_offered_total", expressions["offered"])
        self.assertTrue(all(expr.startswith("sum(rate(") for expr in expressions.values()))
        field_config = panel["vizConfig"]["spec"]["fieldConfig"]
        stacking = field_config["defaults"]["custom"].get("stacking", {"mode": "none"})
        self.assertEqual(stacking["mode"], "none")
        self.assertFalse(
            [
                prop
                for override in field_config["overrides"]
                for prop in override["properties"]
                if prop["id"] == "custom.stacking"
            ]
        )

    def test_dashboard_colors_come_from_the_palette(self):
        spec = dashboard()["spec"]
        allowed = set(DASHBOARD_PALETTE.values()) | {"text", "transparent"}

        def colors(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    named = key in {"color", "fixedColor", "iconColor"}
                    scheme = key == "mode" and "palette" in str(child)
                    if isinstance(child, str) and (named or scheme):
                        yield child
                    else:
                        yield from colors(child)
            elif isinstance(value, list):
                for child in value:
                    yield from colors(child)

        annotations = [a for a in spec["annotations"] if not a["spec"].get("builtIn")]
        panels = [
            element["spec"]
            for element in spec["elements"].values()
            if element["spec"]["vizConfig"]["kind"] != "text"
        ]
        self.assertLessEqual(set(colors([annotations, panels])), allowed)
        for panel in panels:
            if panel["vizConfig"]["kind"] != "timeseries":
                continue
            colored = {
                override["matcher"]["options"]
                for override in panel["vizConfig"]["spec"]["fieldConfig"]["overrides"]
                if any(prop["id"] == "color" for prop in override["properties"])
            }
            legends = {
                query["spec"]["query"]["spec"]["legendFormat"]
                for query in panel["data"]["spec"]["queries"]
            }
            literal = {legend for legend in legends if "{{" not in legend}
            with self.subTest(panel=panel["title"]):
                self.assertLessEqual(literal, colored)
                if "{{role}}" in legends:
                    self.assertLessEqual({"prefill", "decode"}, colored)

    async def test_flip_metrics_outlive_history_and_reset_with_scheduler(self):
        scheduler = self.router.scheduler
        scheduler.roles._flip_history = 2
        self.router.monitor.add(Instance("p2", "http://prefill2", Role.PREFILL))
        self.router.monitor.add(Instance("d2", "http://decode2", Role.DECODE))
        engine = self.router.monitor.instances["p"]
        engine.prefill["r1"] = Request("r1", 10)
        engine.decode["r2"] = Request("r2", 20)
        engine.decode["r3"] = Request("r3", 30)
        for index in range(12):
            target = Role.DECODE if index % 2 == 0 else Role.PREFILL
            self.assertIs(
                scheduler.roles.flip(
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
        self.assertEqual(len(scheduler.roles.flips), 2)
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
        self.assertNotIn('by="test:controller"', response.text)
        self.assertIn('narwhal_flips_total{to="decode",by="reactive"} 0\n', response.text)
        self.assertIn('narwhal_flip_inflight_total{phase="decode"} 0\n', response.text)

    async def test_flip_refusals_outlive_the_twenty_record_state_window(self):
        for count in range(1, 31):
            # The source floor refuses moving the only decode engine.
            self.assertIsNone(self.router.scheduler.roles.flip(Role.PREFILL))
            response = await self.client.get("/metrics")
            self.assertIn(f"narwhal_flips_refused_total {count}\n", response.text)
        state = (await self.client.get("/narwhal/state")).json()
        self.assertEqual(len(state["flips_refused"]), 20)
        self.assertEqual(state["control"]["flips_refused"], 30)
        self.router.scheduler.roles.flips_refused.clear()
        response = await self.client.get("/metrics")
        self.assertIn("narwhal_flips_refused_total 30\n", response.text)

    async def test_each_refusal_path_counts_once(self):
        self.router.monitor.add(Instance("p2", "http://prefill2", Role.PREFILL))
        self.router.monitor.add(Instance("d2", "http://decode2", Role.DECODE))
        scheduler = self.router.scheduler
        # Opening cooldown.
        self.assertIsNone(scheduler.roles.flip(Role.DECODE))
        self.assertEqual(scheduler.roles.control_snapshot()["flips_refused"], 1)
        # flip() counts the nested decode-floor refusal once.
        scheduler.min_decode = 2
        self.assertIsNone(scheduler.roles.flip(Role.PREFILL))
        self.assertEqual(scheduler.roles.control_snapshot()["flips_refused"], 2)
        scheduler.min_decode = 1
        scheduler.pinned = frozenset({"d", "d2"})
        self.assertIsNone(scheduler.roles.flip(Role.PREFILL))
        self.assertEqual(scheduler.roles.control_snapshot()["flips_refused"], 3)
        scheduler.pinned = frozenset()
        scheduler.th.dwell_s = 60
        scheduler.roles._last_flip = {iid: scheduler._clock() for iid in ("d", "d2")}
        self.assertIsNone(scheduler.roles.flip(Role.PREFILL))
        self.assertEqual(scheduler.roles.control_snapshot()["flips_refused"], 4)
        scheduler.advisory = True
        self.assertIsNone(scheduler.roles.flip(Role.PREFILL, bypass_dwell=True))
        state = scheduler.roles.control_snapshot()
        self.assertEqual(state["flips_refused"], 5)
        self.assertEqual(state["flips"], {})
        self.assertEqual(state["last_decision"]["result"], "advisory")

    async def test_haproxy_health_route_remains_available(self):
        config = (ROOT / "deploy/ha/haproxy.cfg").read_text()
        path = re.search(r"http-check send meth GET uri (\S+)", config)[1]
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
