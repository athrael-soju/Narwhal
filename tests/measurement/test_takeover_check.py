import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.measurement import takeover_check as check

SHIPPED = check.SHIPPED_CONFIG.read_text()


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", check.DEFAULT_IMAGE],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class ShippedConfigTests(unittest.TestCase):
    """The timings that docs/operate/01-Start-Routers.md states for the shipped config."""

    def setting(self, pattern: str) -> str:
        match = re.search(pattern, SHIPPED, re.MULTILINE)
        self.assertIsNotNone(match, pattern)
        return match[1]

    def test_a_router_leaves_rotation_at_its_first_failed_check(self):
        servers = self.setting(r"^\s*default-server (.+)$").split()
        options = dict(zip(servers[1::2], servers[2::2], strict=False))
        self.assertEqual(servers[0], "check")
        self.assertEqual(options["inter"], "200ms")
        self.assertEqual(options["fall"], "1")
        self.assertEqual(options["rise"], "1")
        self.assertEqual(self.setting(r"^\s*timeout check\s+(\S+)$"), "1s")

    def test_requests_wait_on_a_refusing_backup_while_no_router_is_in_rotation(self):
        hold = self.setting(r"^\s*server hold (.+)$").split()
        self.assertEqual(hold[0], "127.0.0.1:1")
        self.assertIn("backup", hold)
        # A health check would mark the refusing address down, and HAProxy would answer 503 at once.
        self.assertIn("no-check", hold)
        # Each refused connection is retried after min(timeout connect, 1 s), 10 times.
        self.assertEqual(self.setting(r"^\s*timeout connect\s+(\S+)$"), "1s")
        self.assertEqual(self.setting(r"^\s*retries\s+(\d+)$"), "10")
        self.assertEqual(self.setting(r"^\s*option redispatch\s+(\d+)$"), "1")

    def test_render_binds_local_ports_and_logs_requests(self):
        rendered = check.render_config(SHIPPED, 18000, {"router-a": 18001, "router-b": 18002})
        self.assertIn("server router-a 127.0.0.1:18001", rendered)
        self.assertIn("server router-b 127.0.0.1:18002", rendered)
        self.assertIn("bind 127.0.0.1:18000", rendered)
        self.assertIn("defaults\n    log global\n    option httplog", rendered)
        self.assertNotIn("ADDRESS", rendered)

    def test_render_refuses_a_config_without_the_router_placeholders(self):
        with self.assertRaisesRegex(ValueError, "ROUTER_A_ADDRESS"):
            check.render_config(SHIPPED.replace("ROUTER_A_ADDRESS", "a"), 1, {"router-a": 2})


@unittest.skipUnless(_image_present(), f"needs Docker with {check.DEFAULT_IMAGE} pulled")
class ImmediateRestartTakeoverTests(unittest.TestCase):
    def test_a_sigterm_takeover_with_an_immediate_restart_returns_no_client_503(self):
        # 12 failed probes hold the new holder's readiness to about 3 s after the SIGTERM, so
        # the restarted router answers /ready with 503 for about 2 s before the holder is ready.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            argv = [
                "--out",
                str(out),
                "--takeovers",
                "1",
                "--takeover-after",
                "12",
                "--rate",
                "4",
                "--warmup",
                "2",
                "--interval",
                "4",
            ]
            with redirect_stdout(io.StringIO()):
                check.main(argv)
            report = json.loads((out / "report.json").read_text())
        (takeover,) = report["takeovers"]
        self.assertGreater(takeover["holder_ready_s"], takeover["restarted_answered_s"])
        self.assertGreater(takeover["holder_ready_s"], 2.0)
        self.assertEqual(report["statuses"].get("503", 0), 0, report["errors"])
