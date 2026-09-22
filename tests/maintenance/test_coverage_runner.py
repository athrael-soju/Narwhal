"""Check coverage checkpoint failures and the CPU command inventory."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.coverage_support import ha
from tools.maintenance.run_coverage import verify_checkpoints


class CoverageRunnerTests(unittest.TestCase):
    """Instrumentation failures must fail measurement before reports are accepted."""

    def test_missing_kill_acknowledgement_fails_measurement(self):
        """A completed suite still needs evidence from its deliberately killed primary."""
        with (
            tempfile.TemporaryDirectory() as folder,
            self.assertRaisesRegex(RuntimeError, "pre-kill checkpoint"),
        ):
            verify_checkpoints(Path(folder))

    def test_checkpoint_requires_the_process_to_save_its_shard(self):
        """The wrapper records the acknowledgement before returning to SIGKILL."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = SimpleNamespace(pid=123, poll=lambda: None)

            def signal_saved(pid, signum):
                (root / "saved-123.json").write_text(json.dumps({"data_file": "fixture"}))

            with (
                patch.dict("os.environ", {"NARWHAL_COVERAGE_DIR": folder}),
                patch.object(ha.os, "kill", side_effect=signal_saved) as send,
            ):
                ha.checkpoint_before_kill(process)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(
                (root / "killed-123.json").read_bytes(), (root / "saved-123.json").read_bytes()
            )

    def test_checkpoint_timeout_prevents_unmeasured_kill(self):
        """An unresponsive primary produces an instrumentation failure."""
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict("os.environ", {"NARWHAL_COVERAGE_DIR": folder}),
            patch.object(ha.os, "kill"),
            self.assertRaisesRegex(RuntimeError, "checkpoint failed"),
        ):
            ha.checkpoint_before_kill(SimpleNamespace(pid=123, poll=lambda: None), timeout_s=0)

    def test_wrapper_keeps_original_kill_and_graceful_stop_semantics(self):
        """Only the forced-stop path requests a checkpoint before the original drill stop."""
        process = SimpleNamespace(pid=123, poll=lambda: None)
        original = Mock()

        def drill(argv):
            ha.ha_failover._stop(process, kill=False)
            ha.ha_failover._stop(process, kill=True)
            return 0

        with (
            patch.object(ha.ha_failover, "_stop", original),
            patch.object(ha.ha_failover, "main", side_effect=drill),
            patch.object(ha, "checkpoint_before_kill") as checkpoint,
        ):
            self.assertEqual(ha.main([]), 0)
        checkpoint.assert_called_once_with(process)
        self.assertEqual(original.call_count, 2)
        self.assertTrue(original.call_args.kwargs["kill"])
