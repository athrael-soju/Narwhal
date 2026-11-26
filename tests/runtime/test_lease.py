"""Check lease contention, clock boundaries and persisted fencing epochs."""

import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.contracts import LEASE, versioned
from narwhal.runtime.lease import FileLease, LeaseError


class LeaseTests(unittest.TestCase):
    """Two holders share a real lock file and independently controlled clocks."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "router.lease"
        self.wall = 100.0
        self.mono = 50.0
        self.a = self.lease("a")
        self.b = self.lease("b")

    def lease(self, holder):
        """Give a holder the shared file and the current test clocks."""
        return FileLease(
            self.path,
            holder,
            10,
            safety_margin_s=2,
            wall_clock=lambda: self.wall,
            monotonic_clock=lambda: self.mono,
            sleep=self.wait,
        )

    def wait(self, seconds):
        """Advance the monotonic clock, as a held lock makes a holder wait."""
        self.mono += seconds
        self.waits += 1
        if self.waits == self.release_after:
            self.holder_lock.close()

    def hold_lock(self, release_after=None):
        """Hold the coordination lock from another open file, as another router does."""
        self.waits = 0
        self.release_after = release_after
        self.holder_lock = self.a.lock_path.open("a+")
        self.addCleanup(self.holder_lock.close)
        fcntl.flock(self.holder_lock.fileno(), fcntl.LOCK_EX)

    def test_constructor_checks_lease_parameters(self):
        """Holder, TTL and safety margin failures identify the invalid setting."""
        for holder, ttl, margin in (("", 10, 0), ("a", 0, 0), ("a", 10, -1), ("a", 10, 10)):
            with self.subTest(holder=holder, ttl=ttl, margin=margin), self.assertRaises(ValueError):
                FileLease(self.path, holder, ttl, safety_margin_s=margin)

    def test_constructor_rejects_nonfinite_timings_before_creating_files(self):
        """Each timing must be finite before a holder can touch shared state."""
        for value in (float("nan"), float("inf"), float("-inf")):
            for field in ("ttl_s", "safety_margin_s"):
                options = {"ttl_s": 10, "safety_margin_s": 2, field: value}
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, "finite"),
                ):
                    FileLease(self.path, "a", **options)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_a_held_lock_delays_a_claim_until_it_is_released(self):
        """Attempts repeat while another holder has the lock, then the claim proceeds."""
        self.hold_lock(release_after=3)
        self.assertTrue(self.b.claim())
        self.assertEqual(self.waits, 3)
        self.assertEqual(self.b.read().holder, "b")

    def test_a_lock_held_past_the_wait_deadline_fails_the_claim(self):
        """A claim waits one validity period at most and reports the held lock."""
        self.hold_lock()
        with self.assertRaisesRegex(LeaseError, "stayed held past the wait deadline"):
            self.b.claim()
        self.assertFalse(self.b.owned)
        self.assertGreaterEqual(self.mono, 58)
        self.assertLess(self.mono, 58.1)

    def test_a_renewal_that_cannot_lock_before_its_deadline_fences(self):
        """The holder's own deadline bounds the wait, so a stalled lock fences it in time."""
        self.assertTrue(self.a.claim())
        self.mono = 55
        self.hold_lock()
        self.assertFalse(self.a.renew())
        self.assertFalse(self.a.valid())
        self.assertGreaterEqual(self.mono, 58)
        self.assertLess(self.mono, 58.1)

    def test_competing_holder_waits_until_expiry_and_advances_epoch(self):
        """Takeover at the expiry boundary fences renewal by the previous holder."""
        self.assertIsNone(self.a.read())
        self.assertTrue(self.a.claim())
        self.assertFalse(self.b.claim())
        self.assertEqual(self.a.epoch, 1)
        self.wall = 110
        self.assertTrue(self.b.claim())
        self.assertEqual(self.b.epoch, 2)
        self.assertFalse(self.a.renew())
        self.assertFalse(self.a.owned)
        self.a.release()
        self.assertEqual(self.b.read().holder, "b")

    def test_same_holder_reclaim_retains_epoch_until_expiry(self):
        """Reclaiming an expired lease increments the epoch."""
        self.assertTrue(self.a.claim())
        self.assertTrue(self.a.claim())
        self.assertEqual(self.a.epoch, 1)
        self.wall = 110
        self.assertTrue(self.a.claim())
        self.assertEqual(self.a.epoch, 2)

    def test_monotonic_deadline_fences_despite_wall_clock_rollback(self):
        """A wall-clock adjustment leaves the local safety deadline unchanged."""
        self.a.claim()
        self.wall = 1
        self.mono = 57.999
        self.assertTrue(self.a.valid())
        self.mono = 58
        self.assertFalse(self.a.valid())

    def test_renewal_extends_deadline_after_successful_write(self):
        """A persisted renewal buys a fresh local TTL minus the safety margin."""
        self.assertFalse(self.a.renew())
        self.a.claim()
        self.wall = 104
        self.mono = 54
        self.assertTrue(self.a.renew())
        self.assertEqual(self.a.read().expires_at, 114)
        self.mono = 61.999
        self.assertTrue(self.a.valid())
        self.mono = 62
        self.assertFalse(self.a.valid())

    def test_failed_renewal_preserves_the_last_successful_deadline(self):
        """A storage failure consumes the existing lease time budget."""
        self.a.claim()
        with patch("narwhal.runtime.lease.os.replace", side_effect=OSError("disk")):
            self.assertFalse(self.a.renew())
        self.assertEqual(self.a.read().expires_at, 110)
        self.mono = 58
        self.assertFalse(self.a.valid())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_release_expires_own_epoch_and_preserves_replacement(self):
        """Releasing a stale holder leaves the replacement record intact."""
        self.a.claim()
        self.a.release()
        self.assertFalse(self.a.owned)
        self.assertEqual(self.a.read().expires_at, 100)
        self.b.claim()
        before = self.path.read_bytes()
        self.a.release()
        self.assertEqual(self.path.read_bytes(), before)

    def test_renewal_rejects_lost_record_and_changed_epoch(self):
        """Missing ownership evidence and a replaced epoch fence the local holder."""
        for replacement in (
            None,
            versioned(
                LEASE,
                {
                    "epoch": 2,
                    "holder": "a",
                    "expires_at": 120,
                    "updated_at": 100,
                },
            ),
        ):
            with self.subTest(replacement=replacement):
                self.a.claim()
                if replacement is None:
                    self.path.unlink()
                else:
                    self.path.write_text(json.dumps(replacement))
                self.assertFalse(self.a.renew())
                self.assertFalse(self.a.owned)

    def test_malformed_records_and_field_types_fail_closed(self):
        """Corrupt lease documents prevent a new ownership claim."""
        body = {"epoch": 1, "holder": "a", "expires_at": 110, "updated_at": 100}
        for field, value in (
            ("epoch", True),
            ("epoch", 0),
            ("holder", ""),
            ("expires_at", True),
            ("updated_at", "100"),
            ("extra", 1),
        ):
            with self.subTest(field=field):
                self.path.write_text(json.dumps(versioned(LEASE, {**body, field: value})))
                with self.assertRaises(LeaseError):
                    self.a.claim()
                self.assertFalse(self.a.owned)
        for raw in ("{broken", json.dumps(versioned(LEASE, {"epoch": 1}))):
            self.path.write_text(raw)
            with self.assertRaises(LeaseError):
                self.a.read()

    def test_claim_write_failure_and_release_failure_clear_ownership(self):
        """Failed ownership writes preserve the previous bytes and clear local ownership."""
        self.a.claim()
        previous = self.path.read_bytes()
        with patch("narwhal.runtime.lease.os.replace", side_effect=OSError("disk")):
            with self.assertRaisesRegex(LeaseError, "cannot claim"):
                self.a.claim()
            self.assertFalse(self.a.owned)
        self.assertEqual(self.path.read_bytes(), previous)
        self.a.claim()
        with patch("narwhal.runtime.lease.os.replace", side_effect=OSError("disk")):
            self.a.release()
        self.assertFalse(self.a.valid())

    def test_nonfinite_timestamps_fail_read_claim_and_renew(self):
        """Invalid persisted timestamps preserve the shared record and block renewal."""
        for field in ("expires_at", "updated_at"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    self.path.unlink(missing_ok=True)
                    self.assertTrue(self.a.claim())
                    body = json.loads(self.path.read_text())
                    body[field] = value
                    self.path.write_text(json.dumps(body))
                    previous = self.path.read_bytes()
                    with self.assertRaisesRegex(LeaseError, f"{field}.*finite"):
                        self.a.read()
                    with self.assertRaisesRegex(LeaseError, f"{field}.*finite"):
                        self.b.claim()
                    self.assertFalse(self.b.owned)
                    self.assertFalse(self.a.renew())
                    self.assertEqual(self.path.read_bytes(), previous)
                    self.mono += 10
                    self.assertFalse(self.a.valid())
