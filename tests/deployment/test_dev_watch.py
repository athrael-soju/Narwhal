import io
import itertools
import math
from pathlib import Path
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
    "quarantined": ["n3"],
    # The flip list holds a bounded history of the seven switches the counters record.
    "flips": [{"iid": "n2", "to": "decode"}, {"iid": "n3", "to": "decode"}],
    "control": {"flips": {"reactive:decode": 4, "reactive:prefill": 3}},
}


def metrics(served: int, tokens: int, ttft: tuple[int, int, int], failed: int = 0) -> str:
    fast, slow, total = ttft
    return (
        "# HELP narwhal_served_total Requests completed without error\n"
        f"narwhal_served_total {served}\n"
        f'narwhal_failed_total{{reason="engine_error"}} {failed}\n'
        f"narwhal_output_tokens_total {tokens}\n"
        f'narwhal_expired_total{{reason="deadline"}} 2\n'
        "narwhal_rejected_total 1\n"
        f'narwhal_tpot_seconds_bucket{{slo="0.5",le="0.05"}} {total}\n'
        f'narwhal_tpot_seconds_bucket{{slo="0.5",le="+Inf"}} {total}\n'
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
                {"iid": "n3", "role": "decode", "requests": 0, "state": "quarantined"},
                {"iid": "n4", "role": "decode", "requests": 0, "state": "ejected"},
            ],
        )
        # Twenty requests arrived in the window, all between 0.25 and 0.5 s.
        self.assertEqual(shown["ttft_s"], {"p50": 0.375, "p95": 0.4875})
        self.assertEqual(shown["tpot_s"], {"p50": 0.025, "p95": 0.0475})
        self.assertEqual(
            (shown["requests_per_second"], shown["output_tokens_per_second"]), (2.0, 100.0)
        )
        self.assertEqual((shown["served"], shown["failed"], shown["role_switches"]), (30, 1, 7))
        self.assertEqual(shown["unserved"], 3)
        self.assertEqual(shown["recent_switches"][-1], {"iid": "n3", "to": "decode"})
        text = watch.render(shown, "http://router")
        for piece in (
            "1 prefill, 3 decode",
            "figures cover the last 10 s",
            "n4      decode          0  ejected",
            "time to first token      375ms      488ms",
            "time per output token     25ms       48ms",
            "2.0 requests/s   100.0 output tokens/s",
            "served 30   failed 1   refused, rejected or expired 3",
            "role switches 7   n2 to decode   n3 to decode",
        ):
            self.assertIn(piece, text)

    def test_quantiles_interpolate_inside_a_bucket_and_stop_at_the_last_bound(self):
        counts = {0.25: 2.0, 0.5: 6.0, math.inf: 8.0}
        self.assertEqual(watch.quantile(counts, 0.5), 0.375)
        self.assertEqual(watch.quantile(counts, 0.25), 0.25)
        self.assertEqual(watch.quantile(counts, 0.95), 0.5)
        for empty in ({}, {0.25: 0.0, math.inf: 0.0}):
            self.assertIsNone(watch.quantile(empty, 0.5))

    def test_once_takes_two_readings_one_interval_apart(self):
        ticks, naps = iter((10.0, 20.0)), []
        shown = watch.run(
            self.root,
            once=True,
            interval=10.0,
            fetch=self.fetch,
            clock=lambda: next(ticks),
            sleep=naps.append,
        )
        self.assertEqual(
            (shown["status"], shown["served"], shown["window_s"]), ("observed", 30, 10.0)
        )
        self.assertEqual((naps, shown["requests_per_second"]), ([10.0], 2.0))
        self.readings = [(STATE, metrics(1, 1, (1, 1, 1)))] * 2
        with patch.object(watch, "http_fetch", self.fetch):
            arguments = [
                "dev",
                "watch",
                "--instance",
                str(self.root),
                "--once",
                "--interval",
                "0.01",
            ]
            self.assertEqual(cli._main(arguments), 0)

    def test_a_restarted_router_starts_the_figures_again(self):
        self.readings = [
            (STATE, metrics(100, 4000, (100, 100, 100))),
            (STATE, metrics(5, 100, (0, 5, 5))),
            (STATE, metrics(15, 300, (0, 15, 15))),
        ]
        old, new = (
            watch.parse(at, *reading)
            for at, reading in zip((0.0, 1.0), self.readings[:2], strict=True)
        )
        self.assertTrue(watch.restarted(old, new))
        self.assertFalse(watch.restarted(new, watch.parse(2.0, *self.readings[2])))
        ticks = itertools.count(0.0, 1.0)
        shown = watch.run(
            self.root,
            frames=3,
            fetch=self.fetch,
            out=io.StringIO(),
            clock=lambda: next(ticks),
            sleep=lambda seconds: None,
        )
        # The figures cover the second and third readings, both from the new router.
        self.assertEqual((shown["window_s"], shown["requests_per_second"]), (1.0, 10.0))
        self.assertEqual(shown["ttft_s"]["p50"], 0.375)

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
        # A pipe or file takes the frames one after another, with no terminal codes.
        self.assertEqual((text.count("\x1b["), text.count("narwhal dev watch")), (0, 3))
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

    def test_a_terminal_gets_the_live_view_and_its_screen_back(self):
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        out = Terminal()
        shown = watch.run(
            self.root, frames=2, fetch=self.fetch, out=out, sleep=lambda seconds: None
        )
        drawn = out.getvalue()
        self.assertEqual(shown["status"], "observed")
        for text in ("narwhal dev watch", "time to first token", "role switches", "Ctrl+C"):
            self.assertIn(text, drawn)
        # Entering and leaving the alternate screen.
        self.assertIn("\x1b[?1049h", drawn)
        self.assertIn("\x1b[?1049l", drawn)

    def test_the_terminal_view_marks_slots_trends_and_a_fresh_switch(self):
        frame = {
            "engines": [
                {"iid": "n1", "role": "prefill", "requests": 1, "state": "live"},
                {"iid": "n2", "role": "decode", "requests": 2, "state": "draining"},
            ],
            "ttft_s": {"p50": 0.05, "p95": 0.2},
            "tpot_s": {"p50": None, "p95": None},
            "requests_per_second": 2.0,
            "output_tokens_per_second": 80.0,
            "served": 1200,
            "failed": 1,
            "unserved": 0,
            "role_switches": 1,
            "recent_switches": [{"iid": "n2", "to": "decode"}],
            "window_s": 30.0,
        }
        earlier = {**frame, "output_tokens_per_second": 0.0}
        context = watch.Context(Path("runs/dev"), "http://127.0.0.1:18000", "ERNIE-4.5-0.3B", 2)
        console = watch.Console(width=100, record=True, file=io.StringIO())
        console.print(watch.panel(frame, context, [earlier], {"n2"}))
        text = console.export_text()
        for expected in ("ERNIE-4.5-0.3B", "2 slots", "draining", "1,200", "n2 → decode"):
            self.assertIn(expected, text)
        self.assertIn("▁█", text)
        self.assertEqual(watch.trend([None, 3.0, 3.0]), " ▄▄")
        self.assertEqual(watch.trend([1.0, 2.0, 3.0]), "▁▅█")

    def test_the_role_history_fills_the_width_and_marks_each_switch(self):
        def frame(n1: str) -> dict:
            return {
                "engines": [
                    {"iid": "n1", "role": n1, "requests": 0, "state": "live"},
                    {"iid": "n2", "role": "decode", "requests": 1, "state": "live"},
                ],
                "ttft_s": {"p50": 0.05, "p95": 0.2},
                "tpot_s": {"p50": 0.02, "p95": 0.03},
                "requests_per_second": 2.0,
                "output_tokens_per_second": 80.0,
                "served": 10,
                "failed": 0,
                "unserved": 0,
                "role_switches": 1,
                "recent_switches": [{"iid": "n1", "to": "decode"}],
                "controller": {
                    "result": "applied",
                    "prefill": 0,
                    "decode": 2,
                    "current_prefill": 1,
                    "current_decode": 1,
                    "reason": "decode backlog",
                },
                "window_s": 30.0,
            }

        history = [frame("prefill")] * 3
        context = watch.Context(Path("runs/dev"), "http://127.0.0.1:18000", None, 2, 2.0)
        console = watch.Console(width=100, record=True, file=io.StringIO())
        console.print(watch.panel(frame("decode"), context, history, {"n1"}, [12.0]))
        text = console.export_text()
        n1 = next(line for line in text.splitlines() if " n1 " in line)
        # Four frames so far: an empty track to their left fills the column from the start.
        self.assertIn("──███┃", n1)
        self.assertTrue(n1.rstrip(" │").endswith("███┃"))
        heading = next(line for line in text.splitlines() if "role, last" in line)
        cells = len(n1.rstrip(" │")) - n1.index("─")
        self.assertIn(f"role, last {cells * 2} s", heading)
        ttft = next(line for line in text.splitlines() if "time to first token" in line)
        # A steady figure draws middle ticks after the empty track.
        self.assertTrue(ttft.rstrip(" │").endswith("▁▄▄▄▄"))
        self.assertIn("n1 → decode 12 s ago", text)
        self.assertIn("controller  moved to 0P:2D: decode backlog", text)

        # More frames than the column holds: the newest fill it, with the switch at the right.
        long = [frame("prefill")] * 200
        console = watch.Console(width=100, record=True, file=io.StringIO())
        console.print(watch.panel(frame("decode"), context, long))
        n1 = next(line for line in console.export_text().splitlines() if " n1 " in line)
        self.assertTrue(n1.rstrip(" │").endswith("█┃"))
        self.assertLessEqual(len(n1), 100)

    def test_the_controller_line_names_the_split_kept_or_moved_to(self):
        decision = {"prefill": 2, "decode": 2, "current_prefill": 3, "current_decode": 1}
        self.assertIsNone(watch.decision_text(None))
        self.assertEqual(
            watch.decision_text({**decision, "result": "held", "reason": "prefill pressure"}),
            "held at 3P:1D instead of 2P:2D: prefill pressure",
        )
        self.assertEqual(
            watch.decision_text({**decision, "result": "applied", "reason": None}),
            "moved to 2P:2D",
        )
        self.assertEqual(
            watch.decision_text({"result": "blocked", "prefill": 1, "decode": 3}),
            "blocked at 1P:3D",
        )
        state = {**STATE, "control": {"last_decision": {"result": "held", "at": 1.0}}}
        self.readings = [(state, metrics(10, 400, (4, 10, 10)))] * 2
        shown = watch.run(self.root, once=True, fetch=self.fetch, sleep=lambda seconds: None)
        self.assertEqual(shown["controller"]["result"], "held")
        self.assertNotIn("at", shown["controller"])

    def test_the_live_view_times_the_switches_it_sees(self):
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        later = {
            **STATE,
            "flips": [*STATE["flips"], {"iid": "n1", "to": "prefill"}],
            "control": {"flips": {"reactive:decode": 4, "reactive:prefill": 4}},
        }
        self.readings = [
            (STATE, metrics(10, 400, (4, 10, 10))),
            (later, metrics(20, 800, (4, 20, 20))),
            (later, metrics(30, 1200, (4, 30, 30))),
        ]
        clock = itertools.count(0.0, 5.0)
        out = Terminal()
        watch.run(
            self.root,
            frames=3,
            fetch=self.fetch,
            out=out,
            clock=lambda: next(clock),
            sleep=lambda seconds: None,
        )
        drawn = out.getvalue()
        self.assertIn("5 s ago", drawn)
        # A switch from before the view began has no time.
        self.assertNotIn("n2 → decode 1", drawn)

    def test_the_logo_shows_while_the_frame_fits_the_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        for lines, shown in (("60", True), ("24", False)):
            self.readings = [(STATE, metrics(10, 400, (4, 10, 10)))]
            out = Terminal()
            with patch.dict("os.environ", {"LINES": lines, "COLUMNS": "100"}):
                watch.run(self.root, frames=1, fetch=self.fetch, out=out, sleep=lambda s: None)
            self.assertEqual("▝███▌" in out.getvalue(), shown, lines)

    def test_watch_needs_a_launched_instance_and_a_positive_interval(self):
        for interval in (0, -1, math.inf, math.nan):
            with self.assertRaisesRegex(ValueError, "--interval must be a positive number"):
                watch.run(self.root, interval=interval, fetch=self.fetch)
        with self.assertRaises(SystemExit) as stopped:
            cli._main(["dev", "watch", "--instance", str(self.root), "--interval", "inf"])
        self.assertEqual(stopped.exception.code, 2)
        self.phase("stopped")
        with self.assertRaisesRegex(
            ValueError, "requires a launched instance; this one is stopped"
        ):
            watch.run(self.root, once=True, fetch=self.fetch)
