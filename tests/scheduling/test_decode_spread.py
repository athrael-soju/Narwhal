from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from dataclasses import replace

from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.types import Phase, Request, Role
from tests.fixtures import role_scheduler


def fleet(folder: str) -> GlobalScheduler:
    roles = {"p1": Role.PREFILL, "d1": Role.DECODE, "d2": Role.DECODE, "d3": Role.DECODE}
    scheduler = role_scheduler(folder, roles)
    scheduler.availability.roles_bound = True
    # As on a fleet, the measured KV domain binds well before the TPOT fit.
    for iid in ("d1", "d2", "d3"):
        profile = scheduler.profiles.get(iid)
        assert profile is not None
        scheduler.profiles.put(replace(profile, tpot_request_slope=1e-5, tpot_slope=1e-8))
    return scheduler


def scale_headroom(scheduler: GlobalScheduler, iid: str, factor: float) -> None:
    profile = scheduler.profiles.get(iid)
    assert profile is not None and profile.decode_max_kv_tokens is not None
    scheduler.profiles.put(
        replace(profile, decode_max_kv_tokens=int(profile.decode_max_kv_tokens * factor))
    )


def place(scheduler: GlobalScheduler, legs: int, length: int = 600) -> Counter[str]:
    placed: Counter[str] = Counter()
    for index in range(legs):
        leg = Request(f"r{index}", length, phase=Phase.DECODE, prefill_instance="p1")
        chosen = scheduler.schedule(leg)
        chosen.decode[leg.rid] = leg
        placed[chosen.iid] += 1
    return placed


class DecodeSpreadTests(unittest.TestCase):
    def test_profiling_noise_between_engines_does_not_pack_decode_legs(self):
        with tempfile.TemporaryDirectory() as folder:
            scheduler = fleet(folder)
            # Engines on the same hardware differ in profiled headroom by a few percent.
            scale_headroom(scheduler, "d2", 0.98)
            scale_headroom(scheduler, "d3", 0.97)
            placed = place(scheduler, 12)
        self.assertEqual(set(placed), {"d1", "d2", "d3"})
        self.assertLessEqual(max(placed.values()) - min(placed.values()), 1, placed)

    def test_decode_legs_fill_engines_in_proportion_to_their_headroom(self):
        with tempfile.TemporaryDirectory() as folder:
            scheduler = fleet(folder)
            scale_headroom(scheduler, "d2", 0.5)
            scale_headroom(scheduler, "d3", 0.5)
            placed = place(scheduler, 16)
        self.assertEqual(placed["d1"], 8, placed)
        self.assertEqual(placed["d2"] + placed["d3"], 8, placed)


if __name__ == "__main__":
    unittest.main()
