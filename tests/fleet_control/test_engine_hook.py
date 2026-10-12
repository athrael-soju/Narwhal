"""The reference engine hook against a stubbed container runtime and engine health route."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from narwhal import backends
from tools.fleet_control import engine_hook

CID = "c" * 64

# Records each call and answers `inspect` with the state that DOCKER_RUNNING selects.
FAKE_DOCKER = """#!{python}
import json, os, sys
with open(os.environ["DOCKER_CALLS"], "a") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == os.environ.get("DOCKER_FAIL"):
    print("stub daemon refused", file=sys.stderr)
    sys.exit(1)
if sys.argv[1] == "inspect":
    print(json.dumps({{"Running": os.environ.get("DOCKER_RUNNING", "1") == "1"}}))
"""

# Writes the launch directory it received to argv[1], then exits with argv[2].
AFTER_START = """
import os, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(os.environ["NARWHAL_CONTROL_LAUNCH_DIR"])
sys.exit(int(sys.argv[2]))
"""


class Engine(BaseHTTPRequestHandler):
    status = 200
    path_seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).path_seen.append(self.path)
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class EngineHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable))
        docker.chmod(0o755)
        self.calls = self.root / "calls"
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_CALLS": str(self.calls),
            "NARWHAL_CONTROL_ENGINE": "n1",
            "no_proxy": "*",
        }
        Engine.status, Engine.path_seen = 200, []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.endpoint = f"http://127.0.0.1:{server.server_port}"
        self.dirs = self.root / "launch-dirs.json"
        self.launch("n1", {"endpoint": self.endpoint})

    def launch(self, iid: str, plan: dict, *, container: str | None = CID) -> Path:
        run = self.root / f"engine-launch-{iid}"
        run.mkdir()
        (run / "launch.json").write_text(json.dumps(plan))
        if container is not None:
            (run / "container.id").write_text(container + "\n")
        dirs = json.loads(self.dirs.read_text()) if self.dirs.exists() else {}
        self.dirs.write_text(json.dumps({**dirs, iid: str(run)}))
        return run

    def hook(self, *args: str, **env: str) -> tuple[int, str]:
        err = io.StringIO()
        with (
            patch.dict(os.environ, {**self.env, **env}),
            patch.object(engine_hook, "HEALTH_POLL_S", 0.05),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = engine_hook.main(["--launch-dirs", str(self.dirs), *args])
        return code, err.getvalue()

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_each_action_runs_the_runtime_command_on_the_recorded_container(self) -> None:
        for action, command in (
            ("pause", "pause"),
            ("resume", "unpause"),
            ("stop", "stop"),
            ("start", "start"),
        ):
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                code, err = self.hook(action, NARWHAL_CONTROL_ACTION=action)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.docker_calls()[0], f"{command} {CID}")

    def test_start_waits_for_each_backends_health_route(self) -> None:
        for name in backends.names():
            with self.subTest(backend=name):
                Engine.path_seen = []
                self.launch(name, {"endpoint": self.endpoint + "/", "engine": name})
                self.assertEqual(self.hook("start", NARWHAL_CONTROL_ENGINE=name), (0, ""))
                self.assertEqual(Engine.path_seen, [backends.load(name).dialect.health_path])
                self.assertEqual(
                    self.docker_calls()[-2:],
                    [f"start {CID}", f"inspect --format {{{{json .State}}}} {CID}"],
                )

    def test_start_runs_the_post_start_command_with_the_launch_directory(self) -> None:
        marker = self.root / "after-start"
        code, err = self.hook("start", "--", sys.executable, "-c", AFTER_START, str(marker), "0")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(marker.read_text(), str(self.root / "engine-launch-n1"))
        self.assertEqual(Engine.path_seen, ["/health"])

    def test_a_failed_post_start_command_fails_the_start(self) -> None:
        marker = self.root / "after-start"
        code, err = self.hook("start", "--", sys.executable, "-c", AFTER_START, str(marker), "3")
        self.assertEqual(code, 1)
        self.assertIn("post-start command exited 3", err)

    def test_start_fails_without_the_post_start_command_when_the_engine_stays_unhealthy(
        self,
    ) -> None:
        Engine.status = 503
        marker = self.root / "after-start"
        code, err = self.hook(
            "--health-timeout",
            "0.2",
            "start",
            "--",
            sys.executable,
            "-c",
            AFTER_START,
            str(marker),
            "0",
        )
        self.assertEqual(code, 1)
        self.assertIn("did not answer within 0.2s", err)
        self.assertFalse(marker.exists())

    def test_start_fails_when_the_container_stops_running(self) -> None:
        code, err = self.hook("start", DOCKER_RUNNING="0")
        self.assertEqual(code, 1)
        self.assertIn("stopped running while starting", err)
        self.assertEqual(Engine.path_seen, [])

    def test_a_runtime_failure_fails_the_action(self) -> None:
        code, err = self.hook("pause", DOCKER_FAIL="pause")
        self.assertEqual(code, 1)
        self.assertIn("docker pause exited 1: stub daemon refused", err)

    def test_refusals_run_no_runtime_command(self) -> None:
        self.launch("n2", {"endpoint": self.endpoint, "backend": "native"})
        self.launch("n3", {"endpoint": self.endpoint}, container=None)
        self.launch("n4", {"endpoint": self.endpoint}, container="not-a-container-id")
        dirs = json.loads(self.dirs.read_text())
        self.dirs.write_text(json.dumps({**dirs, "n5": "relative/engine-launch-n5"}))
        cases = [
            (
                ("stop",),
                {"NARWHAL_CONTROL_ENGINE": "n9"},
                "has no launch directory for engine 'n9'",
            ),
            (("stop",), {"NARWHAL_CONTROL_ENGINE": "n2"}, "is a native launch"),
            (("stop",), {"NARWHAL_CONTROL_ENGINE": "n3"}, "records no container"),
            (("stop",), {"NARWHAL_CONTROL_ENGINE": "n4"}, "does not hold a container ID"),
            (("stop",), {"NARWHAL_CONTROL_ENGINE": "n5"}, "must be an absolute path"),
            (("stop",), {"NARWHAL_CONTROL_ENGINE": ""}, "NARWHAL_CONTROL_ENGINE is not set"),
            (("stop",), {"NARWHAL_CONTROL_ACTION": "pause"}, "requested 'pause'"),
            (("pause", "--", "true"), {}, "post-start command applies only to start"),
        ]
        for args, env, reason in cases:
            with self.subTest(reason=reason):
                code, err = self.hook(*args, **env)
                self.assertEqual(code, 2)
                self.assertIn(reason, err)
        self.assertEqual(self.docker_calls(), [])

    def test_an_unreadable_map_refuses(self) -> None:
        self.dirs.write_text("[]")
        code, err = self.hook("stop")
        self.assertEqual(code, 2)
        self.assertIn("must map engine IDs to launch directory paths", err)


if __name__ == "__main__":
    unittest.main()
