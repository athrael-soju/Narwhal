"""Configuration overlays, cold restarts and baseline restores with fake hooks and a fake router."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from narwhal.config import FleetConfig
from narwhal.contracts import canonical_digest
from tests.fleet_control.test_service_core import FLEET, NOW, TOKEN, FakeRunner
from tools.fleet_control import cli as control_cli
from tools.fleet_control.app import create_app
from tools.fleet_control.config import (
    ControlConfig,
    Hook,
    RouterEndpoint,
    load_config,
)
from tools.fleet_control.overlays import (
    COLD_RESTART_HOOK,
    FLEET_ENV,
    ROUTER_RESTART_HOOK,
    Overlays,
    merge,
    overlay_problems,
    overlay_routes,
)
from tools.fleet_control.service import ControlService

BASELINE = json.loads(FLEET.read_text())

CAPTURE_SCRIPT = f"""
import json, os, pathlib, sys
fleet = os.environ.get({FLEET_ENV!r})
entry = {{
    "hook": sys.argv[1],
    "fleet": fleet,
    "document": json.loads(pathlib.Path(fleet).read_text()) if fleet else None,
}}
with open(os.environ["CAPTURE"], "a") as stream:
    stream.write(json.dumps(entry) + "\\n")
print("ran", sys.argv[1])
"""

WAIT_SCRIPT = """
import os, pathlib, time
marker = pathlib.Path(os.environ["MARKER"])
deadline = time.monotonic() + 20
while not marker.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def hook(name: str, script: str = CAPTURE_SCRIPT) -> Hook:
    return Hook(name, (sys.executable, "-c", script, name), 30.0)


def capture_hooks() -> dict[str, Hook]:
    return {name: hook(name) for name in ("restore", ROUTER_RESTART_HOOK, COLD_RESTART_HOOK)}


class FakeRouter:
    """Answer `GET /ready` with 503 for the first `not_ready` probes, then 200."""

    def __init__(self, not_ready: int | None) -> None:
        self.not_ready = not_ready
        self.probes = 0
        self.unreachable = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ready", request.url
        self.probes += 1
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        if self.not_ready is None or self.probes <= self.not_ready:
            body = {"status": "not_ready", "reason": "engine identity validation pending"}
            return httpx.Response(503, json=body)
        return httpx.Response(200, json={"status": "ready", "reason": ""})


class MergeTests(unittest.TestCase):
    def test_objects_merge_and_scalars_and_arrays_replace(self) -> None:
        base = {
            "serving": {"admission": "predictive", "max_connections": 512},
            "controller": {"thresholds": {"expand": 1.0, "shrink": 0.5}, "min_decode": 1},
            "_notes": ["a", "b"],
        }
        overlay = {
            "serving": {"max_connections": 64},
            "controller": {"thresholds": {"shrink": 0.25}},
            "recovery": {"health": {"window_s": 10.0}},
            "_notes": ["c"],
        }
        self.assertEqual(
            merge(base, overlay),
            {
                "serving": {"admission": "predictive", "max_connections": 64},
                "controller": {"thresholds": {"expand": 1.0, "shrink": 0.25}, "min_decode": 1},
                "recovery": {"health": {"window_s": 10.0}},
                "_notes": ["c"],
            },
        )

    def test_null_removes_a_key_and_the_inputs_are_unchanged(self) -> None:
        base = {"serving": {"admission": "reactive", "max_connections": 64}, "slo": {"ttft_s": 1}}
        overlay: dict[str, Any] = {"serving": {"admission": None, "absent": None}, "slo": None}
        snapshot = json.dumps([base, overlay])
        merged = merge(base, overlay)
        self.assertEqual(merged, {"serving": {"max_connections": 64}})
        merged["serving"]["max_connections"] = 1
        self.assertEqual(json.dumps([base, overlay]), snapshot)

    def test_an_object_replaces_a_scalar(self) -> None:
        self.assertEqual(
            merge({"serving": 3}, {"serving": {"queue_capacity": 4, "x": None}}),
            {"serving": {"queue_capacity": 4}},
        )

    def test_only_router_policy_sections_may_change(self) -> None:
        self.assertEqual(overlay_problems({"serving": {}, "slo": {}, "_why": "test"}), [])
        problems = overlay_problems({"engines": [], "model": "other", "recovery": {}})
        self.assertEqual(len(problems), 2)
        self.assertIn("may not change 'engines'", problems[0])
        self.assertIn("may not change 'model'", problems[1])
        self.assertEqual(overlay_problems({}), ["the overlay changes no section"])


