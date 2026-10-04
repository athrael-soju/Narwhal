"""Pin the monitor's gap, ratio and correction maths through resident and profile changes."""

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.profiling.model import Profile
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

    def tokens(self, rid: str, count: int, *, start: float, gap: float) -> None:
        """Emit `count` d0 tokens for `rid`, `gap` seconds apart from `start`."""
        for step in range(count):
            self.clock.now = start + step * gap
            self.monitor.output_token("d0", rid)

    def test_tokens_without_an_expected_interval_record_gaps_only(self):
        """No profile, an empty store or a row without a request bound leaves the ratio unset."""
        unbounded = profile("d0")
        object.__setattr__(unbounded, "decode_max_requests", None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        empty = ProfileStore(Path(folder.name) / "empty.json", load=False)
        empty.bind_role_mix({"d0": "gpu-0"}, lambda group: (1, 1), lambda iid: Role.DECODE)
        bare = ProfileStore(Path(folder.name) / "bare.json", load=False)
        bare.put(profile("p0"))
        cases = {"no store": None, "empty store": empty, "no row": bare, "unbounded": self.store}
        for name, store in cases.items():
            with self.subTest(case=name):
                if name == "unbounded":
                    self.store._by_id["d0"] = unbounded
                self.clock.now = 0.0
                self.monitor = InstanceMonitor(self.clock, store)
                self.monitor.add(Instance("d0", "http://d0", Role.DECODE))
                self.monitor.add(Instance("p0", "http://p0", Role.PREFILL))
                self.decode("a", 1000)
                self.tokens("a", 10, start=0.05, gap=0.05)
                window = self.monitor._windows["d0"]
                self.assertEqual(
                    (window.count, window.ratio_count, window.ratio_total), (9, 0, 0.0)
                )
                self.monitor.roll_interval()
                self.assertAlmostEqual(self.monitor.mean_token_interval("d0"), 0.05)
                self.assertEqual(self.monitor.decode_correction("d0"), 1.0)

    def test_correction_clamps_at_both_bounds(self):
        """Ratios above the maximum or below the minimum move the correction by the bound."""
        cases = {"slow": (profile("d0"), 2.0), "fast": (profile("d0", tpot_intercept=1.0), 0.5)}
        for name, (row, bound) in cases.items():
            with self.subTest(case=name):
                self.store.put(row)
                self.monitor = InstanceMonitor(self.clock, self.store)
                self.monitor.add(Instance("d0", "http://d0", Role.DECODE))
                self.monitor.add(Instance("p0", "http://p0", Role.PREFILL))
                self.clock.now = 0.0
                self.decode("a", 1000)
                self.tokens("a", 10, start=0.05, gap=0.05)
                window = self.monitor._windows["d0"]
                observed = window.ratio_total / window.ratio_count
                self.assertEqual(window.ratio_count, 9)
                self.assertTrue(observed > 2.0 if bound == 2.0 else observed < 0.5)
                self.monitor.roll_interval()
                self.assertEqual(self.monitor.decode_correction("d0"), 1.0 + 0.2 * (bound - 1.0))

    def test_correction_waits_for_the_minimum_samples_in_one_window(self):
        """Seven ratios per window publish the mean but leave the correction unchanged."""
        self.decode("a", 1000)
        self.tokens("a", 8, start=0.05, gap=0.05)
        self.assertEqual(self.monitor._windows["d0"].ratio_count, 7)
        self.monitor.roll_interval()
        self.assertAlmostEqual(self.monitor.mean_token_interval("d0"), 0.05)
        self.assertEqual(self.monitor.decode_correction("d0"), 1.0)
        self.tokens("a", 7, start=0.5, gap=0.1)
        self.assertEqual(self.monitor._windows["d0"].ratio_count, 7)
        self.monitor.roll_interval()
        self.assertEqual(self.monitor.decode_correction("d0"), 1.0)
        self.tokens("a", 8, start=1.5, gap=0.1)
        self.monitor.roll_interval()
        self.assertGreater(self.monitor.decode_correction("d0"), 1.0)

    def test_published_mean_persists_through_an_empty_window(self):
        """A window without tokens keeps the previous mean and reports no current gap."""
        self.decode("a", 1000)
        self.tokens("a", 4, start=0.05, gap=0.05)
        self.monitor.roll_interval()
        published = self.monitor.mean_token_interval("d0")
        self.assertAlmostEqual(published, 0.05)
        self.clock.now = 0.3
        self.monitor.roll_interval()
        self.assertEqual(self.monitor.mean_token_interval("d0"), published)
        self.assertEqual(self.monitor.current_token_interval("d0"), 0.0)
        # The first gap of the next window spans the empty one.
        self.tokens("a", 3, start=0.4, gap=0.1)
        self.monitor.roll_interval()
        self.assertAlmostEqual(self.monitor.mean_token_interval("d0"), (0.2 + 0.1 + 0.1) / 3)

    def test_a_gap_spanning_completed_prefill_marks_overlap(self):
        """A gap that opened before the engine's last prefill boundary blocks correction."""
        self.decode("a", 1000)
        self.tokens("a", 2, start=0.05, gap=0.05)
        self.clock.now = 0.15
        self.monitor.dispatched("d0", Request("m", 200))
        self.clock.now = 0.2
        self.monitor.first_token("d0", "m")
        self.clock.now = 0.25
        self.monitor.roll_interval()
        window = self.monitor._windows["d0"]
        self.assertFalse(window.prefill_overlap)
        self.assertEqual(self.monitor.instances["d0"].prefill, {})
        self.clock.now = 0.3
        self.monitor.output_token("d0", "a")
        self.assertTrue(window.prefill_overlap)
        self.tokens("a", 9, start=0.35, gap=0.05)
        self.assertGreaterEqual(window.ratio_count, 8)
        self.monitor.roll_interval()
        self.assertEqual(self.monitor.decode_correction("d0"), 1.0)
        self.clock.now = 0.85
        self.monitor.output_token("d0", "a")
        self.assertFalse(window.prefill_overlap)


class RunningDecodeTotalTests(unittest.TestCase):
    """The monitor's running decode total equals the resident recompute after every event."""

    ENGINES = ("e0", "e1", "e2")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "profiles.json"
        self.fresh()
        original = Profile.token_interval

        def checked(row, batch_tokens, batch_requests=0.0):
            self.assertIs(type(batch_tokens), int)
            self.assertEqual(batch_tokens, self.monitor.instances[row.iid].decode_tokens())
            self.priced += 1
            return original(row, batch_tokens, batch_requests)

        priced = patch.object(Profile, "token_interval", autospec=True, side_effect=checked)
        priced.start()
        self.addCleanup(priced.stop)

    def fresh(self) -> None:
        """Start an empty profiled monitor with three decode engines."""
        self.store = ProfileStore(self.path, load=False)
        for iid in self.ENGINES:
            self.store.put(profile(iid))
        self.clock = Clock()
        self.monitor = InstanceMonitor(self.clock, self.store)
        for iid in self.ENGINES:
            self.monitor.add(Instance(iid, f"http://{iid}", Role.DECODE))
        self.priced = 0

    def assert_totals(self, event: str) -> None:
        for iid, inst in self.monitor.instances.items():
            self.assertEqual(
                self.monitor._decode_tokens[iid], inst.decode_tokens(), f"{event} on {iid}"
            )

    def test_add_starts_from_the_registered_residents(self):
        resident = Request("x", 40, phase=Phase.DECODE, output_len=3)
        self.monitor.add(Instance("e3", "http://e3", Role.DECODE, decode={"x": resident}))
        self.assertEqual(self.monitor._decode_tokens["e3"], 43)
        self.store.put(profile("e3"))
        self.monitor.output_token("e3", "x")
        self.clock.now = 0.1
        self.monitor.output_token("e3", "x")
        self.assertEqual((self.monitor._decode_tokens["e3"], self.priced), (45, 1))

    def test_random_event_sequences_keep_the_running_total_exact(self):
        """Dispatch, tokens, retries, replacements and releases in random order."""
        for seed in range(25):
            with self.subTest(seed=seed):
                self.fresh()
                self.run_events(random.Random(seed), events=400)
                self.assertGreater(self.priced, 0)

    def run_events(self, rng: random.Random, *, events: int) -> None:
        monitor = self.monitor
        # rid -> (request, owned engines, prefill engine or None once decoding)
        live: dict[str, tuple[Request, set[str], str | None]] = {}
        serial = 0
        for _ in range(events):
            self.clock.now += rng.choice((0.0, 0.0, 0.01, 0.05))
            decoding = [rid for rid, (_, _, prefill) in live.items() if prefill is None]
            prefilling = [rid for rid, (_, _, prefill) in live.items() if prefill is not None]
            kind = rng.choice(
                (
                    "arrive",
                    "first_token",
                    "tokens",
                    "tokens",
                    "tokens",
                    "stray",
                    "retry",
                    "redispatch",
                    "replace",
                    "finish",
                    "roll",
                )
            )
            if kind == "arrive" or (kind == "first_token" and not prefilling):
                serial += 1
                rid = f"r{serial}"
                request = Request(rid, rng.randint(1, 4000))
                engine = rng.choice(self.ENGINES)
                monitor.dispatched(engine, request)
                live[rid] = (request, {engine}, engine)
                kind = "arrive"
            elif kind == "first_token":
                rid = rng.choice(prefilling)
                request, owned, prefill = live[rid]
                assert prefill is not None
                # Decode can land on the prefill engine, before or after its first token.
                engine = prefill if rng.random() < 0.4 else rng.choice(self.ENGINES)
                early = engine == prefill and rng.random() < 0.5
                request.phase = Phase.DECODE
                if early:
                    monitor.dispatched(engine, request)
                monitor.first_token(prefill, rid)
                if not early:
                    monitor.dispatched(engine, request)
                owned.add(engine)
                live[rid] = (request, owned, None)
            elif kind == "tokens" and decoding:
                rid = rng.choice(decoding)
                engine = next(iid for iid in live[rid][1] if rid in monitor.instances[iid].decode)
                for _ in range(rng.randint(1, 4)):
                    monitor.output_token(engine, rid)
            elif kind == "stray":
                others = [
                    (iid, rid)
                    for rid in decoding
                    for iid in self.ENGINES
                    if rid not in monitor.instances[iid].decode
                ]
                engine, rid = (
                    rng.choice(others)
                    if others and rng.random() < 0.5
                    else (
                        rng.choice(self.ENGINES),
                        f"ghost{serial}",
                    )
                )
                monitor.output_token(engine, rid)
            elif kind == "retry" and live:
                rid = rng.choice(list(live))
                request, owned, _ = live[rid]
                for iid in owned:
                    monitor.finished(iid, rid)
                request.output_len = 0
                request.phase = Phase.PREFILL
                engine = rng.choice(self.ENGINES)
                monitor.dispatched(engine, request)
                live[rid] = (request, {engine}, engine)
            elif kind == "redispatch" and decoding:
                rid = rng.choice(decoding)
                request, owned, _ = live[rid]
                engine = next(iid for iid in owned if rid in monitor.instances[iid].decode)
                monitor.dispatched(engine, request)
            elif kind == "replace" and decoding:
                rid = rng.choice(decoding)
                request, owned, _ = live[rid]
                engine = next(iid for iid in owned if rid in monitor.instances[iid].decode)
                fresh = Request(
                    rid, rng.randint(1, 4000), phase=Phase.DECODE, output_len=rng.randint(0, 50)
                )
                monitor.dispatched(engine, fresh)
                live[rid] = (fresh, owned, None)
            elif kind == "finish" and live:
                rid = rng.choice(list(live))
                for iid in live.pop(rid)[1]:
                    monitor.finished(iid, rid)
            elif kind == "roll":
                monitor.roll_interval()
            self.assert_totals(kind)


if __name__ == "__main__":
    unittest.main()
