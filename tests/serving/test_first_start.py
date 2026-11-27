"""Check how `--first-start-primary` picks the router's start as primary or standby."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.cli import serve


class FirstStartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lease = Path(directory.name) / "lease.json"

    def start(self, *extra: str):
        argv = ["--fleet", "unused", "--port", "0", "--lease-path", str(self.lease), *extra]
        with (
            patch(
                "narwhal.cli.FleetConfig.load",
                return_value=SimpleNamespace(graceful_timeout_s=1, max_connections=8, resume=False),
            ),
            patch("narwhal.cli.create_app") as app,
            patch("narwhal.cli.YieldingServer"),
        ):
            self.assertEqual(serve(argv), 0)
        return app.call_args.kwargs["standby_of"]

    def test_the_first_start_finds_no_lease_and_starts_as_the_primary(self):
        self.assertIsNone(self.start("--standby-of", "http://peer:8000", "--first-start-primary"))

    def test_a_later_start_finds_the_lease_and_shadows_the_peer(self):
        self.lease.write_text("{}")
        standby = self.start("--standby-of", "http://peer:8000/", "--first-start-primary")
        self.assertEqual(standby, "http://peer:8000")

    def test_the_flag_needs_a_peer(self):
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            self.start("--first-start-primary")
        self.assertIn("--first-start-primary requires --standby-of", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
