"""Check operator command validation before profiling starts."""

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import AsyncMock, patch

from narwhal.profiling import probe
from tests.fixtures import ROOT


class OperatorCliTests(unittest.TestCase):
    """Profiling options reject sweeps that cannot identify both model axes."""

    def test_profile_cli_rejects_unidentifiable_sweeps_before_measurement(self):
        """Sweep validation checks axis variation, positive lengths and enough token gaps."""
        with patch.object(probe, "run", AsyncMock()) as run:
            for options in (
                ["--prefill-lens", "1,2"],
                ["--decode-concurrency", "1"],
                ["--decode-input-lens", "10"],
                ["--decode-tokens", "2"],
                ["--prefill-lens", "0,1,2"],
                ["--prefill-repeats", "0"],
            ):
                with (
                    self.subTest(options=options),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    probe.main(["--fleet", str(ROOT / "config/fleet.stub.json"), *options])
            run.assert_not_awaited()
