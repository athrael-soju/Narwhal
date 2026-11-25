"""Baseline engines outside the router's membership: their probes, metric and one-click join."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import httpx

from tests.fleet_control.test_engine_actions import TOKEN, EngineCase, FakeRouter
from tests.fleet_control.test_membership_actions import MemberRouter
from tools.fleet_control import engines as engines_module
from tools.fleet_control.app import create_app
from tools.fleet_control.engines import EngineActions
from tools.fleet_control.signals import FleetSignals, metrics, signal_routes
from tools.observability.artifacts import render_dashboard

GAUGE = "narwhal_control_engine_available"
STARTING = "narwhal_control_engine_starting"


class AvailableRouter(MemberRouter):
    """Answer each engine's health route and attestation sidecar as well as the router's.

    Engines in `running` answer `/health`, and engines in `sidecars` answer their
    attestation URL. Any other engine request is refused.
    """

    def __init__(self, stopped: Path) -> None:
        super().__init__(stopped)
        self.running = set(self.members)
        self.sidecars = set(self.members)
        self.probes: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host.startswith("engine-"):
            return super().handle(request)
        iid = "e" + host.split(".", 1)[0].removeprefix("engine-")
        self.probes.append(f"{iid} {request.url.path}")
        if request.url.path == "/health" and iid in self.running:
            return httpx.Response(200)
        if request.url.path == "/v1/attestation" and iid in self.sidecars:
            return httpx.Response(200, json={})
        raise httpx.ConnectError("connection refused", request=request)


def engine(
    iid: str,
    *,
    up: bool = True,
    sidecar: bool = True,
    error: str | None = None,
    starting: bool = False,
) -> dict:
    return {
        "iid": iid,
        "backend": "vllm",
        "engine": up,
        "sidecar": sidecar,
        "available": up and sidecar,
        "starting": starting,
        "error": error,
    }


class AvailableCase(EngineCase):
    router_class: ClassVar[type[FakeRouter]] = AvailableRouter
    router: AvailableRouter

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Every read probes again unless a case restores the cache.
        patcher = mock.patch.object(engines_module, "AVAILABILITY_TTL_S", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        actions = EngineActions(self.service, httpx.MockTransport(self.router.handle), 0.02)
        signals = FleetSignals(self.service, engines=actions)
        app = create_app(self.service, TOKEN, routers=[signal_routes(signals)])
        self.metrics_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://control",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.metrics_client.aclose)

    async def available(self) -> Any:
        response = await self.client.get("/api/engines")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["available"]

    async def gauge(self) -> list[str]:
        text = (await self.metrics_client.get("/metrics")).text
        self.assertIn(f"# TYPE {GAUGE} gauge", text)
        return [line for line in text.splitlines() if line.startswith(GAUGE + "{")]

    async def leave(self, iid: str) -> None:
        response = await self.client.post(f"/api/engines/{iid}/leave")
        self.assertEqual(response.status_code, 200, response.text)


class AvailabilityTests(AvailableCase):
    async def test_members_are_not_listed(self) -> None:
        self.assertEqual(await self.available(), {"engines": [], "error": None})
        self.assertEqual(await self.gauge(), [])

    async def test_a_departed_engine_that_still_runs_is_available(self) -> None:
        await self.start_session()
        await self.leave("e1")
        self.assertEqual(await self.available(), {"engines": [engine("e1")], "error": None})
        self.assertEqual(await self.gauge(), [f'{GAUGE}{{iid="e1",backend="vllm"}} 1'])

    async def test_a_stopped_engine_or_sidecar_reads_zero(self) -> None:
        await self.start_session()
        await self.leave("e1")
        await self.leave("e2")
        self.router.running.discard("e1")
        self.router.sidecars.discard("e2")
        self.assertEqual(
            (await self.available())["engines"],
            [engine("e1", up=False), engine("e2", sidecar=False)],
        )
        self.assertEqual(
            await self.gauge(),
            [f'{GAUGE}{{iid="e1",backend="vllm"}} 0', f'{GAUGE}{{iid="e2",backend="vllm"}} 0'],
        )

    async def test_an_endpoint_that_does_not_resolve_reads_zero_with_its_reason(self) -> None:
        await self.start_session()
        assert self.service.session is not None
        self.service.session.baseline["engines"][1]["url"] = "${NARWHAL_TEST_E1_URL}"
        self.router._remove("e1")
        [found] = (await self.available())["engines"]
        self.assertFalse(found["available"])
        self.assertIn("does not resolve", found["error"])
        self.assertEqual(await self.gauge(), [f'{GAUGE}{{iid="e1",backend="vllm"}} 0'])

    async def test_an_unreadable_membership_lists_nothing_and_names_the_reason(self) -> None:
        self.router._remove("e1")
        self.router.membership_status = 500
        available = await self.available()
        self.assertEqual(available["engines"], [])
        self.assertIn("/narwhal/engines", available["error"])
        self.assertEqual(await self.gauge(), [])

    async def test_a_reading_serves_until_an_engine_action(self) -> None:
        with mock.patch.object(engines_module, "AVAILABILITY_TTL_S", 60.0):
            await self.start_session()
            await self.leave("e1")
            await self.available()
            await self.available()
            self.assertEqual(self.router.probes.count("e1 /health"), 1)
            response = await self.client.post("/api/engines/e0/drain")
            self.assertEqual(response.status_code, 200, response.text)
            await self.available()
            self.assertEqual(self.router.probes.count("e1 /health"), 2)

    async def test_concurrent_readers_share_one_probe(self) -> None:
        with mock.patch.object(engines_module, "AVAILABILITY_TTL_S", 60.0):
            self.router._remove("e1")
            await asyncio.gather(self.available(), self.gauge(), self.available())
            # The control app and the metrics app each hold one reading.
            self.assertEqual(self.router.probes.count("e1 /health"), 2)


class StartingTests(AvailableCase):
    """An engine fleet control started reads Starting until it responds or the router timeout."""

    async def start_without_response(self) -> None:
        """Leave and stop e1, then restore: the start hook runs and the router refuses the join."""
        await self.start_session()
        await self.leave("e1")
        self.router.running.discard("e1")
        self.router.refuse_join.add("e1")
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 409, response.text)
        restore = next(
            entry for entry in self.record()["actions"] if entry["action"] == "config.restore"
        )
        self.assertEqual([step["action"] for step in restore["result"]["steps"]], ["start"])

    async def starting_gauge(self) -> list[str]:
        text = metrics(self.service, await self.actions.available())
        return [line for line in text.splitlines() if line.startswith(STARTING + "{")]

    async def test_a_started_engine_reads_starting_until_it_responds(self) -> None:
        await self.start_without_response()
        self.assertEqual(
            (await self.available())["engines"], [engine("e1", up=False, starting=True)]
        )
        self.assertEqual(await self.starting_gauge(), [f'{STARTING}{{iid="e1",backend="vllm"}} 1'])
        self.router.running.add("e1")
        self.assertEqual((await self.available())["engines"], [engine("e1")])
        self.assertEqual(await self.starting_gauge(), [f'{STARTING}{{iid="e1",backend="vllm"}} 0'])
        # Once it has responded, an engine that stops again is unavailable.
        self.router.running.discard("e1")
        self.assertEqual((await self.available())["engines"], [engine("e1", up=False)])

    async def test_a_started_engine_is_unavailable_after_the_router_timeout(self) -> None:
        await self.start_without_response()
        timeout = self.service.config.router.timeout_s
        self.actions._started["e1"] = time.monotonic() - timeout
        self.assertEqual((await self.available())["engines"], [engine("e1", up=False)])

    async def test_an_engine_fleet_control_did_not_start_is_unavailable(self) -> None:
        await self.start_session()
        await self.leave("e1")
        self.router.running.discard("e1")
        self.assertEqual((await self.available())["engines"], [engine("e1", up=False)])
        self.assertEqual(await self.starting_gauge(), [f'{STARTING}{{iid="e1",backend="vllm"}} 0'])


class NoMembershipRouteTests(EngineCase):
    async def test_a_router_without_the_membership_route_lists_no_available_engines(self) -> None:
        response = await self.client.get("/api/engines")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["available"])


class BaselineJoinTests(AvailableCase):
    async def test_join_sends_the_baseline_entry_as_an_exclusive_action(self) -> None:
        await self.start_session()
        await self.leave("e1")
        seen = []
        self.router.on_change = lambda: seen.append(self.service.status()["in_progress"])
        response = await self.client.post("/api/engines/e1/join")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen, ["engine.join"])
        assert self.service.session is not None
        baseline = self.service.session.baseline["engines"][1]
        posts = [body for method, path, body in self.router.requests if method == "POST"]
        self.assertEqual(posts[-1], baseline)
        entry = self.last_action()
        self.assertEqual(entry["action"], "engine.join")
        self.assertEqual(entry["params"], {"engine": "e1", "baseline": True})
        self.assertEqual(entry["result"]["router"]["status"], 200)
        self.assertEqual(await self.available(), {"engines": [], "error": None})
        changes = await self.service.changes(self.service.session)
        self.assertNotIn("left", changes)
        self.assertNotIn("joined", changes)

    async def test_join_resolves_baseline_references_from_the_service_environment(self) -> None:
        await self.start_session()
        assert self.service.session is not None
        self.service.session.baseline["engines"][1]["url"] = "${NARWHAL_TEST_E1_URL}"
        self.router._remove("e1")
        response = await self.client.post("/api/engines/e1/join")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("does not resolve", response.json()["detail"])
        self.assertNotIn("e1", self.router.members)
        self.service._env = {**self.service._env, "NARWHAL_TEST_E1_URL": "http://engine-1.test:9"}
        response = await self.client.post("/api/engines/e1/join")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.router.members["e1"]["url"], "http://engine-1.test:9")

    async def test_join_follows_the_session_exclusive_and_baseline_rules(self) -> None:
        self.router._remove("e1")
        response = await self.client.post("/api/engines/e1/join")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("no session is active", response.json()["detail"])
        await self.start_session()
        response = await self.client.post("/api/engines/e9/join")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("not in the baseline", response.json()["detail"])
        self.service._exclusive = "engine.leave"
        try:
            response = await self.client.post("/api/engines/e1/join")
        finally:
            self.service._exclusive = None
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "engine.leave is in progress")
        self.assertEqual(self.last_action()["outcome"], "refused")

    async def test_a_refused_join_relays_the_router_status(self) -> None:
        await self.start_session()
        await self.leave("e1")
        self.router.refuse_join.add("e1")
        response = await self.client.post("/api/engines/e1/join")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("attestation differs", response.json()["detail"])
        self.assertEqual((await self.available())["engines"], [engine("e1")])


PAGE = Path(__file__).resolve().parents[2] / "tools/fleet_control/console.html"


def page_function(name: str) -> str:
    """Return the source of one top-level function of the console page."""
    page = PAGE.read_text(encoding="utf-8")
    match = re.search(rf"^function {name}\(.*?^\}}$", page, re.M | re.S)
    assert match is not None, name
    return match.group(0)


class JoinGateTests(unittest.TestCase):
    """The console disables an available engine's Join and names the reason."""

    def reasons(self, cases: list[tuple[dict[str, Any], str, str]]) -> list[str]:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")
        script = (
            page_function("availableGate")
            + "\nconst cases = JSON.parse(process.argv[1]);"
            + "\nconsole.log(JSON.stringify(cases.map((c) => availableGate(...c))));"
        )
        result = subprocess.run(
            [node, "-e", script, json.dumps(cases)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return list(json.loads(result.stdout))

    def test_each_disabled_state_names_its_reason(self) -> None:
        up = engine("e1")
        unresolved = {**engine("e1", up=False, sidecar=False), "error": "url does not resolve"}
        failed = "router /narwhal/engines failed"
        cases = [
            (up, "", ""),
            (engine("e1", up=False), "", ""),
            (engine("e1", sidecar=False), "", ""),
            (engine("e1", up=False, sidecar=False), "", ""),
            (unresolved, "", ""),
            (engine("e1", up=False, starting=True), "", ""),
            (up, failed, ""),
            (up, "", "engine.leave is in progress."),
            (engine("e1", up=False), failed, "Start a session first."),
        ]
        self.assertEqual(
            self.reasons(cases),
            [
                "",
                "The engine does not respond.",
                "The attestation sidecar does not respond.",
                "The engine and its attestation sidecar do not respond.",
                "url does not resolve",
                "The engine is starting.",
                f"The router cannot be read: {failed}",
                "engine.leave is in progress.",
                "Start a session first.",
            ],
        )

    def test_a_refused_join_shows_the_routers_reason_verbatim(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")
        detail = (
            "router refused join of e1 with HTTP 409: stale profiles for engines outside the "
            "proposed set: e4"
        )
        script = (
            'const OUTCOME_TONES = { ok: "ok", refused: "orange", failed: "bad" };\n'
            + page_function("actionEntry")
            + "\n"
            + page_function("outcomeOf")
            + "\nconsole.log(JSON.stringify(outcomeOf(JSON.parse(process.argv[1]))));"
        )
        reply = {"status": 409, "body": {"detail": detail}}
        result = subprocess.run(
            [node, "-e", script, json.dumps(reply)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self.assertEqual(json.loads(result.stdout), {"kind": "refused", "detail": detail})
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('act("join", "Join " + iid, "engineBaselineJoin", { iid })', page)
        self.assertIn('renderFeedback($("fb-join"), "join");', page)

    def test_the_session_gate_covers_exclusive_actions_and_running_joins(self) -> None:
        gate = page_function("busyGate")
        self.assertIn("state.health.in_progress", gate)
        self.assertIn('state.pending.has("join")', gate)
        render = page_function("renderAvailable")
        self.assertIn("availableGate(engine, routerError, gate)", render)
        self.assertIn("const gate = sessionGate();", render)
        self.assertIn("state.enginesError || listed.error", render)
        self.assertIn("setControl(button, reason)", render)


DASHBOARD = Path(__file__).resolve().parents[2] / "tools/observability/grafana-narwhal.json"


class AvailableEnginesPanelTests(unittest.TestCase):
    def test_the_panel_lists_fleet_controls_engines_in_grey_under_the_engine_table(self) -> None:
        spec = render_dashboard(json.loads(DASHBOARD.read_text()))["spec"]
        panel = spec["elements"]["panel-61"]["spec"]
        self.assertEqual(panel["title"], "Available engines")
        [query] = [q["spec"]["query"]["spec"] for q in panel["data"]["spec"]["queries"]]
        available = f'max by(iid, backend) ({GAUGE}{{job="fleet-control"}})'
        starting = f'max by(iid, backend) ({STARTING}{{job="fleet-control"}})'
        self.assertEqual(
            query["expr"], f"{available} + on(iid, backend) (2 * {starting} or 0 * {available})"
        )
        self.assertTrue(query["instant"])
        overrides = {
            override["matcher"]["options"]: override["properties"]
            for override in panel["vizConfig"]["spec"]["fieldConfig"]["overrides"]
        }
        [mapping] = next(
            p["value"] for p in overrides["Engine and sidecar"] if p["id"] == "mappings"
        )
        self.assertEqual(
            mapping["options"],
            {
                "0": {"text": "No response", "color": "#8E9196"},
                "1": {"text": "Respond", "color": "#8E9196"},
                "2": {"text": "Starting", "color": "#8E9196"},
            },
        )
        self.assertEqual(
            next(p["value"] for p in overrides["Backend"] if p["id"] == "custom.cellOptions"),
            {"type": "image"},
        )
        rows = {
            row["spec"]["title"]: {
                item["spec"]["element"]["name"]: item["spec"]
                for item in row["spec"]["layout"]["spec"]["items"]
            }
            for row in spec["layout"]["spec"]["rows"]
        }
        table, available = rows["Engines"]["panel-7"], rows["Engines"]["panel-61"]
        self.assertEqual((available["x"], available["width"]), (table["x"], table["width"]))
        self.assertEqual(available["y"], table["y"] + table["height"])
