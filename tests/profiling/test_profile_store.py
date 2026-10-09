"""Check persisted profile contracts, scoped aggregation and measured-domain limits."""

import json
import random
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from narwhal.config.model import SharedDeviceAllocation
from narwhal.contracts import PROFILES, versioned
from narwhal.observability.journal import RunJournal
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Role
from tests.fixtures import fleet, profile


def colocated(iid: str, group: str, mix: tuple[int, int], role: str, **changes) -> Profile:
    """Return a role-mix variant of the shared fixture row."""
    return replace(
        profile(iid, **changes),
        colocated_group=group,
        colocated_target_role=role,
        colocated_prefill_engines=mix[0],
        colocated_decode_engines=mix[1],
        colocated_prefill_rps=1.0,
        colocated_decode_rps=1.0,
    )


class ProfileStoreTests(unittest.TestCase):
    """Each mutation starts from the valid measured row of the shared `profile` fixture."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "profiles.json"

    def test_round_trip_scope_and_engine_set_difference(self):
        """Scoped means use each requested engine once and exclude stale rows."""
        store = ProfileStore(self.path)
        self.assertIsNone(store.mean_token_interval(10))
        store.put(profile("a", tpot_intercept=0.001))
        store.put(profile("b", tpot_intercept=0.003))
        restored = ProfileStore(self.path)
        self.assertEqual(restored.engine_set_diff(["a", "c"]), (["c"], ["b"]))
        self.assertAlmostEqual(restored.mean_token_interval(10), 0.00201)
        self.assertAlmostEqual(
            restored.mean_token_interval(10, iids=["a", "a", "missing"]), 0.00101
        )
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

    def test_a_batch_below_the_measured_domain_prices_at_its_smallest_batch(self):
        """A TPOT budget under the smallest measured batch keeps a conservative capacity."""
        row = replace(profile(), decode_min_kv_tokens=600)
        bounded = row.decode_rps(0.006, 80, 64)
        self.assertAlmostEqual(bounded, 4 / (64 * row.token_interval(600, 4)))
        self.assertLess(bounded, 4 / (64 * row.token_interval(320, 4)))
        uncapped = row.decode_rps(1.0, 80, 64)
        self.assertAlmostEqual(uncapped, 16 / (64 * row.token_interval(16 * 80, 16)))

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
        self.assertEqual(row.prefill_time(64), row.prefill_time(128))
        self.assertLess(row.prefill_time(128), row.prefill_time(256))
        self.assertFalse(row.covers_prefill(2048))
        self.assertGreater(row.prefill_time(2048), row.prefill_time(1024))
        self.assertGreater(row.decode_rps(1, 256, 32), 0)
        self.assertAlmostEqual(row.decode_rps(1, 256, 32), row.decode_rps(1, 256, 16) / 2)
        self.assertEqual(replace(row, decode_min_output_tokens=16).decode_rps(1, 256, 8), 0)
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

    def test_role_mix_engine_index_keeps_engine_sets_and_their_order(self):
        """Engine sets derived from role-mix rows keep their members and iteration order."""
        rng = random.Random(11)
        store = ProfileStore(self.path)
        groups = {f"e{index}": f"gpu-{index % 3}" for index in range(40)}
        # e0 and e1 keep only standalone rows in two different groups.
        store.put(profile("e0"))
        store.put(profile("e1", tpot_intercept=0.002))
        for _ in range(160):
            iid = rng.choice(list(groups)[2:])
            cost = {"tpot_intercept": rng.uniform(0.001, 0.003)}
            if rng.random() < 0.3:
                store.put(profile(iid, **cost))
            else:
                mix = rng.choice(((1, 1), (2, 1)))
                role = rng.choice(("prefill", "decode"))
                store.put(colocated(iid, groups[iid], mix, role, **cost))
        for loaded in (store, ProfileStore(self.path)):
            loaded.bind_role_mix(groups, lambda group: (1, 1), lambda iid: Role.PREFILL)
            rebuilt = {key[0] for key in loaded._by_mix}
            self.assertEqual(list(loaded._mix_iids), list(rebuilt))
            stored = set(loaded._by_id) | rebuilt
            self.assertEqual(list(set(loaded._by_id) | loaded._mix_iids), list(stored))
            self.assertEqual(len(loaded), len(stored))
            configured = {f"e{index}" for index in range(0, 50, 2)}
            self.assertEqual(
                loaded.engine_set_diff(configured),
                (sorted(configured - stored), sorted(stored - configured)),
            )
            rows = [row for iid in dict.fromkeys(stored) if (row := loaded.get(iid)) is not None]
            self.assertEqual(loaded._rows_for(None), rows)
            self.assertGreater(len(rows), 1)
            self.assertEqual(
                loaded.mean_token_interval(10),
                sum(row.token_interval(10) for row in rows) / len(rows),
            )
            mixed = sorted(rebuilt, key=lambda iid: groups[iid])
            self.assertEqual(loaded.profiles_for_split([mixed[0], mixed[-1], "e0"], 1, 1), ())
            self.assertEqual(
                loaded.profiles_for_split(["e0", "e1"], 1, 1),
                (loaded._by_id["e0"], loaded._by_id["e1"]),
            )


class RoleMixBindingTests(unittest.IsolatedAsyncioTestCase):
    """The router reports each shared-device group's live role mix to the profile store."""

    async def test_role_mix_counts_each_group_from_live_engine_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder), engines=("e0", "e1", "e2", "e3", "e4", "e5"))
            devices = {"e0": "gpu-0", "e3": "gpu-0", "e4": "gpu-0", "e1": "gpu-1", "e5": "gpu-1"}
            cfg.engines = [
                replace(
                    spec,
                    shared_device=SharedDeviceAllocation(
                        devices[spec.iid], f"uuid-{devices[spec.iid]}", 0.3, 0.3
                    ),
                )
                if spec.iid in devices
                else spec
                for spec in cfg.engines
            ]
            router = NarwhalRouter(cfg, RunJournal(Path(folder) / "journal.jsonl"))
            self.addAsyncCleanup(router.engines.aclose)
        mix_for_group = router.profiles._mix_for_group
        assert mix_for_group is not None

        def counted(group: str) -> tuple[int, int]:
            members = [
                inst for iid, inst in router.monitor.instances.items() if devices.get(iid) == group
            ]
            return (
                sum(inst.role is Role.PREFILL for inst in members),
                sum(inst.role is Role.DECODE for inst in members),
            )

        self.assertEqual(
            [mix_for_group(group) for group in ("gpu-0", "gpu-1", "gpu-x")],
            [(1, 2), (1, 1), (0, 0)],
        )
        for iid, role in (("e3", Role.PREFILL), ("e1", Role.DECODE), ("e2", Role.DECODE)):
            router.monitor.instances[iid].role = role
            for group in ("gpu-0", "gpu-1", "gpu-x"):
                self.assertEqual(mix_for_group(group), counted(group))
        self.assertEqual([mix_for_group("gpu-0"), mix_for_group("gpu-1")], [(2, 1), (0, 2)])
