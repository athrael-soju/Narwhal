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
        self.boot.write_text("a\n")
        self.reported = 1_000
        self.directory = self.root / f"narwhal-{os.geteuid()}"
        for patched in (
            patch.object(host_process, "BOOT_RECORDS", self.root),
            patch.object(host_process, "BOOT_ID", self.boot),
            patch.object(host_process, "_reported_boot_time", lambda: self.reported),
            patch.object(host_process, "_warned", False),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def records(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())

    def test_a_clock_correction_leaves_a_process_start_unchanged(self):
        started = host_process.native_start(os.getpid())
        self.assertGreater(started, 1_000)
        # The host now reports a boot two seconds later, as after a clock correction.
        self.reported = 1_002
        self.assertEqual(host_process.native_start(os.getpid()), started)
        self.assertEqual(host_process.boot_time(), 1_000)
        self.assertEqual(self.records(), ["boot-a"])
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)

    def test_a_new_boot_takes_its_own_time_and_drops_the_old_record(self):
        self.assertEqual(host_process.boot_time(), 1_000)
        self.boot.write_text("b\n")
        self.reported = 5_000
        self.assertEqual(host_process.boot_time(), 5_000)
        self.assertEqual(self.records(), ["boot-b"])

    def test_a_reader_that_loses_the_race_takes_the_record_of_the_winner(self):
        real_link = os.link

        def late(*args, **kwargs):
            # Another reader records the boot between this reader's check and its link.
            (self.directory / "boot-a").write_text("990")
            return real_link(*args, **kwargs)

        with patch.object(os, "link", side_effect=late):
            self.assertEqual(host_process.boot_time(), 990)
        self.assertEqual(self.records(), ["boot-a"])

    def test_a_record_that_holds_no_time_is_replaced(self):
        self.directory.mkdir(mode=0o700)
        for content in ("", "not a number", "-5"):
            (self.directory / "boot-a").write_text(content)
            self.assertEqual(host_process.boot_time(), 1_000)
            self.assertEqual((self.directory / "boot-a").read_text(), "1000")
        self.reported = 1_009
        self.assertEqual(host_process.boot_time(), 1_000)

    def test_a_file_system_with_no_hard_links_still_keeps_one_record(self):
        with patch.object(os, "link", side_effect=PermissionError("no hard links")):
            self.assertEqual(host_process.boot_time(), 1_000)
        self.reported = 1_004
        self.assertEqual(host_process.boot_time(), 1_000)
        self.assertEqual(self.records(), ["boot-a"])

    def test_an_abandoned_draft_is_removed_and_a_fresh_one_is_left(self):
        self.directory.mkdir(mode=0o700)
        old, fresh = self.directory / ".boot-a-old", self.directory / ".boot-a-fresh"
        for draft in (old, fresh):
            draft.write_text("1")
        os.utime(old, (0, 0))
        host_process.boot_time()
        self.assertEqual(self.records(), [".boot-a-fresh", "boot-a"])

    def test_a_directory_others_can_reach_is_left_alone_with_one_warning(self):
        planted = self.root / "planted"
        planted.mkdir()
        (planted / "boot-a").write_text("7")
        (planted / "boot-earlier").write_text("1")
        cases = {
            "a symlink to another directory": lambda: self.directory.symlink_to(planted),
            "a directory open to its group": lambda: self.directory.mkdir(mode=0o770),
            "a file": lambda: self.directory.write_text(""),
        }
        for name, prepare in cases.items():
            with self.subTest(name), self.assertLogs("narwhal.host", "WARNING") as logged:
                prepare()
                if self.directory.is_dir() and not self.directory.is_symlink():
                    os.chmod(self.directory, 0o770)
                    (self.directory / "boot-a").write_text("7")
                self.assertEqual(host_process.boot_time(), 1_000)
                self.assertEqual(host_process.boot_time(), 1_000)
                self.assertEqual(len(logged.output), 1)
                host_process._warned = False
            if self.directory.is_symlink() or self.directory.is_file():
                self.directory.unlink()
            else:
                (self.directory / "boot-a").unlink()
                self.directory.rmdir()
        self.assertEqual(sorted(p.name for p in planted.iterdir()), ["boot-a", "boot-earlier"])

    def test_a_directory_of_another_user_is_left_alone(self):
        self.directory.mkdir(mode=0o700)
        (self.directory / "boot-a").write_text("7")
        other = os.stat_result((0o40700, 0, 0, 0, os.geteuid() + 1, 0, 0, 0, 0, 0))
        with (
            patch.object(os, "fstat", return_value=other),
            self.assertLogs("narwhal.host", "WARNING"),
        ):
            self.assertEqual(host_process.boot_time(), 1_000)

    def test_a_record_that_is_a_symlink_is_never_read(self):
        self.directory.mkdir(mode=0o700)
        secret = self.root / "secret"
        secret.write_text("42")
        (self.directory / "boot-a").symlink_to(secret)
        with self.assertLogs("narwhal.host", "WARNING"):
            self.assertEqual(host_process.boot_time(), 1_000)

    def test_a_host_with_no_boot_id_reports_its_live_boot_time(self):
        self.boot.unlink()
        with self.assertLogs("narwhal.host", "WARNING"):
            self.assertEqual(host_process.boot_time(), 1_000)

    def test_a_process_that_exited_has_no_start(self):
        with self.assertRaises((FileNotFoundError, ProcessLookupError)):
            host_process.native_start(2**22 + 12345)