class OverlayConfigTests(unittest.TestCase):
    def write(self, folder: str, document: dict[str, Any]) -> Path:
        path = Path(folder) / "fleet-control.local.json"
        path.write_text(
            json.dumps({"fleet": "f", "hooks": {"restore": {"argv": ["x"]}}, **document})
        )
        return path

    def test_example_config_names_the_restart_hooks(self) -> None:
        config = load_config(
            Path(__file__).resolve().parents[2] / "config/fleet-control.example.json", {}
        )
        self.assertEqual(config.router.url, "http://127.0.0.1:8000")
        self.assertIn(ROUTER_RESTART_HOOK, config.hooks)
        self.assertIn(COLD_RESTART_HOOK, config.hooks)

    def test_cli_serves_the_overlay_routes(self) -> None:
        async def post(app: Any, route: str) -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://control",
                headers={"authorization": f"Bearer {TOKEN}"},
            ) as client:
                return await client.post(route, json={"serving": {}})

        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder) / "runs"
            path = self.write(folder, {"fleet": str(FLEET), "runs_dir": str(runs)})
            with (
                mock.patch.dict(control_cli.os.environ, {"NARWHAL_CONTROL_TOKEN": TOKEN}),
                mock.patch.object(control_cli, "check_http_bind"),
                mock.patch.object(control_cli.uvicorn, "run") as run,
            ):
                self.assertEqual(control_cli.main(["--config", str(path)]), 0)
            app = run.call_args.args[0]
            for route in ("/api/config/overlay", "/api/config/cold-restart", "/api/config/restore"):
                with self.subTest(route=route):
                    response = asyncio.run(post(app, route))
                    # No session is active, so a served action route answers 409, not 404.
                    self.assertEqual(response.status_code, 409, response.text)


class OverlayCase(unittest.IsolatedAsyncioTestCase):
    """Serve the control app with capturing hooks and a fake router readiness route."""

    not_ready: int | None = 0
    router_timeout_s = 0.3

    def hooks(self) -> dict[str, Hook]:
        return capture_hooks()

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        self.capture = Path(folder.name) / "capture.jsonl"
        self.marker = Path(folder.name) / "marker"
        config = ControlConfig(
            FLEET,
            self.hooks(),
            runs_dir=self.runs,
            router=RouterEndpoint(timeout_s=self.router_timeout_s),
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "NARWHAL_CONTROL_TOKEN": TOKEN,
            "CAPTURE": str(self.capture),
            "MARKER": str(self.marker),
        }
        self.runner = FakeRunner()
        self.service = ControlService(config, runner=self.runner, env=env, now=lambda: NOW)
        self.router = FakeRouter(self.not_ready)
        overlays = Overlays(
            self.service, transport=httpx.MockTransport(self.router.handle), poll_s=0.01
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(self.service, TOKEN, [overlay_routes(overlays)])
            ),
            base_url="http://control",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.service.close)

    async def start_session(self) -> Path:
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 201, response.text)
        return self.runs / "sessions" / str(response.json()["session"])

    def record(self) -> dict[str, Any]:
        (directory,) = (self.runs / "sessions").iterdir()
        return dict(json.loads((directory / "run.json").read_text()))

    def actions(self) -> list[tuple[str, str]]:
        return [(entry["action"], entry["outcome"]) for entry in self.record()["actions"]]

    def captured(self) -> list[dict[str, Any]]:
        if not self.capture.exists():
            return []
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def sources(self) -> list[str]:
        return [entry["source"] for entry in self.record()["configurations"]]


