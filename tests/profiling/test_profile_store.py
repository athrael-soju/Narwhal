"""Check persisted profile contracts, scoped aggregation and measured-domain limits."""

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from narwhal.contracts import PROFILES, versioned
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.types import Role
from tests.fixtures import profile


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
            ("tpot_slope", -0.001),
            ("decode_cv_mape", None),
            ("decode_fit_mape", -1),
            ("decode_min_requests", 17),
            ("decode_max_requests", True),
            ("decode_min_kv_tokens", 100_001),
            ("kv_capacity_tokens", 10),
            ("generation_digest", "sha256:invalid"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                Profile(**{**base, field: value})

    def test_flat_decode_fit_stays_inside_measured_request_and_kv_limits(self):
        flat = profile(tpot_slope=0, tpot_request_slope=0, tpot_intercept=0.013)
        self.assertEqual(flat.max_tokens(0.125), 100_000)
        self.assertEqual(flat.decode_request_limit(10_000), 10)
        with self.assertRaisesRegex(ValueError, "zero tpot_slope requires measured decode bounds"):
            profile(tpot_slope=0, decode_max_requests=None)

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

    def test_workload_domain_and_colocated_load_are_persisted(self):
        row = replace(
            profile(),
            prefill_min_tokens=128,
            prefill_max_tokens=1024,
            decode_min_output_tokens=1,
            decode_max_output_tokens=16,
            colocated_group="gpu-0",
            colocated_target_role="prefill",
            colocated_prefill_engines=2,
            colocated_decode_engines=1,
            colocated_prefill_rps=3.0,
            colocated_decode_rps=1.0,
        )
        store = ProfileStore(self.path)
        store.put(row)
        restored = ProfileStore(self.path)
        restored.bind_role_mix({row.iid: "gpu-0"}, lambda group: (2, 1), lambda iid: Role.PREFILL)
        self.assertEqual(restored.get(row.iid), row)
        self.assertIsNone(restored.mean_prefill_time(64))
        self.assertIsNotNone(restored.mean_prefill_time(256))
        self.assertEqual(row.decode_rps(1, 256, 32), 0)
        with self.assertRaisesRegex(ValueError, "colocated role mix"):
            replace(row, colocated_decode_rps=None)

    def test_colocated_variants_require_the_exact_role_mix(self):
        base = profile("e0", ttft_b=0.001)
        one_prefill = replace(
            base,
            colocated_group="gpu-0",
            colocated_target_role="prefill",
            colocated_prefill_engines=1,
            colocated_decode_engines=2,
            colocated_prefill_rps=1.0,
            colocated_decode_rps=2.0,
        )
        two_prefill = replace(
            one_prefill,
            ttft_b=0.003,
            colocated_prefill_engines=2,
            colocated_decode_engines=1,
        )
        store = ProfileStore(self.path)
        store.put(base)
        store.put(one_prefill)
        store.put(two_prefill)
        restored = ProfileStore(self.path)
        mix = (1, 2)
        restored.bind_role_mix({"e0": "gpu-0"}, lambda group: mix, lambda iid: Role.PREFILL)
        self.assertEqual(restored.get("e0"), one_prefill)
        self.assertEqual(restored.profiles_for_split(["e0"], 2, 1), (two_prefill,))
        self.assertEqual(restored.profiles_for_split(["e0"], 3, 0), ())
        mix = (2, 1)
        self.assertEqual(restored.get("e0"), two_prefill)
