"""Check drift probation, peer vetoes, recovery and evidence windows."""

import unittest

from narwhal.scheduling.health import DriftTracker


class HealthTests(unittest.TestCase):
    """One-second windows use two observations per engine."""

    def setUp(self):
        self.now = 0
        self.tracker = DriftTracker(
            clock=lambda: self.now,
            window_s=1,
            min_samples=2,
            probation_windows=2,
            evict_windows=3,
            recovery_windows=2,
        )

    def window(self, values):
        """Close a full window after recording each engine's residual twice."""
        for iid, residual in values.items():
            self.tracker.note(iid, residual)
            self.tracker.note(iid, residual)
        self.now += 1
        return self.tracker.tick()

    def test_sustained_drift_probation_evict_and_confirmed_removal(self):
        """Consecutive bad windows escalate and history clears after confirmed eviction."""
        self.assertEqual(self.window({"e": 1}), [])
        self.assertEqual(self.window({"e": 3}), [])
        self.assertEqual(self.window({"e": 3}), [("probation", "e")])
        self.assertEqual(self.tracker.probation_set(), {"e"})
        self.assertEqual(self.window({"e": 3}), [("evict", "e")])
        self.assertEqual(self.tracker.probation_set(), {"e"})
        self.tracker.evicted("e")
        self.assertEqual(self.tracker.probation_set(), set())
        self.assertIsNone(self.tracker.score("e"))

    def test_recovery_requires_consecutive_healthy_windows(self):
        """A transient healthy window resets the drift streak and recovery counts."""
        self.window({"e": 1})
        self.window({"e": 3})
        self.window({"e": 3})
        self.assertEqual(self.window({"e": 1}), [])
        self.assertEqual(self.window({"e": 1}), [("recover", "e")])
        self.assertEqual(self.tracker.probation_set(), set())

    def test_peer_majority_veto_preserves_outlier_detection(self):
        """A fleet-wide slowdown suppresses ordinary drift while a larger outlier escalates."""
        self.window({"a": 1, "b": 1, "c": 1})
        self.assertEqual(self.window({"a": 3, "b": 3, "c": 3}), [])
        self.assertEqual(self.window({"a": 3, "b": 3, "c": 3}), [])
        self.assertEqual(self.window({"a": 10, "b": 3, "c": 3}), [])
        self.assertEqual(self.window({"a": 10, "b": 3, "c": 3}), [("probation", "a")])

    def test_disabled_peer_veto_scores_each_engine(self):
        """A zero relative band makes probation depend on each engine's own history."""
        self.tracker.relative_band = 0
        self.window({"a": 1, "b": 1, "c": 1})
        self.window({"a": 3, "b": 3, "c": 3})
        self.assertEqual(
            set(self.window({"a": 3, "b": 3, "c": 3})),
            {("probation", "a"), ("probation", "b"), ("probation", "c")},
        )

    def test_sparse_windows_drop_old_samples_and_count_only_evidence(self):
        """A sparse window closes before later observations can fill its deficit."""
        self.tracker.note("e", 10)
        self.assertEqual(self.tracker.tick(), [])
        self.now = 1
        self.assertEqual(self.tracker.tick(), [])
        self.assertEqual(self.tracker.window_stats()["e"]["undersampled"], 1)
        self.now = 2
        self.tracker.tick()
        self.assertEqual(self.tracker.window_stats()["e"]["undersampled"], 1)
        self.window({"e": 1})
        self.assertEqual(self.tracker.score("e"), 1)

    def test_prefill_pause_preserves_probation_and_restarts_decode_window(self):
        """Prefill pauses keep the engine on probation."""
        self.window({"e": 1})
        self.window({"e": 3})
        self.window({"e": 3})
        self.tracker.pause_for_prefill("e")
        self.tracker.pause_for_prefill("e")
        self.assertEqual(self.tracker.probation_set(), {"e"})
        self.assertEqual(self.tracker.window_stats()["e"]["prefill_pauses"], 1)
        self.now += 100
        self.tracker.note("e", 1)
        self.assertFalse(self.tracker.window_stats()["e"]["prefill_paused"])
        self.assertEqual(self.tracker.tick(), [])
