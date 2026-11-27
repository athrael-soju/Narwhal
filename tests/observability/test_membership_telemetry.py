"""Check that journal events, metrics and scrape targets follow a join and a leave."""

import ast
import json
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.contracts import JOURNAL, current
from narwhal.observability.journal import RunJournal
from narwhal.observability.stack.artifacts import render_dashboard
from narwhal.runtime.lifecycle import leave
from narwhal.runtime.lifecycle.records import ValidationOutcome
from narwhal.types import Request
from tests.runtime.test_join import NEW, JoinHarness

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "src/narwhal/observability/stack/grafana-narwhal.json"
NEW_ROW = {
    "iid": NEW.iid,
    "url": NEW.url,
    "role": NEW.role.value,
    "attestation_url": NEW.attestation_url,
}
PER_ENGINE = (
    "narwhal_engine_requests_total",
    "narwhal_engine_seats",
    "narwhal_instance_role",
    "narwhal_resident_requests",
)


def emitted_actions() -> set[str]:
    """Return every action the lifecycle package writes as an `engine_lifecycle` event."""
    actions = set()
    for path in (ROOT / "src/narwhal/runtime/lifecycle").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "_emit"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                actions.add(node.args[0].value)
    return actions


class MembershipCase(JoinHarness):
    """A router that journals to a file, with identity capture for leaves patched."""

    def setUp(self):
        super().setUp()
        self.journal_path = Path(self.cfg.state_path).parent / "journal.jsonl"
        self.router.journal = RunJournal(self.journal_path)
        self.router.journal.open()
        self.addCleanup(self.router.journal.close)
        capture = AsyncMock(
            side_effect=lambda cfg, engines, **kwargs: (dict.fromkeys(engines, 100.0), {})
        )
        patcher = patch.object(leave, "capture_process_identities", capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://router"
        )
        self.addAsyncCleanup(self.client.aclose)

    def membership_rows(self):
        rows = [json.loads(line) for line in self.journal_path.read_text().splitlines()]
        return [
            row
            for row in rows
            if row.get("event") == "engine_lifecycle"
            and row["action"] in {"join_started", "joined", "join_refused", "leave_started", "left"}
        ]

    async def series(self, iid):
        text = (await self.client.get("/metrics")).text
        return {line.split("{")[0] for line in text.splitlines() if f'iid="{iid}"' in line}

    async def targets(self):
        response = await self.client.get("/narwhal/targets")
        return [group["labels"]["iid"] for group in response.json()]


