"""Control service authentication, run records, the load-job lock and ending a session."""

from __future__ import annotations

import asyncio
import io
import json
import stat
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from tools.fleet_control import cli as control_cli
from tools.fleet_control.app import create_app
from tools.fleet_control.config import (
    DEFAULT_HOST,
    ConfigError,
    ControlConfig,
    Hook,
    is_loopback,
    load_config,
    read_token,
)
from tools.fleet_control.jobs import Job
from tools.fleet_control.service import ControlService
from tools.maintenance.check_publication import private_path

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "tests/data/fleet.json"
TOKEN = "fake-control-token-" + "0" * 24
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def python_hook(script: str, *, timeout_s: float = 30.0) -> Hook:
    return Hook("check", (sys.executable, "-c", script), timeout_s)


class FakeRunner:
    """Hold each job open until the test releases it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[Job] = []
        self.cancelled = 0

    def validate(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if "rate" in params and not isinstance(params["rate"], int | float):
            raise ValueError("rate must be a number")
        return {"rate": params.get("rate", 1)}

    async def run(self, job: Job) -> Mapping[str, Any]:
        self.started.append(job)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return {"requests": 10}


class ConfigTests(unittest.TestCase):
    def write(self, folder: str, document: dict[str, Any]) -> Path:
        path = Path(folder) / "fleet-control.local.json"
        path.write_text(json.dumps(document))
        return path

    def test_defaults_bind_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}})
            config = load_config(path, {})
        self.assertEqual(config.host, DEFAULT_HOST)
        self.assertTrue(is_loopback(config.host))
        self.assertEqual(config.port, 8020)
        self.assertEqual(config.token_env, "NARWHAL_CONTROL_TOKEN")
        self.assertEqual(config.runs_dir, Path("runs/fleet-control"))
        self.assertEqual(config.hook("restore"), Hook("restore", ("x",), 900.0))

    def test_example_config_loads(self) -> None:
        config = load_config(ROOT / "config/fleet-control.example.json", {})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertIn("router_restart", config.hooks)

    def test_non_loopback_listener_is_refused(self) -> None:
        for host in ("0.0.0.0", "::", "example.invalid"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as folder:
                document = {"host": host, "fleet": "f", "hooks": {"restore": {"argv": ["x"]}}}
                with self.assertRaisesRegex(ConfigError, "host must be a loopback address"):
                    load_config(self.write(folder, document), {})
        for host in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"):
            self.assertTrue(is_loopback(host), host)

    def test_every_problem_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            document = {"port": 0, "hooks": {"router_restart": {"argv": []}}, "extra": 1}
            with self.assertRaises(ConfigError) as caught:
                load_config(self.write(folder, document), {})
        message = str(caught.exception)
        for problem in (
            "unknown key 'extra'",
            "port must be an integer",
            "fleet must name the baseline",
            "hooks.router_restart.argv must be a non-empty list",
        ):
            self.assertIn(problem, message)

    def test_fleet_falls_back_to_narwhal_fleet(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"hooks": {"restore": {"argv": ["x"]}}})
            config = load_config(path, {"NARWHAL_FLEET": str(FLEET)})
        self.assertEqual(config.fleet, FLEET)

    def test_token_comes_from_the_named_environment_variable(self) -> None:
        config = ControlConfig(FLEET, {}, token_env="CONTROL_TOKEN_UNDER_TEST")
        self.assertEqual(read_token(config, {"CONTROL_TOKEN_UNDER_TEST": TOKEN}), TOKEN)
        for value in ("", "short", f" {TOKEN}"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                read_token(config, {"CONTROL_TOKEN_UNDER_TEST": value})

    def test_private_configuration_is_excluded_from_publication(self) -> None:
        self.assertTrue(private_path("config/fleet-control.local.json"))
        self.assertFalse(private_path("config/fleet-control.example.json"))

    def test_cli_serves_on_the_configured_loopback_listener(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}})
            env = {"NARWHAL_CONTROL_TOKEN": TOKEN}
            with (
                mock.patch.dict(control_cli.os.environ, env),
                mock.patch.object(control_cli, "check_http_bind") as bind,
                mock.patch.object(control_cli.uvicorn, "run") as run,
            ):
                self.assertEqual(control_cli.main(["--config", str(path)]), 0)
        bind.assert_called_once_with("127.0.0.1", 8020)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8020)

    def test_cli_refuses_to_start_without_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}})
            stderr = io.StringIO()
            with (
                mock.patch.dict(control_cli.os.environ, {"NARWHAL_CONTROL_TOKEN": ""}),
                mock.patch.object(control_cli.uvicorn, "run") as run,
                redirect_stderr(stderr),
            ):
                self.assertEqual(control_cli.main(["--config", str(path)]), 2)
        run.assert_not_called()
        self.assertIn("NARWHAL_CONTROL_TOKEN must hold the control bearer token", stderr.getvalue())


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    """Serve the control app in-process with a fake runner."""

    with_runner = True

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        self.marker = Path(folder.name) / "marker"
        config = ControlConfig(FLEET, {}, runs_dir=self.runs)
        self.runner = self.make_runner()
        env = {"PATH": "/usr/bin:/bin", "NARWHAL_CONTROL_TOKEN": TOKEN, "MARKER": str(self.marker)}
        self.service = ControlService(
            config, runner=self.runner if self.with_runner else None, env=env, now=lambda: NOW
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.service, TOKEN)),
            base_url="http://control",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.service.close)

    def make_runner(self) -> FakeRunner:
        return FakeRunner()

    async def start_session(self) -> str:
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["session"])

    def record(self) -> dict[str, Any]:
        (directory,) = (self.runs / "sessions").iterdir()
        return dict(json.loads((directory / "run.json").read_text()))

    def log(self) -> list[dict[str, Any]]:
        lines = (self.runs / "actions.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def actions(self) -> list[tuple[str, str]]:
        return [(entry["action"], entry["outcome"]) for entry in self.record()["actions"]]


class AuthenticationTests(ServiceCase):
    async def test_requests_without_a_valid_token_are_refused(self) -> None:
        cases = {
            "missing": {"authorization": ""},
            "invalid": {"authorization": "Bearer " + "1" * 43},
            "wrong scheme": {"authorization": f"Basic {TOKEN}"},
            "token prefix": {"authorization": f"Bearer {TOKEN[:-1]}"},
        }
        for label, headers in cases.items():
            for method, path in (
                ("GET", "/api/health"),
                ("POST", "/api/session"),
                ("POST", "/api/jobs"),
                ("POST", "/api/session/end"),
                ("GET", "/no/such/route"),
            ):
                with self.subTest(label=label, path=path):
                    request = self.client.build_request(method, path)
                    request.headers.update(headers)
                    if not headers["authorization"]:
                        del request.headers["authorization"]
                    response = await self.client.send(request)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
                    self.assertEqual(response.json(), {"detail": "missing or invalid bearer token"})
        self.assertIsNone(self.service.session)
        self.assertFalse(self.runs.exists())

    async def test_a_valid_token_is_admitted(self) -> None:
        response = await self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "session": None,
                "job": None,
                "in_progress": None,
                "in_progress_since": None,
            },
        )


class RunRecordTests(ServiceCase):
    async def test_session_start_records_the_baseline_and_the_action(self) -> None:
        session = await self.start_session()
        self.assertRegex(session, r"^20260102T030405Z-[0-9a-f]{6}$")
        directory = self.runs / "sessions" / session
        record = self.record()
        baseline = json.loads(FLEET.read_text())
        self.assertEqual(record["schema"], "narwhal.fleet-control-run")
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["session"], session)
        self.assertEqual(record["started_at"], NOW.isoformat())
        self.assertIsNone(record["ended_at"])
        self.assertEqual(record["baseline"], {"source": str(FLEET), "copy": "baseline.json"})
        self.assertEqual(record["configuration"]["source"], "baseline")
        self.assertEqual(record["configuration"]["document"], baseline)
        self.assertEqual(json.loads((directory / "baseline.json").read_text()), baseline)
        (action,) = record["actions"]
        self.assertEqual(action["seq"], 1)
        self.assertEqual(action["session"], session)
        self.assertEqual(action["action"], "session.start")
        self.assertEqual(action["outcome"], "ok")
        self.assertEqual(action["started_at"], NOW.isoformat())
        self.assertEqual(action["finished_at"], NOW.isoformat())
        self.assertTrue(action["result"]["baseline_digest"].startswith("sha256:"))
        self.assertEqual(self.log(), record["actions"])
        for path in (
            directory / "run.json",
            directory / "baseline.json",
            self.runs / "actions.jsonl",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    async def test_refused_actions_are_logged_with_their_reason(self) -> None:
        response = await self.client.post("/api/jobs", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("no session is active", response.json()["detail"])
        (entry,) = self.log()
        self.assertEqual(
            (entry["action"], entry["outcome"], entry["session"]), ("job.start", "refused", None)
        )
        await self.start_session()
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["action"]["outcome"], "refused")
        self.assertEqual(self.actions(), [("session.start", "ok"), ("session.start", "refused")])
        self.assertEqual(len(self.log()), 3)

    async def test_session_route_returns_the_run_record(self) -> None:
        response = await self.client.get("/api/session")
        self.assertEqual(response.status_code, 404)
        await self.start_session()
        response = await self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        changes = {"configuration": False, "engines": {}}
        self.assertEqual(response.json(), {**self.record(), "changes": changes})

    async def test_an_invalid_baseline_refuses_the_session(self) -> None:
        broken = self.runs.parent / "broken.json"
        broken.write_text('{"schema": "narwhal.fleet", "schema_version": 1}')
        self.service.config = ControlConfig(broken, self.service.config.hooks, runs_dir=self.runs)
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 500)
        self.assertIn("baseline fleet configuration is invalid", response.json()["detail"])
        self.assertIsNone(self.service.session)
        self.assertEqual(self.log()[0]["outcome"], "failed")


class JobLockTests(ServiceCase):
    async def test_a_second_concurrent_load_job_is_refused(self) -> None:
        await self.start_session()
        first = await self.client.post("/api/jobs", json={"rate": 2})
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["result"]["job"]["state"], "running")
        second = await self.client.post("/api/jobs", json={"rate": 4})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "load job job-001 is running")
        await asyncio.sleep(0)
        self.assertEqual(len(self.runner.started), 1)
        self.runner.release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        status = await self.client.get("/api/jobs/current")
        self.assertEqual(status.json()["state"], "succeeded")
        self.assertEqual(status.json()["result"], {"requests": 10})
        third = await self.client.post("/api/jobs", json={"rate": 4})
        self.assertEqual(third.status_code, 201, third.text)
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("job.start", "ok"),
                ("job.start", "refused"),
                ("job.complete", "ok"),
                ("job.start", "ok"),
            ],
        )
        entries = self.record()["actions"]
        self.assertEqual(entries[1]["params"], {"rate": 2})
        self.assertEqual(entries[3]["result"]["params"], {"rate": 2})
        self.assertTrue((self.runs / "sessions" / entries[0]["session"] / "jobs/job-001").is_dir())

    async def test_stopping_a_job_frees_the_slot(self) -> None:
        await self.start_session()
        await self.client.post("/api/jobs", json={})
        await asyncio.sleep(0)
        response = await self.client.post("/api/jobs/current/stop")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["job"]["state"], "stopped")
        self.assertEqual(self.runner.cancelled, 1)
        response = await self.client.post("/api/jobs/current/stop")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("job.start", "ok"),
                ("job.complete", "ok"),
                ("job.stop", "ok"),
                ("job.stop", "refused"),
            ],
        )

    async def test_invalid_job_parameters_are_refused(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/jobs", json={"rate": "fast"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "rate must be a number")
        response = await self.client.post("/api/jobs", content=b"[1]")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runner.started, [])


class NoRunnerTests(ServiceCase):
    with_runner = False

    async def test_load_jobs_need_a_runner(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/jobs", json={})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(self.actions()[-1], ("job.start", "refused"))


HOOK_SCRIPT = """
import json, os, pathlib
baseline = json.loads(pathlib.Path(os.environ["NARWHAL_CONTROL_BASELINE"]).read_text())
print("checking", baseline["model"], os.environ["NARWHAL_CONTROL_SESSION"])
print("token visible:", "NARWHAL_CONTROL_TOKEN" in os.environ)
"""


class HookEnvironmentTests(ServiceCase):
    async def test_a_hook_sees_the_session_and_never_the_token(self) -> None:
        session = await self.start_session()
        assert self.service.session is not None
        result = await self.service.run_hook(python_hook(HOOK_SCRIPT), self.service.session)
        document = result.document(self.service.session.directory)
        self.assertEqual(document["exit_code"], 0)
        self.assertEqual(document["log"], "hooks/001-check.log")
        self.assertIn(f"checking test-model {session}", document["tail"])
        self.assertIn("token visible: False", document["tail"])
        log = self.runs / "sessions" / session / document["log"]
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)


class EndSessionTests(ServiceCase):
    async def test_ending_a_session_stops_the_job_and_leaves_the_fleet(self) -> None:
        await self.start_session()
        await self.client.post("/api/jobs", json={})
        await asyncio.sleep(0)
        response = await self.client.post("/api/session/end")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.service.session)
        self.assertEqual(self.runner.cancelled, 1)
        record = self.record()
        self.assertEqual(record["ended_at"], NOW.isoformat())
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("job.start", "ok"),
                ("job.complete", "ok"),
                ("job.stop", "ok"),
                ("session.end", "ok"),
            ],
        )
        self.assertEqual(
            record["actions"][4]["result"],
            {
                "changes": {"configuration": False, "engines": {}},
                "journal": {"extract": None, "notes": ["router.journal is not configured"]},
            },
        )
        self.assertEqual([entry["source"] for entry in record["configurations"]], ["baseline"])
        response = await self.client.post("/api/session/end")
        self.assertEqual(response.status_code, 409)


class SlowStopRunner(FakeRunner):
    """Hold a cancelled job open until the test releases the stop."""

    def __init__(self) -> None:
        super().__init__()
        self.stop_gate = asyncio.Event()

    async def run(self, job: Job) -> Mapping[str, Any]:
        self.started.append(job)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.stop_gate.wait()
            raise
        return {"requests": 10}


class ExclusiveActionTests(ServiceCase):
    def make_runner(self) -> FakeRunner:
        return SlowStopRunner()

    async def test_actions_are_refused_while_the_session_ends(self) -> None:
        await self.start_session()
        await self.client.post("/api/jobs", json={})
        await asyncio.sleep(0)
        ending = asyncio.create_task(self.client.post("/api/session/end"))
        for _ in range(200):
            if self.service.status()["in_progress"] == "session.end":
                break
            await asyncio.sleep(0.01)
        response = await self.client.post("/api/jobs", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "session.end is in progress")
        assert isinstance(self.runner, SlowStopRunner)
        self.runner.stop_gate.set()
        self.assertEqual((await ending).status_code, 200)
        self.assertEqual(
            self.actions(),
            [
                ("session.start", "ok"),
                ("job.start", "ok"),
                ("job.start", "refused"),
                ("job.complete", "ok"),
                ("job.stop", "ok"),
                ("session.end", "ok"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
