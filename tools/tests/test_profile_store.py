"""Check persisted profile contracts, scoped aggregation and measured-domain limits."""

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from narwhal.contracts import PROFILES, versioned
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from tools.tests.fixtures import profile


class ProfileStoreTests(unittest.TestCase):
    """A shared measured profile supplies the valid row for each mutation."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "profiles.json"

    def test_round_trip_scope_and_engine_set_difference(self):
        """Scoped means use each requested engine once and exclude stale rows."""
        store = ProfileStore(self.path)
        self.assertIsNone(store.mean_prefill_time(10))
        store.put(profile("a", ttft_b=1))
        store.put(profile("b", ttft_b=3))
        restored = ProfileStore(self.path)
        self.assertEqual(restored.engine_set_diff(["a", "c"]), (["c"], ["b"]))
        self.assertAlmostEqual(restored.mean_prefill_time(10), 20.01)
        self.assertAlmostEqual(restored.mean_prefill_time(10, ["a", "a", "missing"]), 10.01)
        self.assertIsNone(restored.mean_token_interval(10, iids=["missing"]))
        self.assertFalse(restored.covers_decode(1, 1, ["missing"]))
        self.assertEqual(restored.get("a"), store.get("a"))

    def test_profile_field_validation(self):
        """Rows reject type coercion, nonfinite costs and inconsistent measured bounds."""
        base = asdict(profile())
        for field, value in (
            ("iid", ""),
            ("ttft_a", True),
            ("ttft_b", float("inf")),
            ("ttft_c", -1),
            ("tpot_slope", 0),
            ("decode_cv_mape", None),
            ("decode_fit_mape", -1),
            ("decode_min_requests", 17),
            ("decode_max_requests", True),
            ("decode_min_kv_tokens", 100_001),
            ("kv_capacity_tokens", 10),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                Profile(**{**base, field: value})

    def test_store_rejects_duplicate_unknown_and_incomplete_rows(self):
        """A current profile store names malformed rows before exposing any profile."""
        base = asdict(profile())
        for rows, message in (
            ([base, base], "duplicate profile"),
            ([None], "must be an object"),
            ([{**base, "surprise": 1}], "unknown profile field"),
            ([{key: value for key, value in base.items() if key != "ttft_a"}], "missing profile"),
        ):
            self.path.write_text(json.dumps(versioned(PROFILES, {"profiles": rows})))
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                ProfileStore(self.path)

    def test_measured_domain_and_physical_capacity_bound_predictions(self):
        """Capacity stays inside both the SLO and measured request ceiling."""
        row = profile()
        self.assertTrue(row.covers_decode(1, 1))
        self.assertTrue(row.covers_decode(16, 100_000))
        self.assertFalse(row.covers_decode(17, 100))
        self.assertFalse(row.covers_decode(1, 100_001))
        self.assertEqual(row.max_tokens(1, batch_requests=17), 0)
        self.assertEqual(row.max_tokens(0.001, batch_requests=1), 0)
        self.assertLessEqual(row.max_tokens(100, batch_requests=1), 100_000)
