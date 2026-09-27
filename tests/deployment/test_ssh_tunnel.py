"""Bind tunnel readiness and cleanup to the helper's own process tree."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import ssh_tunnel as tunnel
from narwhal.deployment.management_records import OperationError


class TunnelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.trust = Path(directory.name) / "known_hosts"
        self.trust.write_text("registered-host ssh-ed25519 fixture\n")
        self.trust.chmod(0o600)
        self.options = {
            "destination": "operator@registered-host",
            "password": None,
            "known_hosts": self.trust,
            "ports": ((18000, 8000),),
            "connect_timeout_s": 3,
            "deadline": time.monotonic() + 5,
        }

    def test_invalid_binding_and_expired_deadline_never_spawn(self):
        for changes in (
            {"destination": "-oProxyCommand=anything"},
            {"ports": ((18000, 8000), (18000, 9000))},
            {"ports": ((True, 8000),)},
            {"deadline": time.monotonic() - 1},
        ):
            with patch.object(tunnel.subprocess, "Popen") as spawn:
                with (
                    self.subTest(changes=changes),
                    self.assertRaises(OperationError),
                    tunnel.forward(**(self.options | changes)),
                ):
                    self.fail("Invalid tunnel became ready")
                spawn.assert_not_called()

    def test_owned_listener_readiness_and_cleanup_leave_unrelated_listener(self):
        unrelated = socket.socket()
        self.addCleanup(unrelated.close)
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        chosen = socket.socket()
        chosen.bind(("127.0.0.1", 0))
        port = chosen.getsockname()[1]
        chosen.close()
        original = subprocess.Popen
        children = []
        calls = []

        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertNotIn("fixture-password", " ".join(argv))
            self.assertTrue(any(f"/proc/{os.getpid()}/fd/" in value for value in argv))
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("ExitOnForwardFailure=yes", argv)
            self.assertEqual(os.pread(kwargs["pass_fds"][0], 100, 0), b"fixture-password\n")
            process = original(
                [
                    sys.executable,
                    "-c",
                    "import socket,time; s=socket.socket(); "
                    f"s.bind(('127.0.0.1',{port})); s.listen(); time.sleep(30)",
                ],
                **kwargs,
            )
            children.append(process)
            return process

        with (
            patch.object(tunnel.shutil, "which", side_effect=lambda name, **_: "/usr/bin/" + name),
            patch.object(tunnel.subprocess, "Popen", side_effect=spawn),
            tunnel.forward(
                **(self.options | {"password": "fixture-password", "ports": ((port, 8000),)})
            ) as receipt,
        ):
            self.assertTrue(tunnel._ports_owned(receipt["pid"], {port}))
            self.assertFalse(tunnel._ports_owned(receipt["pid"], {unrelated.getsockname()[1]}))
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(unrelated.getsockname()[0], "127.0.0.1")

    def test_an_unrelated_listener_cannot_satisfy_failed_tunnel_readiness(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        self.addCleanup(process.wait)
        with (
            patch.object(tunnel.subprocess, "Popen", return_value=process),
            self.assertRaises(OperationError) as caught,
            tunnel.forward(**self.options),
        ):
            self.fail("Exited SSH process became ready")
        self.assertEqual(caught.exception.code, "source_unavailable")
