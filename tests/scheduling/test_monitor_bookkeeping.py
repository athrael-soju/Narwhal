"""Pin the monitor's gap, ratio and correction maths through resident and profile changes."""

import tempfile
import unittest
from pathlib import Path

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class MonitorMathTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = ProfileStore(Path(folder.name) / "profiles.json", load=False)
        self.store.put(profile("d0"))
        self.store.put(profile("p0"))
        self.clock = Clock()
        self.monitor = InstanceMonitor(self.clock, self.store)
        self.monitor.add(Instance("d0", "http://d0", Role.DECODE))
        self.monitor.add(Instance("p0", "http://p0", Role.PREFILL))

    def decode(self, rid: str, input_len: int) -> Request:
        """Prefill `rid` on p0 and move it to d0 at the current time."""
        request = Request(rid, input_len)
        self.monitor.dispatched("p0", request)
        self.monitor.first_token("p0", rid)
        request.phase = Phase.DECODE
        self.monitor.dispatched("d0", request)
        return request

    def expected(self) -> float:
        inst = self.monitor.instances["d0"]
        tokens = sum(r.input_len + r.output_len for r in inst.decode.values())
        return self.store.get("d0").token_interval(tokens, len(inst.decode))

    def test_gaps_ratios_and_correction_follow_resident_tokens(self):
        a = self.decode("a", 1000)
        b = self.decode("b", 3000)
        gaps, ratios = [], []
        for step in range(1, 13):
            self.clock.now = step * 0.05
            rid = "a" if step % 2 else "b"
            started = rid in self.monitor._decode_started
            expected = self.expected()
            self.monitor.output_token("d0", rid)
            if started:
                gap = 0.1
                gaps.append(gap)
                ratios.append(gap / expected)
        self.assertEqual((a.output_len, b.output_len), (6, 6))
        self.assertAlmostEqual(self.monitor.current_token_interval("d0"), sum(gaps) / len(gaps))
        self.assertTrue(self.monitor.decode_profile_eligible("d0"))
        self.monitor.roll_interval()
        self.assertAlmostEqual(self.monitor.mean_token_interval("d0"), sum(gaps) / len(gaps))
        observed = max(0.5, min(2.0, sum(ratios) / len(ratios)))
        self.assertAlmostEqual(self.monitor.decode_correction("d0"), 1.0 + 0.2 * (observed - 1.0))
        self.assertEqual(self.monitor.current_token_interval("d0"), 0.0)

    def test_a_profile_change_reprices_the_next_gap(self):
        self.decode("a", 1000)
        self.clock.now = 0.1
        self.monitor.output_token("d0", "a")
        self.store.put(profile("d0", tpot_intercept=0.05))
        self.clock.now = 0.2
        expected = self.expected()
        self.monitor.output_token("d0", "a")
        window = self.monitor._windows["d0"]
        self.assertAlmostEqual(window.ratio_total, 0.1 / expected)

    def test_prefill_overlap_blocks_correction_and_eligibility(self):
        self.decode("a", 1000)
        self.clock.now = 0.05
        self.monitor.output_token("d0", "a")
        mixed = Request("m", 200)
        self.monitor.dispatched("d0", mixed)
        self.assertFalse(self.monitor.decode_profile_eligible("d0"))
        for step in range(2, 12):
            self.clock.now = step * 0.05
            self.monitor.output_token("d0", "a")
        self.assertTrue(self.monitor._windows["d0"].prefill_overlap)
        self.monitor.roll_interval()
        self.assertEqual(self.monitor.decode_correction("d0"), 1.0)
        self.clock.now = 0.7
        self.monitor.first_token("d0", "m")
        self.assertFalse(self.monitor.decode_profile_eligible("d0"))
        self.clock.now = 0.75
        self.monitor.output_token("d0", "a")
        self.monitor.roll_interval()
        self.assertTrue(self.monitor.decode_profile_eligible("d0"))

    def test_stalled_gap_counts_started_decode_residents_only(self):
        self.decode("a", 1000)
        self.decode("b", 1000)
        self.clock.now = 0.1
        self.monitor.output_token("d0", "a")
        self.clock.now = 0.4
        self.monitor.output_token("d0", "a")
        self.clock.now = 1.0
        self.assertAlmostEqual(self.monitor.stalled_gap("d0"), 0.6)
        self.monitor.finished("d0", "a")
        self.assertEqual(self.monitor.stalled_gap("d0"), 0.0)


if __name__ == "__main__":
    unittest.main()
