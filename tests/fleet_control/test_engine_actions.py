"""Engine actions through hooks and the router lifecycle API, with state before and after."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.runtime.lifecycle.records import ValidationOutcome
from narwhal.serving import app as serving_app
from tests.fixtures import fleet
from tools.fleet_control.app import create_app
from tools.fleet_control.config import (
    DEFAULT_ROUTER_TIMEOUT_S,
    DEFAULT_ROUTER_URL,
    ConfigError,
    ControlConfig,
    Hook,
    RouterEndpoint,
    load_config,
)
from tools.fleet_control.engines import EngineActions, engine_routes
from tools.fleet_control.service import ActionError, ControlService

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "tests/data/fleet.json"
TOKEN = "fake-control-token-" + "0" * 24
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
ROUTER = "http://router.invalid:8000"

# Each hook marks or clears `<STOPPED>/<engine>` and prints what it received.
HOOK_SCRIPT = """
import os, pathlib
engine = os.environ["NARWHAL_CONTROL_ENGINE"]
action = os.environ["NARWHAL_CONTROL_ACTION"]
marker = pathlib.Path(os.environ["STOPPED"], engine)
if action in ("stop", "pause"):
    marker.touch()
else:
    marker.unlink(missing_ok=True)
url, session = os.environ["NARWHAL_CONTROL_ENGINE_URL"], os.environ["NARWHAL_CONTROL_SESSION"]
print(action, engine, url, session)
print("token visible:", "NARWHAL_CONTROL_TOKEN" in os.environ)
"""


def hook(name: str, script: str = HOOK_SCRIPT, timeout_s: float = 30.0) -> Hook:
    return Hook(name, (sys.executable, "-c", script), timeout_s)


class FakeRouter:
    """Serve `narwhal.state` and the lifecycle routes for the baseline engines.

    An engine whose marker file exists under `stopped` is reported ejected, as the router
    would after the hook stopped its process.
    """

    def __init__(self, stopped: Path) -> None:
        engines = json.loads(FLEET.read_text())["engines"]
        self.roles = {engine["iid"]: engine["role"] for engine in engines}
        self.stopped = stopped
        self.records = dict.fromkeys(self.roles, "active")
        self.requests: list[tuple[str, str, Any]] = []
        self.refuse: tuple[int, str] | None = None
        self.down = False
        self.state_down = False
        self.state_body: Any = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body: Any = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if self.down or (self.state_down and request.url.path == "/narwhal/state"):
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "GET" and request.url.path == "/narwhal/state":
            return httpx.Response(
                200, json=self.state() if self.state_body is None else self.state_body
            )
        if request.method == "POST" and request.url.path.startswith("/narwhal/lifecycle/"):
            if self.refuse is not None:
                status, error = self.refuse
                return httpx.Response(status, json=self.lifecycle(error))
            (iid,) = body["engines"]
            if request.url.path.endswith("/drain"):
                self.records[iid] = "drained"
            else:
                self.records[iid] = "active"
            return httpx.Response(200, json=self.lifecycle())
        return httpx.Response(404, json={"detail": "Not Found"})

    def out(self, iid: str) -> bool:
        return self.records[iid] != "active" or (self.stopped / iid).exists()

    def lifecycle(self, error: str = "") -> dict[str, Any]:
        return {
            "schema": "narwhal.lifecycle",
            "schema_version": 1,
            **self.view(),
            "error": error,
        }

    def view(self) -> dict[str, Any]:
        return {
            "engine_restart_policy": "individual",
            "process_starts": dict.fromkeys(self.roles, 100.0),
            "router": {"controls_fleet": True, "ready": True},
            "wave": {"id": "", "active": False, "ready_to_stop": False},
            "engines": {
                iid: {
                    "state": state,
                    "draining": state != "active",
                    "accepts_new": not self.out(iid),
                    "ready_to_stop": state == "drained",
                    "resident": {"prefill": 0, "decode": 0},
                    "deadline_at": None,
                    "restart_required": False,
                    "wave_id": "",
                    "old_process_start": 100.0 if state != "active" else None,
                    "new_process_start": None,
                    "checks": [],
                    "error": "",
                }
                for iid, state in self.records.items()
            },
            "events": [],
        }

    def state(self) -> dict[str, Any]:
        return {
            "schema": "narwhal.state",
            "schema_version": 1,
            "pools": {
                role: sorted(iid for iid, owner in self.roles.items() if owner == role)
                for role in ("prefill", "decode")
            },
            "resident": {iid: {"prefill": 0, "decode": 0} for iid in self.roles},
            "ejected": sorted(iid for iid in self.roles if (self.stopped / iid).exists()),
            "draining": sorted(iid for iid, state in self.records.items() if state != "active"),
            "quarantined": [],
            "probation": [],
            "pinned": [],
            "breaker": {
                "failures": {iid: {"connection": 0} for iid in self.roles},
                "verifying": [],
            },
            "lifecycle": self.view(),
        }


class EngineCase(unittest.IsolatedAsyncioTestCase):
    """Serve the control app with the engine routes, a fake router and scripted hooks."""

    hooks: ClassVar[dict[str, Hook]] = {
        name: hook(name)
        for name in ("engine_pause", "engine_resume", "engine_stop", "engine_start")
    }

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        stopped = Path(folder.name) / "stopped"
        stopped.mkdir()
        self.router = FakeRouter(stopped)
        config = ControlConfig(
            FLEET,
            {"restore": hook("restore", "print('restored')"), **self.hooks},
            runs_dir=self.runs,
            router=RouterEndpoint(ROUTER, 5.0),
        )
        env = {"PATH": "/usr/bin:/bin", "NARWHAL_CONTROL_TOKEN": TOKEN, "STOPPED": str(stopped)}
        self.service = ControlService(config, env=env, now=lambda: NOW)
        actions = EngineActions(self.service, httpx.MockTransport(self.router.handle))
        app = create_app(self.service, TOKEN, routers=[engine_routes(actions)])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://control",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)

    async def start_session(self) -> str:
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["session"])

    def record(self) -> dict[str, Any]:
        (directory,) = (self.runs / "sessions").iterdir()
        return dict(json.loads((directory / "run.json").read_text()))

    def last_action(self) -> dict[str, Any]:
        return dict(self.record()["actions"][-1])


class HookActionTests(EngineCase):
    async def test_each_hook_action_runs_its_hook_for_the_named_engine(self) -> None:
        session = await self.start_session()
        for action in ("pause", "resume", "stop", "start"):
            with self.subTest(action=action):
                response = await self.client.post(f"/api/engines/e1/{action}")
                self.assertEqual(response.status_code, 200, response.text)
                entry = self.last_action()
                self.assertEqual(response.json(), entry)
                self.assertEqual(entry["action"], f"engine.{action}")
                self.assertEqual(entry["params"], {"engine": "e1"})
                self.assertEqual(entry["outcome"], "ok")
                result = entry["result"]
                self.assertEqual(result["engine"], "e1")
                self.assertEqual(result["hook"]["hook"], f"engine_{action}")
                self.assertEqual(result["hook"]["exit_code"], 0)
                self.assertIn(
                    f"{action} e1 http://engine-1.invalid:8000 {session}", result["hook"]["tail"]
                )
                self.assertIn("token visible: False", result["hook"]["tail"])
        actions = self.record()["actions"]
        self.assertEqual(
            [entry["result"]["hook"]["log"] for entry in actions[1:]],
            [
                "hooks/001-engine_pause.log",
                "hooks/002-engine_resume.log",
                "hooks/003-engine_stop.log",
                "hooks/004-engine_start.log",
            ],
        )

    async def test_stop_and_start_record_the_engine_state_before_and_after(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/e1/stop")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        before, after = result["before"], result["after"]
        self.assertEqual(before["role"], "prefill")
        self.assertTrue(before["known"])
        self.assertFalse(before["ejected"])
        self.assertTrue(before["lifecycle"]["accepts_new"])
        self.assertTrue(after["ejected"])
        self.assertFalse(after["lifecycle"]["accepts_new"])
        self.assertEqual(after["router"], {"controls_fleet": True, "ready": True})
        self.assertEqual(after["process_start"], 100.0)
        self.assertEqual(after["breaker"], {"connection": 0})
        response = await self.client.post("/api/engines/e1/start")
        result = response.json()["result"]
        self.assertTrue(result["before"]["ejected"])
        self.assertFalse(result["after"]["ejected"])
        self.assertEqual(
            [path for method, path, _ in self.router.requests],
            ["/narwhal/state"] * 4,
        )


class LifecycleActionTests(EngineCase):
    async def test_drain_and_readmit_call_the_router_lifecycle_api(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/e3/drain", json={"deadline_s": 120})
        self.assertEqual(response.status_code, 200, response.text)
        entry = response.json()
        self.assertEqual(entry["action"], "engine.drain")
        self.assertEqual(entry["params"], {"engine": "e3", "deadline_s": 120})
        result = entry["result"]
        self.assertEqual(
            result["router"],
            {
                "path": "/narwhal/lifecycle/drain",
                "body": {"engines": ["e3"], "deadline_s": 120},
                "status": 200,
                "error": "",
            },
        )
        self.assertEqual(result["before"]["role"], "decode")
        self.assertEqual(result["before"]["lifecycle"]["state"], "active")
        self.assertFalse(result["before"]["draining"])
        self.assertEqual(result["after"]["lifecycle"]["state"], "drained")
        self.assertTrue(result["after"]["draining"])
        self.assertTrue(result["after"]["lifecycle"]["ready_to_stop"])

        response = await self.client.post("/api/engines/e3/readmit")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["router"]["body"], {"engines": ["e3"]})
        self.assertEqual(result["before"]["lifecycle"]["state"], "drained")
        self.assertEqual(result["after"]["lifecycle"]["state"], "active")
        self.assertEqual(
            [(method, path) for method, path, _ in self.router.requests],
            [
                ("GET", "/narwhal/state"),
                ("POST", "/narwhal/lifecycle/drain"),
                ("GET", "/narwhal/state"),
                ("GET", "/narwhal/state"),
                ("POST", "/narwhal/lifecycle/readmit"),
                ("GET", "/narwhal/state"),
            ],
        )
        self.assertEqual(
            [(entry["action"], entry["outcome"]) for entry in self.record()["actions"]],
            [("session.start", "ok"), ("engine.drain", "ok"), ("engine.readmit", "ok")],
        )

    async def test_drain_uses_the_router_default_deadline_unless_one_is_given(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/e0/drain")
        self.assertEqual(self.router.requests[1][2], {"engines": ["e0"]})

    async def test_a_router_refusal_is_a_failed_action_with_its_state(self) -> None:
        await self.start_session()
        self.router.refuse = (409, "readmission validation failed")
        response = await self.client.post("/api/engines/e0/readmit")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "router refused readmit with HTTP 409: readmission validation failed",
        )
        entry = self.last_action()
        self.assertEqual(response.json()["action"], entry)
        self.assertEqual((entry["action"], entry["outcome"]), ("engine.readmit", "failed"))
        self.assertEqual(entry["result"]["router"]["status"], 409)
        self.assertEqual(entry["result"]["router"]["error"], "readmission validation failed")
        self.assertEqual(entry["result"]["before"]["lifecycle"]["state"], "active")
        self.assertEqual(entry["result"]["after"]["lifecycle"]["state"], "active")

    async def test_an_unreachable_router_is_a_failed_action(self) -> None:
        await self.start_session()
        self.router.down = True
        response = await self.client.post("/api/engines/e0/drain")
        self.assertEqual(response.status_code, 502)
        self.assertIn("router /narwhal/lifecycle/drain failed: ConnectError", response.text)
        result = self.last_action()["result"]
        self.assertEqual(self.last_action()["outcome"], "failed")
        for side in ("before", "after"):
            self.assertIn("router state unavailable: ConnectError", result[side]["error"])

    async def test_an_invalid_deadline_or_parameter_is_refused(self) -> None:
        await self.start_session()
        for path, body in (
            ("/api/engines/e0/drain", {"deadline_s": 0}),
            ("/api/engines/e0/drain", {"deadline_s": "soon"}),
            ("/api/engines/e0/readmit", {"deadline_s": 30}),
            ("/api/engines/e0/stop", {"force": True}),
        ):
            with self.subTest(path=path, body=body):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.last_action()["outcome"], "refused")
        response = await self.client.post("/api/engines/e0/drain", content=b"[1]")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.router.requests, [])


class RefusalTests(EngineCase):
    async def test_engine_actions_need_a_session(self) -> None:
        response = await self.client.post("/api/engines/e0/stop")
        self.assertEqual(response.status_code, 409)
        self.assertIn("no session is active", response.json()["detail"])
        (line,) = (self.runs / "actions.jsonl").read_text().splitlines()
        entry = json.loads(line)
        self.assertEqual(
            (entry["action"], entry["outcome"], entry["session"]), ("engine.stop", "refused", None)
        )
        response = await self.client.get("/api/engines")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.router.requests, [])

    async def test_an_engine_outside_the_baseline_is_refused(self) -> None:
        await self.start_session()
        for action in ("stop", "drain"):
            with self.subTest(action=action):
                response = await self.client.post(f"/api/engines/e99/{action}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.json()["detail"],
                    "engine 'e99' is not in the baseline fleet configuration",
                )
                entry = self.last_action()
                self.assertEqual(
                    (entry["action"], entry["outcome"], entry["params"]),
                    (f"engine.{action}", "refused", {"engine": "e99"}),
                )
        self.assertEqual(self.router.requests, [])

    async def test_an_unknown_action_has_no_route(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/e0/reboot")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.record()["actions"]), 1)

    async def test_engine_routes_require_the_token(self) -> None:
        for method, path in (("POST", "/api/engines/e0/stop"), ("GET", "/api/engines")):
            with self.subTest(path=path):
                request = self.client.build_request(method, path)
                del request.headers["authorization"]
                response = await self.client.send(request)
                self.assertEqual(response.status_code, 401)
        self.assertFalse(self.runs.exists())


class FleetStateTests(EngineCase):
    async def test_the_engines_route_returns_each_baseline_engine_state(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/e2/drain")
        response = await self.client.get("/api/engines")
        self.assertEqual(response.status_code, 200, response.text)
        engines = response.json()["engines"]
        self.assertEqual(sorted(engines), sorted(self.router.roles))
        self.assertEqual(engines["e2"]["lifecycle"]["state"], "drained")
        self.assertEqual(engines["e0"]["lifecycle"]["state"], "active")
        self.router.down = True
        response = await self.client.get("/api/engines")
        self.assertEqual(response.status_code, 502)
        self.assertIn("router state unavailable", response.json()["detail"])

    async def test_an_unreadable_state_is_recorded_and_the_hook_still_runs(self) -> None:
        await self.start_session()
        self.router.state_down = True
        response = await self.client.post("/api/engines/e0/stop")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["hook"]["exit_code"], 0)
        self.assertIn("router state unavailable", result["before"]["error"])
        self.assertIn("router state unavailable", result["after"]["error"])

    async def test_a_malformed_state_document_is_recorded_as_unavailable(self) -> None:
        await self.start_session()
        bodies: tuple[object, ...] = ([], {"pools": {}}, {"schema": "narwhal.state"})
        for body in bodies:
            with self.subTest(body=body):
                self.router.state_body = body
                response = await self.client.post("/api/engines/e0/drain")
                self.assertEqual(response.status_code, 200, response.text)
                before = response.json()["result"]["before"]
                self.assertEqual(set(before), {"error"})
                self.assertIn("router state unavailable", before["error"])
                await self.client.post("/api/engines/e0/readmit")


class FailingHookTests(EngineCase):
    hooks: ClassVar[dict[str, Hook]] = {
        "engine_stop": hook("engine_stop", "print('container missing'); raise SystemExit(4)"),
        "engine_start": Hook("engine_start", ("/nonexistent/engine-control",), 5.0),
        "engine_pause": hook("engine_pause", "import time; time.sleep(60)", timeout_s=0.5),
    }

    async def test_a_failing_hook_is_a_failed_action_with_its_output(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/e0/stop")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "engine_stop hook exited 4")
        entry = self.last_action()
        self.assertEqual((entry["action"], entry["outcome"]), ("engine.stop", "failed"))
        self.assertEqual(entry["result"]["hook"]["exit_code"], 4)
        self.assertIn("container missing", entry["result"]["hook"]["tail"])
        self.assertTrue(entry["result"]["before"]["known"])
        self.assertFalse(entry["result"]["after"]["ejected"])

    async def test_a_hook_that_cannot_start_or_times_out_fails(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/e0/start")
        self.assertEqual(response.status_code, 502)
        self.assertIn("engine_start hook could not start", response.json()["detail"])
        self.assertIn("after", self.last_action()["result"])
        response = await self.client.post("/api/engines/e0/pause")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "engine_pause hook timed out after 0.5s")
        self.assertTrue(self.last_action()["result"]["hook"]["timed_out"])

    async def test_an_unconfigured_hook_is_refused(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/e0/resume")
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["detail"], "no 'engine_resume' hook is configured")
        self.assertEqual(self.last_action()["outcome"], "refused")
        self.assertEqual(self.router.requests, [])


class NarwhalRouterTests(unittest.IsolatedAsyncioTestCase):
    """Drain and readmit through the real router routes, reading the real state document."""

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = fleet(root, engines=("e0", "e1", "e3"))
        cfg.state_path = root / "handoff.json"
        router_app = serving_app.create_app(cfg)
        self.addAsyncCleanup(router_app.state.router.engines.aclose)
        config = ControlConfig(
            FLEET,
            {"restore": hook("restore", "print('restored')")},
            runs_dir=root / "runs",
            router=RouterEndpoint(ROUTER, 5.0),
        )
        self.service = ControlService(config, env={"PATH": "/usr/bin:/bin"}, now=lambda: NOW)
        self.actions = EngineActions(self.service, httpx.ASGITransport(app=router_app))
        await self.service.start_session()

    async def test_drain_and_readmit_record_the_router_lifecycle_state(self) -> None:
        with patch.object(
            serving_app,
            "capture_process_identities",
            new=AsyncMock(return_value=({"e0": 100}, {})),
        ):
            drained = await self.actions.act("e0", "drain", {"deadline_s": 60})
        assert drained.result is not None
        before, after = drained.result["before"], drained.result["after"]
        self.assertEqual((before["known"], before["role"]), (True, "prefill"))
        self.assertEqual(before["lifecycle"]["state"], "active")
        self.assertFalse(before["draining"])
        self.assertEqual(after["lifecycle"]["state"], "drained")
        self.assertEqual(after["lifecycle"]["old_process_start"], 100)
        self.assertTrue(after["draining"])
        self.assertFalse(after["lifecycle"]["accepts_new"])
        self.assertEqual(after["resident"], {"prefill": 0, "decode": 0})
        self.assertIn("connection", after["breaker"])
        with patch.object(
            serving_app,
            "validate_readmission",
            new=AsyncMock(return_value=ValidationOutcome(starts={"e0": 101})),
        ):
            readmitted = await self.actions.act("e0", "readmit", {})
        assert readmitted.result is not None
        self.assertEqual(readmitted.result["router"]["status"], 200)
        self.assertEqual(readmitted.result["after"]["lifecycle"]["state"], "active")
        self.assertEqual(readmitted.result["after"]["lifecycle"]["new_process_start"], 101)
        self.assertEqual(readmitted.result["after"]["process_start"], 101)
        self.assertTrue(readmitted.result["after"]["lifecycle"]["accepts_new"])

    async def test_a_baseline_engine_the_router_lacks_is_reported_unknown(self) -> None:
        assert self.service.session is not None
        state = await self.actions.fleet_state(self.service.session)
        self.assertEqual(state["e2"]["known"], False)
        self.assertIsNone(state["e2"]["lifecycle"])
        self.assertEqual(state["e3"]["role"], "decode")
        with self.assertRaises(ActionError) as caught:
            await self.actions.act("e2", "drain", {})
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("HTTP 409", str(caught.exception))


class RouterConfigTests(unittest.TestCase):
    def load(self, document: dict[str, Any]) -> ControlConfig:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet-control.local.json"
            base = {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}}
            path.write_text(json.dumps({**base, **document}))
            return load_config(path, {})

    def test_the_router_defaults_to_loopback(self) -> None:
        self.assertEqual(
            self.load({}).router, RouterEndpoint(DEFAULT_ROUTER_URL, DEFAULT_ROUTER_TIMEOUT_S)
        )
        self.assertEqual(DEFAULT_ROUTER_URL, "http://127.0.0.1:8000")

    def test_the_router_endpoint_is_configurable(self) -> None:
        config = self.load({"router": {"url": "http://127.0.0.1:18000/", "timeout_s": 30}})
        self.assertEqual(config.router, RouterEndpoint("http://127.0.0.1:18000", 30.0))

    def test_an_invalid_router_endpoint_is_reported(self) -> None:
        router = {"url": "ftp://router", "timeout_s": -1, "token": "x"}
        with self.assertRaises(ConfigError) as caught:
            self.load({"router": router})
        message = str(caught.exception)
        for problem in (
            "unknown key router.token",
            "router.url must be an http or https URL",
            "router.timeout_s must be a positive number",
        ):
            self.assertIn(problem, message)
        invalid: tuple[object, ...] = ("http://router", [], {"url": "http://[bad"})
        for router_value in invalid:
            with self.subTest(router=router_value), self.assertRaises(ConfigError):
                self.load({"router": router_value})

    def test_the_example_names_every_engine_hook(self) -> None:
        config = load_config(ROOT / "config/fleet-control.example.json", {})
        for name in ("engine_pause", "engine_resume", "engine_stop", "engine_start"):
            self.assertIn(name, config.hooks)
        self.assertEqual(config.router.url, DEFAULT_ROUTER_URL)


if __name__ == "__main__":
    unittest.main()