class OverlayTests(OverlayCase):
    async def test_a_valid_overlay_is_written_and_applied_through_a_router_restart(self) -> None:
        directory = await self.start_session()
        overlay = {"serving": {"max_connections": 64}, "slo": {"ttft_s": 5.0}}
        response = await self.client.post("/api/config/overlay", json=overlay)
        self.assertEqual(response.status_code, 200, response.text)
        action = response.json()
        self.assertEqual((action["action"], action["outcome"]), ("config.overlay", "ok"))
        self.assertEqual(action["params"], {"overlay": overlay})
        result = action["result"]
        expected = merge(BASELINE, overlay)
        fleet = directory / "overlays/001-fleet.json"
        self.assertEqual(result["fleet"], "overlays/001-fleet.json")
        self.assertEqual(json.loads(fleet.read_text()), expected)
        self.assertEqual(stat.S_IMODE(fleet.stat().st_mode), 0o600)
        self.assertEqual(FleetConfig.load(fleet).max_connections, 64)
        self.assertEqual(result["digest"], canonical_digest(expected))
        self.assertEqual(result["base_digest"], canonical_digest(BASELINE))
        self.assertEqual(result["hook"]["exit_code"], 0)
        self.assertEqual(result["hook"]["log"], "hooks/001-router_restart.log")
        (run,) = self.captured()
        self.assertEqual(run["hook"], ROUTER_RESTART_HOOK)
        self.assertEqual(run["fleet"], str(fleet.resolve()))
        self.assertEqual(run["document"], expected)
        self.assertEqual(result["readiness"]["ready"], True)
        self.assertEqual(result["readiness"]["status_code"], 200)
        self.assertEqual(result["readiness"]["path"], "/ready")
        self.assertFalse((directory / "overlays/candidate.json").exists())
        record = self.record()
        self.assertEqual(self.actions(), [("session.start", "ok"), ("config.overlay", "ok")])
        self.assertEqual(self.sources(), ["baseline", "overlay"])
        self.assertEqual(
            [entry["fleet"] for entry in record["configurations"]],
            ["baseline.json", "overlays/001-fleet.json"],
        )
        self.assertEqual(record["configuration"]["document"], expected)
        self.assertEqual(record["configuration"]["digest"], result["digest"])

    async def test_each_overlay_merges_onto_the_current_configuration(self) -> None:
        directory = await self.start_session()
        first = await self.client.post(
            "/api/config/overlay", json={"serving": {"max_connections": 64}}
        )
        second = await self.client.post(
            "/api/config/overlay",
            json={
                "controller": {"thresholds": {"shrink": 0.25}},
                "serving": {"max_connections": None},
            },
        )
        self.assertEqual(second.status_code, 200, second.text)
        document = json.loads((directory / "overlays/002-fleet.json").read_text())
        self.assertNotIn("max_connections", document.get("serving", {}))
        self.assertEqual(
            document["controller"]["thresholds"],
            {"expand": 1.0, "shrink": 0.25, "cooldown_s": 10.0},
        )
        self.assertEqual(second.json()["result"]["base_digest"], first.json()["result"]["digest"])
        self.assertEqual(self.sources(), ["baseline", "overlay", "overlay"])
        self.assertEqual([run["hook"] for run in self.captured()], [ROUTER_RESTART_HOOK] * 2)

    async def test_an_invalid_overlay_is_refused_with_every_error_and_no_hook_runs(self) -> None:
        directory = await self.start_session()
        cases: dict[str, tuple[dict[str, Any], list[str]]] = {
            "field types": (
                {
                    "serving": {"max_connections": "many", "admission_margin": "x", "typo": 1},
                    "controller": {"advisory": 1},
                },
                [
                    "unknown serving key 'typo'",
                    "controller.advisory must be a boolean",
                    "serving.admission_margin must be a number",
                    "serving.max_connections must be an integer",
                ],
            ),
            "cross-field limits": (
                {"slo": {"ttft_s": 0}, "controller": {"monitor_interval_s": -1}},
                ["slo.ttft_s must be positive", "controller.monitor_interval_s must be positive"],
            ),
            "a removed required section": ({"slo": None}, ["missing required key 'slo'"]),
            "deployment sections": (
                {"engines": [], "model": "other", "serving": {"max_connections": 1}},
                ["may not change 'engines'", "may not change 'model'"],
            ),
            "no change": ({}, ["the overlay changes no section"]),
        }
        for label, (overlay, expected) in cases.items():
            with self.subTest(label):
                response = await self.client.post("/api/config/overlay", json=overlay)
                self.assertEqual(response.status_code, 422, response.text)
                body = response.json()
                errors = body["action"]["result"]["errors"]
                self.assertEqual(len(errors), len(expected), errors)
                for problem in expected:
                    self.assertTrue(any(problem in error for error in errors), (problem, errors))
                    self.assertIn(problem, body["detail"])
                self.assertEqual(body["action"]["outcome"], "refused")
                self.assertNotIn(str(directory), body["detail"])
        self.assertEqual(self.captured(), [])
        self.assertEqual(self.router.probes, 0)
        self.assertEqual(list((directory / "overlays").iterdir()), [])
        self.assertEqual(self.sources(), ["baseline"])
        self.assertEqual(self.actions()[1:], [("config.overlay", "refused")] * len(cases))

    async def test_a_check_reports_the_merged_document_without_applying_it(self) -> None:
        session = await self.start_session()
        response = await self.client.post(
            "/api/config/overlay/check", json={"serving": {"max_connections": 64}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        check = response.json()
        self.assertEqual(check["errors"], [])
        self.assertEqual(check["document"]["serving"]["max_connections"], 64)
        assert self.service.session is not None
        current = self.service.session.configurations[-1]
        self.assertEqual(check["base_digest"], current["digest"])
        self.assertEqual(check["digest"], canonical_digest(check["document"]))
        self.assertEqual(len(self.service.session.configurations), 1)
        self.assertEqual(self.actions(), [("session.start", "ok")])
        self.assertEqual(self.captured(), [])
        self.assertEqual(list((session / "overlays").iterdir()), [])

    async def test_a_check_reports_every_error(self) -> None:
        await self.start_session()
        response = await self.client.post(
            "/api/config/overlay/check",
            json={"serving": {"max_connections": "many"}, "engines": []},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("may not change 'engines'", " ".join(response.json()["errors"]))
        self.assertIsNone(response.json()["digest"])
        response = await self.client.post(
            "/api/config/overlay/check", json={"serving": {"max_connections": "many"}}
        )
        self.assertTrue(any("max_connections" in error for error in response.json()["errors"]))
        response = await self.client.post("/api/config/overlay/check", content=b"[1]")
        self.assertEqual(response.status_code, 422)

    async def test_a_body_that_is_not_a_json_object_is_refused(self) -> None:
        await self.start_session()
        for content in (b"", b"[1]", b"{", b'{"serving": {"admission_margin": NaN}}'):
            with self.subTest(content=content):
                response = await self.client.post("/api/config/overlay", content=content)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "the overlay must be a JSON object"})
        self.assertEqual(self.captured(), [])

    async def test_actions_need_a_session(self) -> None:
        for path, body in (
            ("/api/config/overlay", {"serving": {"max_connections": 64}}),
            ("/api/config/cold-restart", None),
            ("/api/config/restore", None),
        ):
            with self.subTest(path=path):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status_code, 409)
                self.assertIn("no session is active", response.json()["detail"])
                self.assertIsNone(response.json()["action"]["session"])
        self.assertEqual(self.captured(), [])

    async def test_the_routes_need_the_token(self) -> None:
        for path in ("/api/config/overlay", "/api/config/cold-restart", "/api/config/restore"):
            with self.subTest(path=path):
                response = await self.client.post(path, headers={"authorization": "Bearer x"})
                self.assertEqual(response.status_code, 401)
        self.assertFalse(self.runs.exists())

    async def test_cold_restart_restarts_the_fleet_from_the_current_fleet_file(self) -> None:
        directory = await self.start_session()
        response = await self.client.post("/api/config/cold-restart")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["fleet"], "baseline.json")
        await self.client.post("/api/config/overlay", json={"serving": {"max_connections": 64}})
        response = await self.client.post("/api/config/cold-restart")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["fleet"], "overlays/001-fleet.json")
        self.assertEqual(result["hook"]["log"], "hooks/003-cold_restart.log")
        self.assertTrue(result["readiness"]["ready"])
        runs = self.captured()
        self.assertEqual(
            [(run["hook"], run["fleet"]) for run in runs],
            [
                (COLD_RESTART_HOOK, str((directory / "baseline.json").resolve())),
                (ROUTER_RESTART_HOOK, str((directory / "overlays/001-fleet.json").resolve())),
                (COLD_RESTART_HOOK, str((directory / "overlays/001-fleet.json").resolve())),
            ],
        )
        self.assertEqual(self.sources(), ["baseline", "overlay"])
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("config.cold_restart", "ok"),
                ("config.overlay", "ok"),
                ("config.cold_restart", "ok"),
            ],
        )

    async def test_restore_returns_to_the_baseline_and_waits_for_the_router(self) -> None:
        directory = await self.start_session()
        await self.client.post("/api/config/overlay", json={"serving": {"max_connections": 64}})
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["restore_seq"], 3)
        self.assertEqual(result["fleet"], "baseline.json")
        self.assertEqual(result["digest"], canonical_digest(BASELINE))
        self.assertTrue(result["readiness"]["ready"])
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("config.overlay", "ok"),
                ("baseline.restore", "ok"),
                ("config.restore", "ok"),
            ],
        )
        record = self.record()
        self.assertEqual(self.sources(), ["baseline", "overlay", "baseline"])
        self.assertEqual(record["configuration"]["document"], BASELINE)
        self.assertEqual(record["configuration"]["fleet"], "baseline.json")
        self.assertEqual(record["actions"][2]["result"]["hook"], "restore")
        await self.client.post("/api/config/cold-restart")
        self.assertEqual(self.captured()[-1]["fleet"], str((directory / "baseline.json").resolve()))
        self.assertEqual(self.router.probes, 3)


