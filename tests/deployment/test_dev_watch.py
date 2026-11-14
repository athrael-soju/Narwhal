import io
import itertools
import math
from unittest.mock import patch

import httpx

from narwhal.dev import cli, lifecycle, watch
from tests.deployment.test_dev_direct import DirectInstance

STATE = {
    "pools": {"prefill": ["n1"], "decode": ["n2", "n3", "n4"]},
    "resident": {
        "n1": {"prefill": 2, "decode": 0},
        "n2": {"prefill": 0, "decode": 1},
        "n3": {"prefill": 0, "decode": 0},
        "n4": {"prefill": 0, "decode": 0},
    },
    "ejected": ["n4"],
    "flips": [{"iid": "n2", "to": "decode"}, {"iid": "n3", "to": "decode"}],
}


def metrics(served: int, tokens: int, ttft: tuple[int, int, int], failed: int = 0) -> str:
    fast, slow, total = ttft
    return (
        "# HELP narwhal_served_total Requests completed without error\n"
        f"narwhal_served_total {served}\n"
        f'narwhal_failed_total{{reason="engine_error"}} {failed}\n'
        f"narwhal_output_tokens_total {tokens}\n"
        f'narwhal_ttft_seconds_bucket{{slo="5",le="0.25"}} {fast}\n'
        f'narwhal_ttft_seconds_bucket{{slo="5",le="0.5"}} {slow}\n'
        f'narwhal_ttft_seconds_bucket{{slo="5",le="+Inf"}} {total}\n'
        "narwhal_ttft_seconds_count 9\n"
        "other_series 1\n"
    )


class WatchTests(DirectInstance):
    def setUp(self):
        super().setUp()
        self.init()
        self.phase("launched")
        self.readings = [
            (STATE, metrics(10, 400, (4, 10, 10))),
            (STATE, metrics(30, 1400, (4, 30, 30), failed=1)),
        ]

    def phase(self, phase: str) -> None:
        document = {"run": str(self.root / "run-1"), "phase": phase, "processes": []}
        lifecycle.write(self.root / "lifecycle.json", document)

    def fetch(self, url: str):
        reading = self.readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading

    def test_a_frame_shows_roles_load_latency_and_switches(self):
        old, new = (
            watch.parse(at, *reading)
            for at, reading in zip((10.0, 20.0), self.readings, strict=True)
        )
        shown = watch.view(old, new)
        self.assertEqual(
            shown["engines"],
            [
                {"iid": "n1", "role": "prefill", "requests": 2, "state": "live"},
                {"iid": "n2", "role": "decode", "requests": 1, "state": "live"},
                {"iid": "n3", "role": "decode", "requests": 0, "state": "live"},
                {"iid": "n4", "role": "decode", "requests": 0, "state": "ejected"},
            ],
        )
        # Twenty requests arrived in the window, all between 0.25 and 0.5 s.
        self.assertEqual(shown["ttft_s"], {"p50": 0.375, "p95": 0.4875})
        self.assertEqual(shown["tpot_s"], {"p50": None, "p95": None})
        self.assertEqual(
            (shown["requests_per_second"], shown["output_tokens_per_second"]), (2.0, 100.0)
        )
        self.assertEqual((shown["served"], shown["failed"], shown["role_switches"]), (30, 1, 2))
        self.assertEqual(shown["recent_switches"][-1], {"iid": "n3", "to": "decode"})
        text = watch.render(shown, "http://router")
        for piece in (
            "1 prefill, 3 decode",
            "figures cover the last 10 s",
            "n4      decode          0  ejected",
            "time to first token    375ms      488ms",
            "2.0 requests/s   100.0 output tokens/s",
            "role switches 2   n2 to decode   n3 to decode",
        ):
            self.assertIn(piece, text)

    def test_quantiles_interpolate_inside_a_bucket_and_stop_at_the_last_bound(self):
        counts = {0.25: 2.0, 0.5: 6.0, math.inf: 8.0}
        self.assertEqual(watch.quantile(counts, 0.5), 0.375)
        self.assertEqual(watch.quantile(counts, 0.25), 0.25)
        self.assertEqual(watch.quantile(counts, 0.95), 0.5)
        for empty in ({}, {0.25: 0.0, math.inf: 0.0}):
            self.assertIsNone(watch.quantile(empty, 0.5))

    def test_once_reads_the_totals_since_the_router_started(self):
        shown = watch.run(self.root, once=True, fetch=self.fetch)
        self.assertEqual(
            (shown["status"], shown["served"], shown["window_s"]), ("observed", 10, None)
        )
        self.assertEqual(shown["ttft_s"]["p50"], 0.25 + 0.25 * (5 - 4) / 6)
        self.assertIsNone(shown["requests_per_second"])
        with patch.object(watch, "http_fetch", self.fetch):
            self.assertEqual(cli._main(["dev", "watch", "--instance", str(self.root), "--once"]), 0)

    def test_the_view_redraws_and_keeps_going_when_the_router_is_unreachable(self):
        self.readings.insert(1, httpx.ConnectError("refused"))
        out, ticks, naps = io.StringIO(), itertools.count(10.0, 10.0), []
        shown = watch.run(
            self.root,
            interval=2.0,
            frames=3,
            fetch=self.fetch,
            out=out,
            clock=lambda: next(ticks),
            sleep=naps.append,
        )
        text = out.getvalue()
        self.assertEqual(text.count(watch.CLEAR), 3)
        self.assertIn("router unreachable: refused", text)
        self.assertEqual(naps, [2.0, 2.0])
        # The failed reading took a clock tick, so the two samples are 20 s apart.
        self.assertEqual((shown["served"], shown["requests_per_second"]), (30, 1.0))

    def test_samples_older_than_the_window_leave_the_figures(self):
        self.readings = [
            (STATE, metrics(0, 0, (0, 0, 0))),
            (STATE, metrics(100, 0, (100, 100, 100))),
            (STATE, metrics(110, 0, (100, 110, 110))),
        ]
        ticks = iter((0.0, 40.0, 50.0))
        shown = watch.run(
            self.root,
            frames=3,
            fetch=self.fetch,
            out=io.StringIO(),
            clock=lambda: next(ticks),
            sleep=lambda seconds: None,
        )
        # The first sample is 50 s old, so the figures cover the last ten requests only.
        self.assertEqual((shown["window_s"], shown["requests_per_second"]), (10.0, 1.0))
        self.assertEqual(shown["ttft_s"]["p50"], 0.375)

    def test_watch_needs_a_launched_instance_and_a_positive_interval(self):
        with self.assertRaisesRegex(ValueError, "--interval must be positive"):
            watch.run(self.root, interval=0, fetch=self.fetch)
        self.phase("stopped")
        with self.assertRaisesRegex(
            ValueError, "requires a launched instance; this one is stopped"
        ):
            watch.run(self.root, once=True, fetch=self.fetch)
