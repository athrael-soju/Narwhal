"""Validate failover timings before application construction or lease writes."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from narwhal import cli
from narwhal.config import FleetConfig
from narwhal.serving.app import create_app
from tests.fixtures import ROOT


class FailoverControlTests(unittest.TestCase):
    """CLI and direct callers share finite timing and interval constraints."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.lease_path = self.root / "lease.json"
        self.fleet_path = ROOT / "tests/data/fleet.json"
        self.cfg = FleetConfig.load(self.fleet_path)

    def test_cli_rejects_nonfinite_timings_before_application_construction(self):
        """Each invalid option exits with its name before constructing the router."""
        for flag in (
            "--lease-ttl",
            "--lease-renew-interval",
            "--lease-safety-margin",
            "--standby-probe-interval",
            "--standby-max-handoff-age",
        ):
            for value in ("nan", "inf", "-inf"):
                with self.subTest(flag=flag, value=value):
                    stderr = io.StringIO()
                    stdout = io.StringIO()
                    with (
                        patch.object(cli, "_port_in_use", return_value=None),
                        patch.object(cli, "create_app", wraps=create_app) as construct,
                        patch.object(cli.uvicorn, "run") as run,
                        redirect_stderr(stderr),
                        redirect_stdout(stdout),
                        self.assertRaises(SystemExit) as raised,
                    ):
                        cli.serve(
                            [
                                "--fleet",
                                str(self.fleet_path),
                                "--lease-path",
                                str(self.lease_path),
                                f"{flag}={value}",
                            ]
                        )
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(f"{flag} must be finite", stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), "")
                    construct.assert_not_called()
                    run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_direct_application_rejects_nonfinite_timings(self):
        """Application construction rejects all five timings before creating a lease."""
        for field in (
            "lease_ttl_s",
            "lease_renew_interval_s",
            "lease_safety_margin_s",
            "standby_probe_interval_s",
            "standby_max_handoff_age_s",
        ):
            for value in (float("nan"), float("inf"), float("-inf")):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, f"{field} must be finite"),
                ):
                    create_app(self.cfg, lease_path=self.lease_path, **{field: value})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_direct_application_preserves_timing_ranges(self):
        """Finite timings also obey positivity, takeover count and the TTL reserve."""
        for options, message in (
            ({"lease_ttl_s": 0}, "must exceed"),
            ({"lease_ttl_s": 2}, "must exceed"),
            ({"lease_renew_interval_s": 0}, "must exceed"),
            ({"lease_safety_margin_s": -1}, "must be nonnegative"),
            ({"standby_probe_interval_s": 0}, "must be positive"),
            ({"standby_probe_interval_s": -1}, "must be positive"),
            ({"standby_max_handoff_age_s": 0}, "must be positive"),
            ({"standby_takeover_after": 0}, "must be at least 1"),
        ):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                create_app(self.cfg, lease_path=self.lease_path, **options)

    def test_cli_constructs_application_with_finite_timings(self):
        """Finite fractional intervals and a zero safety margin reach the server."""
        with (
            patch.object(cli, "_port_in_use", return_value=None),
            patch.object(cli.uvicorn, "run") as run,
        ):
            result = cli.serve(
                [
                    "--fleet",
                    str(self.fleet_path),
                    "--lease-path",
                    str(self.lease_path),
                    "--lease-ttl=0.75",
                    "--lease-renew-interval=0.25",
                    "--lease-safety-margin=0",
                    "--standby-probe-interval=0.1",
                    "--standby-max-handoff-age=0.5",
                ]
            )
        self.assertEqual(result, 0)
        run.assert_called_once()
        lease = run.call_args.args[0].state.router.lease
        self.assertEqual(lease.ttl_s, 0.75)
        self.assertEqual(lease.safety_margin_s, 0)