class SlowRouterTests(OverlayCase):
    not_ready = 2

    async def test_readiness_waits_through_not_ready_answers(self) -> None:
        await self.start_session()
        response = await self.client.post(
            "/api/config/overlay", json={"serving": {"max_connections": 64}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["result"]["readiness"]
        self.assertEqual(readiness["attempts"], 3)
        self.assertTrue(readiness["ready"])


class UnreadyRouterTests(OverlayCase):
    not_ready = None
    router_timeout_s = 0.2

    async def test_a_router_that_never_becomes_ready_fails_the_overlay(self) -> None:
        await self.start_session()
        response = await self.client.post(
            "/api/config/overlay", json={"serving": {"max_connections": 64}}
        )
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["detail"], "router was not ready after 0.2s")
        result = response.json()["action"]["result"]
        readiness = result["readiness"]
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status_code"], 503)
        self.assertEqual(readiness["reason"], "engine identity validation pending")
        self.assertGreater(readiness["attempts"], 1)
        self.assertGreaterEqual(readiness["waited_s"], 0.2)
        self.assertEqual(result["hook"]["exit_code"], 0)
        # The router restarted with the overlay, so it governs the fleet despite the timeout.
        self.assertEqual(self.sources(), ["baseline", "overlay"])
        self.assertEqual(self.actions()[-1], ("config.overlay", "failed"))
        self.assertIsNone(self.service.status()["in_progress"])

    async def test_an_unreachable_router_fails_the_restore(self) -> None:
        await self.start_session()
        self.router.unreachable = True
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 504, response.text)
        readiness = response.json()["action"]["result"]["readiness"]
        self.assertIsNone(readiness["status_code"])
        self.assertIn("ConnectError", readiness["reason"])
        self.assertEqual(
            self.actions(),
            [("session.start", "ok"), ("baseline.restore", "ok"), ("config.restore", "failed")],
        )


