"""SSH access pins host keys, keeps passwords off argument lists and bounds every command."""

import json
import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment.hosts import (
    SSH,
    Host,
    RemoteError,
    RemoteTimeout,
    load_hosts,
    stage_limit,
)
from narwhal.deployment.launch_engine import checkpoint

PASSWORD = "synthetic-management-secret"


class FakeProcess:
    """Stands in for one OpenSSH process, recording how the client started it."""

    def __init__(self, test, result, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid = 0
        self.returncode, self.stdout, self.stderr = result
        test.started.append(self)
        if args[0] == "sshpass":
            self.password = os.read(kwargs["pass_fds"][0], 1024)
        self.stdin = kwargs["stdin"].read()

    def communicate(self, timeout=None):
        self.timeout = timeout
        return self.stdout, self.stderr


class SshTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.known_hosts = self.root / "known-hosts"
        self.known_hosts.write_text("synthetic.invalid ssh-ed25519 synthetic-public-key\n")
        self.hosts = [
            Host("node-1", "NODE_1_SSH", "NODE_1_PASSWORD", ("router", "engine-1")),
            Host("node-2", "NODE_2_SSH", None, ("engine-2",)),
        ]
        self.env = {
            "NODE_1_SSH": "operator@one.example.invalid",
            "NODE_1_PASSWORD": PASSWORD,
            "NODE_2_SSH": "operator@two.example.invalid",
            "NARWHAL_SSH_KNOWN_HOSTS": str(self.known_hosts),
        }
        self.started = []

    def popen(self, result=(0, b"", b"")):
        return patch(
            "narwhal.deployment.hosts.subprocess.Popen",
            side_effect=lambda args, **kwargs: FakeProcess(self, result, args, **kwargs),
        )

    def assert_private_logs(self):
        for path in (self.root / "logs").iterdir():
            self.assertNotIn(PASSWORD, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_every_command_pins_host_keys_and_passes_the_password_by_descriptor(self):
        payload = self.root / "payload"
        payload.write_bytes(b"synthetic-role-file")
        ssh = SSH(self.env, self.root / "logs")
        with self.popen():
            for host in self.hosts:
                ssh.run(host, "install", "test", "cat", payload=payload)
        password, key = self.started
        for process in self.started:
            args = process.args
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("UpdateHostKeys=no", args)
            self.assertIn("GlobalKnownHostsFile=/dev/null", args)
            self.assertIn(f"UserKnownHostsFile={self.known_hosts.resolve()}", args)
            self.assertNotIn("StrictHostKeyChecking=accept-new", args)
            self.assertNotIn(PASSWORD, args)
            self.assertNotIn("NODE_1_PASSWORD", process.kwargs["env"])
            self.assertFalse(any(name.startswith("NARWHAL_") for name in process.kwargs["env"]))
            self.assertTrue(process.kwargs["start_new_session"])
            self.assertEqual(process.stdin, payload.read_bytes())
            self.assertEqual(process.timeout, 1800)
        self.assertEqual(password.args[0], "sshpass")
        self.assertEqual(password.password, (PASSWORD + "\n").encode())
        self.assertNotIn("BatchMode=yes", password.args)
        self.assertEqual(key.args[0], "ssh")
        self.assertEqual(key.kwargs["pass_fds"], ())
        self.assertIn("BatchMode=yes", key.args)
        self.assert_private_logs()

    def test_a_missing_known_hosts_file_stops_before_any_login(self):
        for value in ("", str(self.root / "absent")):
            self.env["NARWHAL_SSH_KNOWN_HOSTS"] = value
            with (
                self.subTest(value=value),
                self.popen(),
                self.assertRaisesRegex(ValueError, "NARWHAL_SSH_KNOWN_HOSTS must name"),
            ):
                SSH(self.env, self.root / "logs")
        self.assertEqual(self.started, [])

    def test_an_unknown_or_changed_host_key_names_the_host(self):
        ssh = SSH(self.env, self.root / "logs")
        failure = (255, b"", b"Host key verification failed.\r\n")
        with (
            self.popen(failure),
            self.assertRaisesRegex(
                RemoteError,
                "^node-2: blocked at management access: the host key differs from "
                "NARWHAL_SSH_KNOWN_HOSTS or is missing from it; inspect ",
            ),
        ):
            ssh.run(self.hosts[1], "access", "management access", "hostname")

    def test_each_stage_bounds_its_remote_commands(self):
        self.assertEqual(stage_limit({}, "access"), 60)
        self.assertEqual(stage_limit({}, "discover"), 3600)
        self.assertEqual(stage_limit({"NARWHAL_STAGE_TIMEOUT_SECONDS": "90"}, "install"), 90)
        both = {
            "NARWHAL_STAGE_TIMEOUT_SECONDS": "90",
            "NARWHAL_STAGE_INSTALL_TIMEOUT_SECONDS": "2.5",
        }
        self.assertEqual(stage_limit(both, "install"), 2.5)
        for value in ("0", "-1", "inf", "nan", "soon"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "must be finite and positive"),
            ):
                stage_limit({"NARWHAL_STAGE_ACCESS_TIMEOUT_SECONDS": value}, "access")

    def test_a_hung_host_fails_its_stage_and_its_processes_are_killed(self):
        tools = self.root / "bin"
        tools.mkdir()
        children = self.root / "children"
        fake = tools / "ssh"
        fake.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {shlex.quote(str(children))}\nwait\n")
        fake.chmod(0o700)
        self.env["NARWHAL_STAGE_ACCESS_TIMEOUT_SECONDS"] = "0.5"
        with patch.dict(os.environ, {"PATH": f"{tools}:{os.environ['PATH']}"}):
            ssh = SSH(self.env, self.root / "logs")
            started = time.monotonic()
            with self.assertRaisesRegex(
                RemoteTimeout, "^node-2: management access exceeded the 0.5s access time limit"
            ) as raised:
                ssh.run(self.hosts[1], "access", "management access", "hostname")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(raised.exception.stage, "access")
        self.assertEqual(raised.exception.context["host"], "node-2")
        child = int(children.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the remote command's child survived the time limit")
        self.assertIn("timeout=0.5s", (self.root / "logs/node-2.log").read_text())

    def test_a_failed_checkpoint_hash_names_the_reason_the_command_reports(self):
        ssh = SSH(self.env, self.root / "logs")
        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        (self.root / "outside.json").write_text("{}")
        (model / "extra-out.json").symlink_to("../outside.json")
        payload = self.root / "payload"
        payload.write_text(json.dumps({"model_dir": str(model)}))
        script = "python3 -c " + shlex.quote(Path(checkpoint.__file__).read_text())
        remote = subprocess.run(
            ["sh", "-c", script], input=payload.read_bytes(), capture_output=True
        )
        self.assertEqual(remote.returncode, 1)
        host = self.hosts[0]
        for reason, message in (
            (
                checkpoint.FAILURE,
                f"^{host.id}: blocked at hash checkpoint files: checkpoint entry resolves "
                r"outside the model directory: extra-out\.json; inspect ",
            ),
            (None, f"^{host.id}: blocked at hash checkpoint files; inspect "),
        ):
            with (
                self.subTest(reason=reason),
                self.popen((remote.returncode, remote.stdout, b"ssh notice\n" + remote.stderr)),
                self.assertRaisesRegex(RemoteError, message),
            ):
                ssh.run(
                    host,
                    "discover",
                    "hash checkpoint files",
                    script,
                    payload=payload,
                    reason=reason,
                )

    def test_tunnel_reuses_inventory_authentication_and_opens_no_remote_shell(self):
        ssh = SSH(self.env, self.root / "logs")
        calls = []

        def execute(args, **kwargs):
            calls.append(args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("ExitOnForwardFailure=yes", args)
            self.assertIn("ServerAliveInterval=30", args)
            self.assertIn("-N", args)
            self.assertNotIn("-t", args)
            self.assertEqual(args[args.index("-L") + 1], "127.0.0.1:18000:127.0.0.1:8000")
            self.assertIn(args[-1], [self.env[h.ssh_env] for h in self.hosts])
            self.assertNotIn(PASSWORD, args)
            if args[0] == "sshpass":
                self.assertEqual(os.read(kwargs["pass_fds"][0], 1024), (PASSWORD + "\n").encode())
            return subprocess.CompletedProcess(args, 0)

        with patch("narwhal.deployment.hosts.subprocess.run", side_effect=execute):
            for host in self.hosts:
                ssh.tunnel(host, ["127.0.0.1:18000:127.0.0.1:8000"])
        self.assertEqual([args[0] for args in calls], ["sshpass", "ssh"])
        self.assert_private_logs()
        with (
            patch(
                "narwhal.deployment.hosts.subprocess.run",
                return_value=subprocess.CompletedProcess([], 255),
            ),
            self.assertRaisesRegex(RemoteError, "^node-1: blocked at service tunnel"),
        ):
            ssh.tunnel(self.hosts[0], ["127.0.0.1:18000:127.0.0.1:8000"])

    def test_role_shell_allocates_a_terminal(self):
        ssh = SSH(self.env, self.root / "logs")
        with patch(
            "narwhal.deployment.hosts.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            ssh.shell(self.hosts[1], "exec bash -l")
        args = run.call_args.args[0]
        self.assertIn("-t", args)
        self.assertNotIn("BatchMode=yes", args)
        self.assertEqual(args[-1], "sh -c " + shlex.quote("exec bash -l"))


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "NODE_1_SSH": "operator@one.example.invalid",
            "NODE_1_PASSWORD": PASSWORD,
            "NODE_2_SSH": "operator@two.example.invalid",
        }
        self.entries = [
            {
                "id": "node-1",
                "ssh_env": "NODE_1_SSH",
                "password_env": "NODE_1_PASSWORD",
                "roles": ["router", "engine-1"],
            },
            {"id": "node-2", "ssh_env": "NODE_2_SSH", "password_env": None, "roles": ["engine-2"]},
        ]

    def inventory(self, folder):
        path = Path(folder) / "hosts.json"
        path.write_text(json.dumps({"hosts": self.entries}))
        return path

    def test_roles_resolve_to_one_authentication_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = self.inventory(folder)
            self.assertNotIn(PASSWORD, inventory.read_text())
            hosts = load_hosts(inventory, self.env)
            router = next(h for h in hosts if "router" in h.roles)
            engine = next(h for h in hosts if "engine-1" in h.roles)
            self.assertIs(router, engine)
            self.assertEqual(len(hosts), 2)
            self.env["NODE_2_SSH"] = self.env["NODE_1_SSH"]
            with self.assertRaisesRegex(ValueError, "combine roles"):
                load_hosts(inventory, self.env)

    def test_duplicate_roles_and_missing_credentials_fail_locally(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.inventory(folder)
            del self.env["NODE_1_PASSWORD"]
            with self.assertRaisesRegex(ValueError, "NODE_1_PASSWORD is unset"):
                load_hosts(path, self.env)
            self.env["NODE_1_PASSWORD"] = "synthetic"
            self.entries[1]["roles"].append("router")
            path = self.inventory(folder)
            with self.assertRaisesRegex(ValueError, "one owner"):
                load_hosts(path, self.env)


if __name__ == "__main__":
    unittest.main()
