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
from narwhal.runtime.lifecycle import leave
from tests.runtime.test_join import NEW, JoinHarness

ROOT = Path(__file__).resolve().parents[2]
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


class MembershipTelemetryTests(JoinHarness):
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


class MembershipTelemetryContractTests(unittest.TestCase):
    def test_the_journal_page_documents_every_lifecycle_action(self):
        text = (ROOT / "docs/telemetry/01-Journal.md").read_text()
        section = text.split("### Engine lifecycle events", 1)[1].split("\n## ", 1)[0]
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))
        self.assertEqual(emitted_actions() - documented, set())
        for action in ("join_started", "joined", "join_refused", "leave_started", "left"):
            row = re.search(rf"^\| `{action}` \|.*$", section, re.M)
            self.assertIn("`generation`", row[0])

    def test_the_engine_variable_follows_the_router_scrape_targets(self):
        dashboard = json.loads((ROOT / "tools/observability/grafana-narwhal.json").read_text())
        variable = next(v for v in dashboard["spec"]["variables"] if v["spec"]["name"] == "iid")
        self.assertEqual(variable["spec"]["definition"], 'label_values(up{job="engines"}, iid)')
        self.assertNotEqual(variable["spec"]["refresh"], "never")
        config = (ROOT / "tools/observability/prometheus.yml").read_text()
        job = config.split("- job_name: engines", 1)[1].split("- job_name:", 1)[0]
        self.assertIn("http_sd_configs:", job)
        self.assertIn("/narwhal/targets", job)