class FailedRouterRestartTests(OverlayCase):
    def hooks(self) -> dict[str, Hook]:
        hooks = capture_hooks()
        hooks[ROUTER_RESTART_HOOK] = hook(
            ROUTER_RESTART_HOOK, "import sys; print('no'); sys.exit(4)"
        )
        return hooks

    async def test_a_failed_router_restart_is_not_recorded_as_applied(self) -> None:
        directory = await self.start_session()
        response = await self.client.post(
            "/api/config/overlay", json={"serving": {"max_connections": 64}}
        )
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"], "router_restart hook exited 4")
        result = response.json()["action"]["result"]
        self.assertEqual(result["hook"]["exit_code"], 4)
        self.assertEqual(result["fleet"], "overlays/001-fleet.json")
        self.assertTrue((directory / result["fleet"]).exists())
        document = json.loads((directory / result["fleet"]).read_text())
        self.assertEqual(result["digest"], canonical_digest(document))
        self.assertEqual(self.router.probes, 0)
        self.assertEqual(self.sources(), ["baseline"])


class MissingRestartHookTests(OverlayCase):
    def hooks(self) -> dict[str, Hook]:
        return {"restore": hook("restore")}

    async def test_restarts_need_their_hooks(self) -> None:
        await self.start_session()
        for path, name in (
            ("/api/config/overlay", ROUTER_RESTART_HOOK),
            ("/api/config/cold-restart", COLD_RESTART_HOOK),
        ):
            with self.subTest(path=path):
                response = await self.client.post(path, json={"serving": {}})
                self.assertEqual(response.status_code, 501, response.text)
                self.assertEqual(response.json()["detail"], f"no {name!r} hook is configured")
                self.assertEqual(response.json()["action"]["outcome"], "refused")
        self.assertEqual(self.captured(), [])


