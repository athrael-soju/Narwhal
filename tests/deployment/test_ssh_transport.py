"""Exercise bounded SSH framing without contacting a remote host."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.deployment import ssh_transport as transport
from narwhal.deployment import ssh_worker
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.ssh_settings import SSHHost, SSHSettings


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trust = self.root / "known_hosts"
        self.trust.write_text("fixture trust")
        self.trust.chmod(0o600)
        self.fd = os.open(self.trust, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)
        self.script = self.root / "ssh"

    def exchange(self, code, timeout=2):
        self.script.write_text(f"#!{sys.executable}\n" + code)
        self.script.chmod(0o700)
        document = {
            "known_hosts_fd": self.fd,
            "connect_timeout_s": 1,
            "destination": "fixture.invalid",
            "worker_source": "pass",
            "request": {"command": "probe", "kind": "inventory", "parameters": {}},
        }
        with patch.object(transport.shutil, "which", return_value=str(self.script)):
            return transport._exchange(document, time.monotonic() + timeout)

    def test_fixed_argv_pins_trust_and_does_not_inherit_process_controls(self):
        code = (
            "import json,os,select,sys; request=json.loads(sys.stdin.readline()); "
            "print(json.dumps({'argv':sys.argv[1:], 'request':request, "
            "'held_open':not select.select([0],[],[],0)[0], "
            "'injected':os.environ.get('LD_PRELOAD')}))"
        )
        with patch.dict(os.environ, {"LD_PRELOAD": "private-placeholder"}):
            value = json.loads(self.exchange(code))
        self.assertIn("StrictHostKeyChecking=yes", value["argv"])
        self.assertIn(f"UserKnownHostsFile=/proc/{os.getpid()}/fd/{self.fd}", value["argv"])
        self.assertEqual(value["argv"][-2], "fixture.invalid")
        self.assertEqual(value["request"]["kind"], "inventory")
        self.assertTrue(value["held_open"])
        self.assertIsNone(value["injected"])

    def test_ssh_failure_never_asserts_remote_absence_or_exposes_stderr(self):
        with self.assertRaises(OperationError) as raised:
            self.exchange("import sys; sys.stderr.write('private diagnostic'); sys.exit(255)")
        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertIn("remote effects remain unknown", raised.exception.message)
        self.assertNotIn("private", raised.exception.message)

    def test_timeout_and_output_limits_stop_the_local_process(self):
        for code, expected in (
            ("import time; time.sleep(30)", "stage_timeout"),
            ("import sys; sys.stdout.write('x'*2200000)", "source_truncated"),
            ("import sys; sys.stderr.write('x'*70000)", "source_truncated"),
        ):
            started = time.monotonic()
            with self.subTest(expected=expected), self.assertRaises(OperationError) as raised:
                self.exchange(code, timeout=0.5)
            self.assertEqual(raised.exception.code, expected)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_read_only_transport_rejects_mutation_before_ssh(self):
        value = SSHSettings.model_validate(
            {
                "schema": "narwhal.ssh-settings",
                "schema_version": 1,
                "source_root": str(self.root),
                "source_commit": "a" * 40,
                "hosts_path": str(self.root / "hosts.json"),
                "launch_path": str(self.root / "launch.json"),
                "known_hosts_path": str(self.trust),
                "remote_root": "/tmp/fixture",
            }
        )
        context = SimpleNamespace(
            read_only=True, deadline=time.monotonic() + 2, operation_deadline=time.monotonic() + 2
        )
        host = SSHHost.model_validate(
            {"id": "one", "ssh_env": "ONE_SSH", "roles": ("router", "engine-1")}
        )
        client = transport.SSHTransport(context, value, (host,), {"ONE_SSH": "fixture.invalid"})
        with patch.object(transport, "_exchange") as exchange, self.assertRaises(OperationError):
            client.submit("one", job_id="not-used")
        exchange.assert_not_called()

    def test_standalone_worker_bootstrap_loads_file_protocol(self):
        result = subprocess.run(
            [sys.executable, "-c", transport._worker_source()],
            input=json.dumps(
                {"op": "read_file", "root": str(self.root), "path": "outside", "max_bytes": 1}
            ).encode(),
            capture_output=True,
            timeout=2,
            cwd=self.root,
        )
        value = json.loads(result.stdout)
        self.assertFalse(value["ok"])
        self.assertNotEqual(value["error"]["code"], "prerequisite_failed")
        self.assertEqual(result.stderr, b"")


class InspectionExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_reaps_the_owned_local_ssh_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trust = root / "known_hosts"
            trust.write_text("fixture")
            trust.chmod(0o600)
            pidfile = root / "pid"
            script = root / "ssh"
            script.write_text(
                f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
                f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n"
            )
            script.chmod(0o700)
            settings = SSHSettings.model_validate(
                {
                    "schema": "narwhal.ssh-settings",
                    "schema_version": 1,
                    "source_root": str(root),
                    "source_commit": "a" * 40,
                    "hosts_path": str(root / "hosts.json"),
                    "launch_path": str(root / "launch.json"),
                    "known_hosts_path": str(trust),
                    "remote_root": "/private/remote",
                }
            )
            host = SSHHost.model_validate(
                {"id": "one", "ssh_env": "TEST_SSH", "roles": ("router", "engine-1")}
            )
            with patch.object(transport.shutil, "which", return_value=str(script)):
                task = asyncio.create_task(
                    transport.inspect_rpc(
                        settings,
                        host,
                        {"TEST_SSH": "fixture.invalid"},
                        {"command": "probe", "kind": "inventory", "parameters": {}},
                        deadline=time.monotonic() + 10,
                    )
                )
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(pidfile.exists())
                pid = int(pidfile.read_text())
                started = time.monotonic()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertLess(time.monotonic() - started, 1.5)
            observed = ssh_worker._proc(pid)
            self.assertTrue(observed is None or observed[0] in {"Z", "X"})
