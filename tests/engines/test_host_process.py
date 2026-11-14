import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.engines import host_process


class BootTimeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.boot = self.root / "boot_id"
        self.boot.write_text("boot-a\n")
        self.reported = 1_000
        for patched in (
            patch.object(host_process, "BOOT_RECORDS", self.root),
            patch.object(host_process, "BOOT_ID", self.boot),
            patch.object(host_process, "_reported_boot_time", lambda: self.reported),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def records(self) -> list[str]:
        return sorted(path.name for path in (self.root / f"narwhal-{os.geteuid()}").iterdir())

    def test_a_clock_correction_leaves_a_process_start_unchanged(self):
        started = host_process.native_start(os.getpid())
        self.assertGreater(started, 1_000)
        # The host now reports a boot two seconds later, as after a clock correction.
        self.reported = 1_002
        self.assertEqual(host_process.native_start(os.getpid()), started)
        self.assertEqual(host_process.boot_time(), 1_000)
        self.assertEqual(self.records(), ["boot-boot-a"])

    def test_a_new_boot_takes_its_own_time_and_drops_the_old_record(self):
        self.assertEqual(host_process.boot_time(), 1_000)
        self.boot.write_text("boot-b\n")
        self.reported = 5_000
        self.assertEqual(host_process.boot_time(), 5_000)
        self.assertEqual(self.records(), ["boot-boot-b"])

    def test_the_first_record_of_a_boot_wins(self):
        directory = self.root / f"narwhal-{os.geteuid()}"
        directory.mkdir(mode=0o700)
        (directory / "boot-boot-a").write_text("990")
        self.assertEqual(host_process.boot_time(), 990)
        (directory / "boot-boot-a").write_text("not a number")
        self.reported = 1_003
        # A record that cannot be read falls back to the host's report.
        self.assertEqual(host_process.boot_time(), 1_003)

    def test_a_directory_that_cannot_be_used_falls_back_to_the_reported_time(self):
        with patch.object(host_process, "BOOT_RECORDS", self.root / "file"):
            (self.root / "file").write_text("")
            self.assertEqual(host_process.boot_time(), 1_000)
        with patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            (self.root / f"narwhal-{os.geteuid()}").mkdir()
            self.assertEqual(host_process.boot_time(), 1_000)
            self.assertEqual(self.records(), [])

    def test_a_process_that_exited_has_no_start(self):
        with self.assertRaises((FileNotFoundError, ProcessLookupError)):
            host_process.native_start(2**22 + 12345)