class MembershipTelemetryTests(MembershipCase):
    async def test_a_join_and_a_leave_write_events_and_move_series_and_targets(self):
        self.assertEqual(await self.series(NEW.iid), set())
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(NEW.iid, await self.targets())
        self.assertTrue(set(PER_ENGINE) <= await self.series(NEW.iid))
        health = (await self.client.get("/health")).json()
        self.assertEqual(health["instances"], len(self.cfg.engines) + 1)

        response = await self.client.delete(f"/narwhal/engines/{NEW.iid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(NEW.iid, await self.targets())
        self.assertEqual(await self.series(NEW.iid), set())
        health = (await self.client.get("/health")).json()
        self.assertEqual(health["instances"], len(self.cfg.engines))

        rows = self.membership_rows()
        self.assertEqual(
            [(row["action"], row["iid"], row["generation"]) for row in rows],
            [
                ("join_started", "e6", 0),
                ("joined", "e6", 1),
                ("leave_started", "e6", 1),
                ("left", "e6", 2),
            ],
        )
        for row in rows:
            self.assertEqual(
                (row["schema"], row["schema_version"]), ("narwhal.journal", current(JOURNAL))
            )
            self.assertIsInstance(row["at"], float)
        self.assertEqual(rows[1]["role"], "decode")
        self.assertEqual(rows[1]["process_start"], 200.0)

    async def test_every_series_of_a_removed_engine_stops(self):
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 200, response.text)
        iid = NEW.iid
        availability = self.router.scheduler.availability
        availability.ejections[(iid, "liveness")] += 1
        availability.hold_starts[(iid, "timed")] += 1
        availability.hold_ends[(iid, "timed", "expired")] += 1
        availability.readmissions[(iid, "probe")] += 1
        availability.failures["stream"][iid] = 1
        self.router.verifier.probes[(iid, "health", "ok")] += 1
        self.router.scheduler.health.note(iid, 1.0)
        self.router.monitor.sent(iid)
        filled = {
            *PER_ENGINE,
            "narwhal_engine_ejections_total",
            "narwhal_engine_hold_starts_total",
            "narwhal_engine_hold_ends_total",
            "narwhal_engine_readmissions_total",
            "narwhal_engine_probes_total",
            "narwhal_engine_breaker_streak",
            "narwhal_health_windows_scored_total",
        }
        self.assertEqual(filled - await self.series(iid), set())
        response = await self.client.delete(f"/narwhal/engines/{iid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.series(iid), set())

    async def test_a_refused_join_writes_its_reason_and_adds_no_series(self):
        self.starts["e6"] = 300.0
        response = await self.client.post(
            "/narwhal/engines", json={**NEW_ROW, "attestation_url": ""}
        )
        self.assertEqual(response.status_code, 409, response.text)
        rows = self.membership_rows()
        self.assertEqual([row["action"] for row in rows], ["join_refused"])
        self.assertEqual(rows[0]["generation"], 0)
        self.assertIn("attestation_url", rows[0]["error"])
        self.assertEqual(await self.series(NEW.iid), set())
        self.assertNotIn(NEW.iid, await self.targets())


class MembershipMetricTests(MembershipCase):
    async def membership_metrics(self):
        text = (await self.client.get("/metrics")).text
        names = (
            "narwhal_membership_generation",
            "narwhal_engine_joins_total",
            "narwhal_engine_leaves_total",
        )
        return {
            line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
            for line in text.splitlines()
            if line.startswith(names)
        }

    def expected(
        self, generation=0, joined=0, join_refused=0, left=0, leave_refused=0, cancelled=0
    ):
        return {
            "narwhal_membership_generation": generation,
            'narwhal_engine_joins_total{outcome="joined"}': joined,
            'narwhal_engine_joins_total{outcome="refused"}': join_refused,
            'narwhal_engine_leaves_total{outcome="left"}': left,
            'narwhal_engine_leaves_total{outcome="refused"}': leave_refused,
            'narwhal_engine_leaves_total{outcome="cancelled"}': cancelled,
        }

    async def test_every_outcome_reads_zero_at_start(self):
        self.assertEqual(await self.membership_metrics(), self.expected())
        state = (await self.client.get("/narwhal/state")).json()
        self.assertEqual(
            state["membership"],
            {
                "generation": 0,
                "joins": {"joined": 0, "refused": 0},
                "leaves": {"left": 0, "refused": 0, "cancelled": 0},
            },
        )

    async def test_a_join_and_a_leave_count_once_and_move_the_generation(self):
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.membership_metrics(), self.expected(generation=1, joined=1))
        response = await self.client.delete(f"/narwhal/engines/{NEW.iid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            await self.membership_metrics(), self.expected(generation=2, joined=1, left=1)
        )

    async def test_refused_joins_count_without_a_generation_change(self):
        # A precondition refusal, then a failed check.
        response = await self.client.post(
            "/narwhal/engines", json={**NEW_ROW, "attestation_url": ""}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.responses[(NEW.iid, "/v1/models")] = {"data": [{"id": "another-model"}]}
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.membership_metrics(), self.expected(join_refused=2))
        # An invalid entry and a router without fleet control count nothing.
        response = await self.client.post("/narwhal/engines", json={"iid": "bad iid"})
        self.assertEqual(response.status_code, 422, response.text)
        self.router.standby = True
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(await self.membership_metrics(), self.expected(join_refused=2))

    async def test_a_refused_leave_counts_and_an_unknown_engine_does_not(self):
        # Removing e3 would leave fewer engines than min_prefill + min_decode.
        response = await self.client.delete("/narwhal/engines/e3")
        self.assertEqual(response.status_code, 409, response.text)
        response = await self.client.delete("/narwhal/engines/e9")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(await self.membership_metrics(), self.expected(leave_refused=1))
        # A deadline that is not positive and finite is a 409 refusal too; a 422 is not.
        for deadline in ("0", "-1", "inf"):
            response = await self.client.delete(f"/narwhal/engines/e1?deadline_s={deadline}")
            self.assertEqual(response.status_code, 409, response.text)
        response = await self.client.delete("/narwhal/engines/e1?deadline_s=soon")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(await self.membership_metrics(), self.expected(leave_refused=4))

    async def test_readmission_cancels_a_leave(self):
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 200, response.text)
        self.router.monitor.dispatched(NEW.iid, Request("r", 64))
        response = await self.client.delete(f"/narwhal/engines/{NEW.iid}")
        self.assertEqual(response.status_code, 202, response.text)
        self.router.monitor.finished(NEW.iid, "r")
        self.manager.refresh()
        self.manager.mark_validating([NEW.iid])
        self.manager.readmitted([NEW.iid], ValidationOutcome(starts={NEW.iid: 200.0}))
        self.assertEqual(
            await self.membership_metrics(), self.expected(generation=1, joined=1, cancelled=1)
        )

    async def leaving(self):
        text = (await self.client.get("/metrics")).text
        return {
            line.split('"')[1]: line.rsplit(" ", 1)[1]
            for line in text.splitlines()
            if line.startswith("narwhal_engine_leaving{")
        }

    async def test_the_leaving_gauge_follows_a_removal_drain(self):
        response = await self.client.post("/narwhal/engines", json=NEW_ROW)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set((await self.leaving()).values()), {"0"})
        self.router.monitor.dispatched(NEW.iid, Request("r", 64))
        response = await self.client.delete(f"/narwhal/engines/{NEW.iid}")
        self.assertEqual(response.status_code, 202, response.text)
        gauges = await self.leaving()
        self.assertEqual(gauges.pop(NEW.iid), "1")
        self.assertEqual(set(gauges.values()), {"0"})
        # Readmission cancels the removal and the gauge returns to 0.
        self.router.monitor.finished(NEW.iid, "r")
        self.manager.refresh()
        self.manager.mark_validating([NEW.iid])
        self.manager.readmitted([NEW.iid], ValidationOutcome(starts={NEW.iid: 200.0}))
        self.assertEqual((await self.leaving())[NEW.iid], "0")


