"""Host steps, uploads, the SSH supervision runner and the host HTTP checks of `narwhal deploy`."""

import base64
import hashlib
import http.server
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar

from narwhal.deployment import host_http, remote
from narwhal.deployment.hosts import HOST_CAUSE, Host, RemoteError
from narwhal.deployment.role_env import write_environment

HOST = Host("node-1", "NARWHAL_NODE_1_SSH", None, ("engine-1",))


class LocalSSH:
    """Run host scripts in a local home directory, as `SSH.run` runs them on a host."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.logs = home / "logs"
        self.logs.mkdir()

    def log(self, host: Host) -> Path:
        return self.logs / f"{host.id}.log"

    def run(self, host, stage, operation, script, payload=None, reason=None):
        with payload.open("rb") if payload else open(os.devnull, "rb") as stream:
            result = subprocess.run(
                ["sh", "-c", script],
                cwd=self.home,
                stdin=stream,
                capture_output=True,
                env={"PATH": os.environ["PATH"], "HOME": str(self.home)},
                timeout=60,
            )
        if result.returncode:
            causes = [
                line.removeprefix(HOST_CAUSE)
                for line in result.stderr.decode().splitlines()
                if line.startswith(HOST_CAUSE)
            ]
            raise RemoteError(host.id, stage, operation, self.log(host), f": {causes[-1:]}")
        return result.stdout.decode().strip()


class StepTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.ssh = LocalSSH(self.home)
        deployment = self.home / "Narwhal-deploy" / "x"
        (deployment / "runs/deployment").mkdir(parents=True)
        (deployment / ".venv/bin").mkdir(parents=True)
        (deployment / ".venv/bin/activate").write_text("export ACTIVATED=1\n")
        write_environment(deployment / "runs/deployment/.env.engine-1", {"ROLE_VALUE": "a b"})
        self.out = self.home / "attempt"
        self.out.mkdir()

    def step(self, command, **fields):
        return remote.Step(
            HOST, "engine-probe", "probe", "Narwhal-deploy/x", "engine-1", command, **fields
        )

    def test_a_step_runs_in_the_role_environment_and_returns_its_files(self):
        command = (
            'mkdir -p work/probe && printf %s "$ROLE_VALUE $ACTIVATED" > work/probe/value.txt'
            " && echo done"
        )
        output = remote.run(
            self.ssh,
            self.step(command, workdir="runs/deploy/r/work", returns={"probe/": "work/probe"}),
            self.out,
        )
        self.assertEqual(output, "done")
        self.assertEqual((self.out / "probe/value.txt").read_text(), "a b 1")
        self.assertEqual(oct((self.out / "probe/value.txt").stat().st_mode & 0o777), "0o600")
        self.assertTrue((self.home / "Narwhal-deploy/x/runs/deploy/r/work").is_dir())

    def test_a_failed_step_names_its_last_line_and_keeps_its_evidence(self):
        command = "{ echo evidence > kept.json; echo first; echo 'the probe failed'; exit 3; }"
        with self.assertRaises(remote.StepFailed) as caught:
            remote.run(
                self.ssh,
                self.step(
                    command,
                    returns={"kept.json": "kept.json", "absent.json": "nope"},
                    on_failure="echo cleaned > cleanup.txt",
                ),
                self.out,
            )
        self.assertIn("probe exited 3: the probe failed", str(caught.exception))
        self.assertEqual((self.out / "kept.json").read_text(), "evidence\n")
        self.assertFalse((self.out / "absent.json").exists())
        self.assertTrue((self.home / "Narwhal-deploy/x/cleanup.txt").exists())

    def test_a_step_refuses_an_existing_working_directory(self):
        (self.home / "Narwhal-deploy/x/runs/deploy/r/work").mkdir(parents=True)
        with self.assertRaises(RemoteError):
            remote.run(self.ssh, self.step("true", workdir="runs/deploy/r/work"), self.out)

    def test_returned_files_with_unsafe_names_are_refused(self):
        data = base64.b64encode(b"x").decode()
        for name in ("../escape", "/absolute", "a/../../b"):
            with self.assertRaisesRegex(ValueError, "unsafe name"):
                remote.files(f"narwhal-file {name} {data}")

    def test_an_upload_arrives_checked_and_refuses_an_existing_file(self):
        source = self.home / "fleet.json"
        source.write_text("{}")
        remote.upload(self.ssh, HOST, "profile", source, "dir/sub/fleet.json")
        self.assertEqual((self.home / "dir/sub/fleet.json").read_text(), "{}")
        with self.assertRaises(RemoteError):
            remote.upload(self.ssh, HOST, "profile", source, "dir/sub/fleet.json")


class SSHRunnerTests(unittest.TestCase):
    def setUp(self):
        self.runner = remote.SSHRunner(LocalSSH(Path(tempfile.mkdtemp())), HOST, "sidecar-start")

    def test_status_output_and_input_come_back_as_from_a_local_run(self):
        result = self.runner(["sh", "-c", "cat; echo out; echo err >&2; exit 4"], input="in\n")
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (4, "in\nout\n", "err\n")
        )

    def test_a_missing_executable_exits_127(self):
        self.assertEqual(self.runner(["narwhal-no-such-command"]).returncode, 127)


class _Router(http.server.BaseHTTPRequestHandler):
    states: ClassVar[dict[str, str]] = {}
    readmitted: ClassVar[list] = []
    bodies: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/narwhal/lifecycle":
            self._send(200, {"engines": {k: {"state": v} for k, v in self.states.items()}})
        elif self.path == "/ready":
            self._send(503, {"status": "not_ready"})
        else:
            self._send(200, {"status": "ok"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.bodies.append((self.path, body))
        if self.path == "/narwhal/lifecycle/readmit":
            self.readmitted.append(body["engines"])
            for engine in body["engines"]:
                self.states[engine] = "active"
        self._send(200, {"ok": True})


class HostHTTPTests(unittest.TestCase):
    def setUp(self):
        _Router.states = {"n1": "active", "n2": "ejected"}
        _Router.readmitted = []
        _Router.bodies = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Router)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_http.main(list(argv))
        return code, remote.files(out.getvalue()), err.getvalue()

    def test_wait_passes_on_http_200_and_names_the_route_that_never_answers(self):
        self.assertEqual(self.call("wait", self.base + "/health", "2")[0], 0)
        code, _, err = self.call("wait", self.base + "/ready", "0.6")
        self.assertEqual(code, 1)
        self.assertIn(f"{HOST_CAUSE}{self.base}/ready did not answer HTTP 200", err)

    def test_responses_keep_every_route_and_fail_on_a_router_that_is_not_ready(self):
        code, files, err = self.call("responses", self.base, "--completion", '{"prompt": "x"}')
        self.assertEqual(code, 1)
        self.assertEqual(
            set(files),
            {"health.json", "ready.json", "state.json", "metrics.txt", "completion.json"},
        )
        self.assertIn("/ready answered HTTP 503", err)
        self.assertEqual(_Router.bodies, [("/v1/completions", {"prompt": "x"})])

    def test_readmit_readmits_only_engines_that_are_not_active(self):
        code, files, _ = self.call("readmit", self.base, "5", "n1", "n2")
        self.assertEqual(code, 0)
        self.assertEqual(_Router.readmitted, [["n2"]])
        record = json.loads(files["readmission.json"])
        self.assertEqual(record["lifecycle_after"]["engines"]["n2"]["state"], "active")

    def test_readmit_fails_when_an_engine_stays_inactive(self):
        _Router.states["n2"] = "blocked"
        original = _Router.do_POST
        _Router.do_POST = lambda self: self._send(409, {"error": "checks failed"})
        self.addCleanup(setattr, _Router, "do_POST", original)
        code, files, err = self.call("readmit", self.base, "0", "n2")
        self.assertEqual(code, 1)
        self.assertIn("engines n2 are not active after readmission", err)
        self.assertEqual(json.loads(files["readmission.json"])["readmitted"]["status"], 409)


class DigestTests(unittest.TestCase):
    def test_returned_files_round_trip(self):
        data = os.urandom(64)
        line = f"narwhal-file a/b.bin {base64.b64encode(data).decode()}"
        self.assertEqual(
            hashlib.sha256(remote.files(line)["a/b.bin"]).digest(), hashlib.sha256(data).digest()
        )


if __name__ == "__main__":
    unittest.main()