class ExclusiveOverlayTests(OverlayCase):
    def hooks(self) -> dict[str, Hook]:
        hooks = capture_hooks()
        hooks[ROUTER_RESTART_HOOK] = hook(ROUTER_RESTART_HOOK, WAIT_SCRIPT)
        return hooks

    async def test_other_actions_are_refused_while_the_router_restarts(self) -> None:
        await self.start_session()
        applying = asyncio.create_task(
            self.client.post("/api/config/overlay", json={"serving": {"max_connections": 64}})
        )
        for _ in range(200):
            if self.service.status()["in_progress"] == "config.overlay":
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.service.status()["in_progress_since"])
        for path, body in (
            ("/api/jobs", {}),
            ("/api/config/overlay", {"serving": {"max_connections": 32}}),
            ("/api/config/cold-restart", None),
            ("/api/config/restore", None),
            ("/api/session/end", None),
        ):
            with self.subTest(path=path):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"], "config.overlay is in progress")
        self.marker.write_text("done")
        self.assertEqual((await applying).status_code, 200)
        self.assertEqual(self.runner.started, [])
        self.assertEqual(self.service.status()["in_progress"], None)
        self.assertIsNone(self.service.status()["in_progress_since"])
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("job.start", "refused"),
                ("config.overlay", "refused"),
                ("config.cold_restart", "refused"),
                ("config.restore", "refused"),
                ("session.end", "refused"),
                ("config.overlay", "ok"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