class MembershipTelemetryContractTests(unittest.TestCase):
    def test_the_journal_page_documents_every_lifecycle_action(self):
        text = (ROOT / "docs/telemetry/01-Journal.md").read_text()
        section = text.split("### Engine lifecycle events", 1)[1].split("\n## ", 1)[0]
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))
        self.assertEqual(emitted_actions() - documented, set())
        for action in ("join_started", "joined", "join_refused", "leave_started", "left"):
            row = re.search(rf"^\| `{action}` \|.*$", section, re.M)
            self.assertIn("`generation`", row[0])

    def test_the_dashboard_marks_each_membership_change_on_the_selected_router(self):
        spec = render_dashboard(json.loads(DASHBOARD.read_text()))["spec"]
        layer = next(
            a["spec"] for a in spec["annotations"] if a["spec"]["name"] == "Membership changes"
        )
        series = 'narwhal_membership_generation{job="narwhal-router",instance=~"$router"}'
        query = layer["query"]["spec"]
        ready = 'narwhal_router_ready{job="narwhal-router",instance=~"$router"}'
        self.assertEqual(
            query["expr"],
            f"({series} != ({series} offset $__interval)) and on(job, instance) "
            f"(({ready} offset $__interval) == 1) and on(job, instance) ({ready} == 1)",
        )
        self.assertEqual((query["step"], query["useValueForTime"]), ("15s", False))
        self.assertEqual(layer["legacyOptions"], query)
        self.assertEqual(layer["iconColor"], "#00BCD4")
        # The marker shows on every panel.
        self.assertNotIn("filter", layer)

    def test_the_joins_and_leaves_panel_sits_under_fleet_events(self):
        spec = render_dashboard(json.loads(DASHBOARD.read_text()))["spec"]
        panel = spec["elements"]["panel-60"]["spec"]
        self.assertEqual(panel["title"], "Engine joins and leaves")
        queries = [query["spec"]["query"]["spec"] for query in panel["data"]["spec"]["queries"]]
        self.assertTrue(all(query["instant"] for query in queries))
        counted = set()
        for query in queries:
            for kind, outcome in re.findall(
                r'narwhal_engine_(joins|leaves)_total\{job="narwhal-router",'
                r'instance=~"\$router",outcome="(\w+)"\}\[\$__range\]',
                query["expr"],
            ):
                counted.add((kind, outcome))
        self.assertEqual(
            counted,
            {
                ("joins", "joined"),
                ("joins", "refused"),
                ("leaves", "left"),
                ("leaves", "refused"),
                ("leaves", "cancelled"),
            },
        )
        refused = next(
            override
            for override in panel["vizConfig"]["spec"]["fieldConfig"]["overrides"]
            if override["matcher"]["options"] == "Refused"
        )
        steps = next(p["value"]["steps"] for p in refused["properties"] if p["id"] == "thresholds")
        self.assertEqual(steps[-1], {"color": "#F2CC0C", "value": 1})
        rows = {
            row["spec"]["title"]: row["spec"]["layout"]["spec"]["items"]
            for row in spec["layout"]["spec"]["rows"]
        }
        grid = {
            item["spec"]["element"]["name"]: item["spec"]
            for item in rows["Request outcomes and fleet events"]
        }
        events, changes = grid["panel-37"], grid["panel-60"]
        self.assertEqual((changes["x"], changes["width"]), (events["x"], events["width"]))
        self.assertEqual(changes["y"], events["y"] + events["height"])
        self.assertEqual(
            changes["y"] + changes["height"], grid["panel-11"]["y"] + grid["panel-11"]["height"]
        )

    def test_the_engine_variable_follows_the_router_scrape_targets(self):
        path = ROOT / "src/narwhal/observability/stack/grafana-narwhal.json"
        dashboard = json.loads(path.read_text())
        variable = next(v for v in dashboard["spec"]["variables"] if v["spec"]["name"] == "iid")
        query = 'label_values(up{job="engines"}, iid)'
        self.assertEqual(variable["spec"]["query"]["spec"]["query"], query)
        self.assertEqual(variable["spec"]["definition"], query)
        self.assertNotEqual(variable["spec"]["refresh"], "never")
        config = (ROOT / "src/narwhal/observability/stack/prometheus.yml").read_text()
        job = config.split("- job_name: engines", 1)[1].split("- job_name:", 1)[0]
        self.assertIn("http_sd_configs:", job)
        self.assertIn("/narwhal/targets", job)
