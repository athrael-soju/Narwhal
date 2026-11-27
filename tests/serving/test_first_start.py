"""Check how `--first-start-primary` picks the router's start as primary or standby."""

import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.cli import serve
from narwhal.runtime.lease import FileLease, LeaseRecord


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
        self.write_lease(expires_in=4)
        standby = self.start("--standby-of", "http://peer:8000/", "--first-start-primary")
        self.assertEqual(standby, "http://peer:8000")

    def test_a_lease_a_standby_could_still_take_over_makes_it_shadow_the_peer(self):
        # Expired 10 s ago: a standby's 30 s handoff may still take it over.
        self.write_lease(expires_in=-10)
        standby = self.start("--standby-of", "http://peer:8000", "--first-start-primary")
        self.assertEqual(standby, "http://peer:8000")

    def test_a_lease_no_standby_can_take_over_is_claimed_on_a_cold_start(self):
        # Both routers stopped long ago: no handoff is fresh enough for a takeover.
        self.write_lease(expires_in=-31)
        self.assertIsNone(self.start("--standby-of", "http://peer:8000", "--first-start-primary"))
        argv = ("--standby-of", "http://peer:8000", "--first-start-primary")
        standby = self.start(*argv, "--standby-max-handoff-age", "60")
        self.assertEqual(standby, "http://peer:8000")

    def test_a_malformed_lease_makes_it_shadow_the_peer(self):
        self.lease.write_text("{}")
        standby = self.start("--standby-of", "http://peer:8000", "--first-start-primary")
        self.assertEqual(standby, "http://peer:8000")

    def write_lease(self, expires_in: float) -> None:
        now = time.time()
        record = LeaseRecord(3, "peer:token", now + expires_in, now + expires_in - 5)
        FileLease(self.lease, "peer:token", 5)._write_unlocked(record)

    def test_without_its_witness_the_first_start_shadows_the_peer(self):
        # An unmounted lease directory holds neither the lease nor the witness; the router
        # must not take the absent lease for a first start.
        witness = self.lease.with_name(".narwhal-lease-witness")
        argv = ("--standby-of", "http://peer:8000", "--first-start-primary")
        standby = self.start(*argv, "--lease-witness", str(witness))
        self.assertEqual(standby, "http://peer:8000")
        witness.write_text("narwhal lease directory\n")
        self.assertIsNone(self.start(*argv, "--lease-witness", str(witness)))

    def test_the_flag_needs_a_peer(self):
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            self.start("--first-start-primary")
        self.assertIn("--first-start-primary requires --standby-of", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
