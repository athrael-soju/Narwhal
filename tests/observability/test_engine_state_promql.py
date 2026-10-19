"""Evaluate the engines table State query with promtool against scraped series."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools.observability import artifacts

SHIPPED = json.loads((artifacts.BASE / "grafana-narwhal.json").read_text())
IMAGE = re.search(r"image:\s*(prom/prometheus:\S+)", (artifacts.BASE / "compose.yml").read_text())[
    1
]
MAJOR = re.search(r":v(\d+)\.", IMAGE)[1]
LENGTH = 130

ENGINE = {"job": "engines", "instance": "e0:9000", "iid": "p0"}
ROUTER = {"job": "narwhal-router", "instance": "r:8000"}
TOOL = {"job": "narwhal-tools", "instance": "t:9100"}

SERVING, BACKLOGGED, PROBING, CALIBRATING = 0, 2, 3, 4

Samples = dict[float, str]


def _docker_image() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, check=False, timeout=30
        )
    except subprocess.TimeoutExpired:
        return False
    return found.returncode == 0


def _local_promtool() -> str | None:
    """Return the promtool on PATH when it has the pinned image's major version."""
    local = shutil.which("promtool")
    if local is None:
        return None
    try:
        version = subprocess.run(
            [local, "--version"], capture_output=True, text=True, check=False, timeout=30
        )
    except subprocess.TimeoutExpired:
        return None
    found = re.search(r"version (\d+)\.", version.stdout + version.stderr)
    return local if found and found[1] == MAJOR else None


# Where promtool runs, as the first case found it.
FOUND: dict[str, str | None] = {}


def _source() -> str | None:
    """Return "docker" when the pinned image is present, else a matching promtool on PATH.

    The first case decides for the whole run, so a hung Docker daemon costs one timeout.
    """
    if "promtool" not in FOUND:
        FOUND["promtool"] = "docker" if _docker_image() else _local_promtool()
    return FOUND["promtool"]


def promtool(folder: Path) -> list[str] | None:
    """Return the promtool command for files in `folder`, or None when none is available."""
    # The pinned image runs promtool when it is already present; the test never pulls it.
    # The container runs as this user so it can read the private test folder.
    source = _source()
    if source == "docker":
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            f"narwhal-promtool-{uuid.uuid4().hex[:12]}",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{folder}:{folder}:ro",
            "--entrypoint",
            "promtool",
            IMAGE,
        ]
    return [source] if source is not None else None


def state_query() -> str:
    table = artifacts.render_dashboard(SHIPPED)["spec"]["elements"]["panel-7"]["spec"]
    expr = next(
        query["spec"]["query"]["spec"]["expr"]
        for query in table["data"]["spec"]["queries"]
        if query["spec"]["refId"] == "S"
    )
    return expr.replace("$iid", ".*").replace("$router", ".*")


def series(name: str, labels: dict[str, str], samples: Samples) -> tuple[str, Samples]:
    """One series with `samples` at their seconds and no sample elsewhere."""
    text = ",".join(f'{key}="{value}"' for key, value in labels.items())
    return f"{name}{{{text}}}", samples


def scrapes(phase: float, until: float = LENGTH) -> list[float]:
    """Scrape times of a target scraped every 5 seconds from `phase`."""
    end = min(until, LENGTH)
    return [round(phase + 5 * k, 1) for k in range(LENGTH // 5) if phase + 5 * k < end]


def constant(times, value: str) -> Samples:
    return dict.fromkeys(times, value)


def counter(times: list[float], steps: dict[float, int]) -> Samples:
    """A counter scraped at `times` that has risen by `steps[t]` at each request time t."""
    out, total = {}, 0
    events = sorted(steps.items())
    for t in times:
        while events and events[0][0] <= t:
            total += events.pop(0)[1]
        out[t] = str(total)
    return out


def engine(
    running: set[int], start: int = 0, until: int = LENGTH, waiting: set[int] = frozenset()
) -> list[tuple[str, Samples]]:
    """An engine scraped every 5 s from `start`, running requests at the seconds in `running`
    and holding waiting requests at the seconds in `waiting`."""
    times = scrapes(start, until)
    tokens = dict.fromkeys(sorted(running), 8)
    labels = {**ENGINE, "model_name": "m"}
    return [
        series("up", ENGINE, constant(times, "1")),
        series(
            "vllm:num_requests_running", labels, {t: "1" if t in running else "0" for t in times}
        ),
        series(
            "vllm:num_requests_waiting", labels, {t: "1" if t in waiting else "0" for t in times}
        ),
        series("vllm:generation_tokens_total", labels, counter(times, tokens)),
    ]


def sglang_engine(running: set[int], finished: dict[float, int]) -> list[tuple[str, Samples]]:
    """An SGLang engine scraped every 5 s, reporting running requests at the seconds in
    `running` and counting a request's tokens when it finishes, at the times in `finished`."""
    times = scrapes(0)
    labels = {**ENGINE, "model_name": "m"}
    return [
        series("up", ENGINE, constant(times, "1")),
        series("sglang:num_running_reqs", labels, {t: "1" if t in running else "0" for t in times}),
        series("sglang:num_queue_reqs", labels, constant(times, "0")),
        series("sglang:prompt_tokens_total", labels, counter(times, finished)),
        series("sglang:generation_tokens_total", labels, counter(times, finished)),
    ]


def router(
    sent: dict[float, int],
    resident: set[int] = frozenset(),
    up: list[float] | None = None,
    down=(),
    *,
    instance: str = "r:8000",
    restart: float | None = None,
    delay: float = 0.0,
) -> list[tuple[str, Samples]]:
    """A router scraped every 5 s from second 3, offset from the engine's scrapes.

    A router that restarts at `restart`, between two scrapes, counts from zero again. Each
    sample lands `delay` seconds after its scrape starts, as from a slow scrape.
    """
    up = up if up is not None else scrapes(3)
    target = {**ROUTER, "instance": instance}
    labels = {**target, "iid": "p0"}
    total = counter(up, sent)
    if restart is not None:
        later = {t: count for t, count in sent.items() if t > restart}
        total.update(counter([t for t in up if t > restart], later))

    def landed(samples: Samples) -> Samples:
        return {round(t + delay, 1): value for t, value in samples.items()}

    return [
        series("up", target, landed({**constant(up, "1"), **constant(down, "0")})),
        series("narwhal_instance_role", {**labels, "role": "decode"}, landed(constant(up, "1"))),
        series(
            "narwhal_resident_requests",
            {**labels, "phase": "decode"},
            landed({t: "1" if t in resident else "0" for t in up}),
        ),
        series(
            "narwhal_resident_requests", {**labels, "phase": "prefill"}, landed(constant(up, "0"))
        ),
        series("narwhal_engine_requests_total", labels, landed(total)),
    ]


def seconds(*spans: tuple[float, float]) -> set[int]:
    """Whole seconds inside any of the `[start, end)` request spans."""
    return {t for start, end in spans for t in range(LENGTH) if start <= t < end}


class EngineStateQueryTests(unittest.TestCase):
    # Set when Docker fails to run promtool in time, so the remaining cases skip at once.
    docker_stalled = False

    def check(
        self, inputs: list[tuple[str, Samples]], expected: dict[float, int], step: float = 1.0
    ) -> None:
        """Evaluate the State query at each expected time, with `step`-second input samples."""
        if EngineStateQueryTests.docker_stalled:
            self.skipTest(f"Docker did not run promtool from {IMAGE} within 120 seconds")
        interval = f"{round(step * 1000)}ms"
        with tempfile.TemporaryDirectory() as folder:
            command = promtool(Path(folder))
            if command is None:
                self.skipTest(f"the {IMAGE} image and a promtool {MAJOR}.x are unavailable")
            lines = [
                "rule_files: []",
                f"evaluation_interval: {interval}",
                "tests:",
                f"  - interval: {interval}",
            ]
            lines.append("    input_series:")
            for name, samples in inputs:
                cells = ["_"] * round(LENGTH / step)
                for at, value in samples.items():
                    if at < LENGTH:
                        cells[round(at / step)] = value
                if set(cells) != {"_"}:
                    values = " ".join(cells)
                    lines += [f"      - series: {json.dumps(name)}", f"        values: {values!r}"]
            lines.append("    promql_expr_test:")
            for at, value in sorted(expected.items()):
                lines += [
                    f"      - expr: {json.dumps(state_query())}",
                    f"        eval_time: {round(at * 1000)}ms",
                    "        exp_samples:",
                    "          - labels: '{iid=\"p0\"}'",
                    f"            value: {value}",
                ]
            path = Path(folder) / "state_test.yml"
            path.write_text("\n".join(lines) + "\n")
            try:
                result = subprocess.run(
                    [*command, "test", "rules", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                if command[0] != "docker":
                    raise
                EngineStateQueryTests.docker_stalled = True
                self.skipTest(f"Docker did not run promtool from {IMAGE} within 120 seconds")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_short_routed_requests_between_router_scrapes_read_serving(self):
        # Each request runs 2 s, covers an engine scrape and ends before the router scrape.
        starts = range(9, 120, 20)
        running = seconds(*((s, s + 2) for s in starts))
        inputs = engine(running) + router(dict.fromkeys(starts, 1))
        self.check(inputs, dict.fromkeys(range(16, 120, 3), SERVING))

    def test_routed_requests_seen_only_at_engine_scrapes_read_serving(self):
        # Every engine scrape sees a 2 s request that ends before the next router scrape.
        starts = range(4, 120, 5)
        running = seconds(*((s, s + 2) for s in starts))
        inputs = engine(running) + router(dict.fromkeys(starts, 1))
        self.check(inputs, dict.fromkeys(range(16, 120, 3), SERVING))

    def test_routed_requests_read_serving_when_router_samples_land_late(self):
        # Each 0.5 s request starts just after a router scrape and covers an engine scrape, so
        # the router counts it at its next scrape, whose sample lands up to 1.5 s late.
        starts = [39.95 + 5 * k for k in range(8)]
        expected = {round(40 + 0.2 * k, 1): SERVING for k in range(250)}
        for delay in (0.0, 1.5):
            with self.subTest(delay=delay):
                inputs = engine(set(range(40, 76, 5))) + router(
                    dict.fromkeys(starts, 1), up=scrapes(4.9), delay=delay
                )
                self.check(inputs, expected, step=0.1)

    def test_a_steady_routed_stream_reads_serving(self):
        # Requests shorter than a scrape interval, back to back, never resident at a router scrape.
        starts = range(0, 120, 4)
        inputs = engine(seconds((0, 120))) + router(dict.fromkeys(starts, 1))
        self.check(inputs, dict.fromkeys(range(22, 120, 4), SERVING))

    def test_a_resident_decode_stream_reads_serving(self):
        inputs = engine(seconds((0, 120))) + router({0: 1}, resident=seconds((0, 120)))
        self.check(inputs, dict.fromkeys(range(22, 120, 7), SERVING))

    def test_requests_from_either_of_two_routers_read_serving(self):
        # The second router routes requests until 40 s; direct requests follow from 60 s.
        starts = range(4, 40, 5)
        running = seconds(*((s, s + 2) for s in starts), (60, 110))
        inputs = [
            *engine(running),
            *router({}),
            *router(dict.fromkeys(starts, 1), instance="r2:8000"),
        ]
        expected = dict.fromkeys(range(16, 74, 3), SERVING)
        # The second router's last count leaves its 37-second window.
        expected.update(dict.fromkeys(range(76, 115, 3), PROBING))
        expected.update(dict.fromkeys(range(115, 130, 3), SERVING))
        self.check(inputs, expected)

    def test_a_router_restart_between_scrapes_still_counts_its_requests(self):
        # The router restarts at 60 s between two scrapes it answers, so its counter falls from
        # 6 to 1 with no failed scrape.
        starts = [*range(4, 30, 5), *range(61, 120, 5)]
        running = seconds(*((s, s + 2) for s in starts))
        inputs = engine(running) + router(dict.fromkeys(starts, 1), restart=60)
        self.check(inputs, dict.fromkeys(range(16, 120, 3), SERVING))

    def test_direct_requests_read_probing_while_the_router_is_down(self):
        # Bursty direct traffic, as calibration sends: work, a short gap, then work again.
        running = seconds((0, 12), (15, 30), (33, 61))
        inputs = engine(running) + router({}, up=[], down=scrapes(3))
        expected = dict.fromkeys(range(23, 65, 3), PROBING)
        # The engine's second scrape after the last request clears the state.
        expected.update(dict.fromkeys(range(65, 120, 4), SERVING))
        self.check(inputs, expected)

    def test_probing_outranks_backlogged(self):
        running = seconds((0, 61))
        inputs = engine(running, waiting=seconds((0, 16), (40, 56))) + router(
            {}, up=[], down=scrapes(3)
        )
        expected = dict.fromkeys(range(2, 20, 3), BACKLOGGED)
        expected.update(dict.fromkeys(range(23, 65, 3), PROBING))
        expected.update(dict.fromkeys(range(65, 120, 4), SERVING))
        self.check(inputs, expected)

    def test_an_engine_idle_for_a_scrape_interval_reads_serving_until_it_works_again(self):
        running = seconds((0, 30), (42, 61))
        inputs = engine(running) + router({}, up=[], down=scrapes(3))
        expected = dict.fromkeys(range(23, 35, 3), PROBING)
        expected.update(dict.fromkeys(range(35, 45, 3), SERVING))
        expected.update(dict.fromkeys(range(45, 65, 3), PROBING))
        expected.update(dict.fromkeys(range(65, 120, 4), SERVING))
        self.check(inputs, expected)

    def test_sglang_direct_requests_read_probing_until_their_kv_transfers_finish(self):
        # A prefill engine reports running requests, and counts each request's tokens, until its
        # KV transfer finishes, 12 s after the request.
        finished = dict.fromkeys(range(16, 73, 4), 8)
        inputs = sglang_engine(seconds((0, 73)), finished) + router({}, up=[], down=scrapes(3))
        expected = dict.fromkeys(range(23, 80, 3), PROBING)
        # The engine's second scrape after the last transfer clears the state.
        expected.update(dict.fromkeys(range(80, 120, 4), SERVING))
        self.check(inputs, expected)

    def test_routed_sglang_requests_read_serving_while_their_kv_transfers_finish(self):
        starts = range(0, 61, 4)
        finished = {start + 12: 8 for start in starts}
        inputs = sglang_engine(seconds((0, 73)), finished) + router(dict.fromkeys(starts, 1))
        self.check(inputs, dict.fromkeys(range(16, 120, 3), SERVING))

    def test_a_tool_names_its_operation_and_its_engines_return_to_serving(self):
        running = seconds((0, 61))
        tool_up = scrapes(2, 62)
        inputs = [
            *engine(running),
            *router({}, up=[], down=scrapes(3)),
            series("up", TOOL, {**constant(tool_up, "1"), **constant(scrapes(62), "0")}),
            series(
                "narwhal_tool_engine_in_use",
                {**TOOL, "operation": "calibration", "iid": "p0"},
                {**constant(tool_up, "1"), 62: "stale"},
            ),
        ]
        expected = dict.fromkeys(range(26, 60, 4), CALIBRATING)
        # The tool's next scrape after it exits clears the state.
        expected.update(dict.fromkeys(range(62, 120, 4), SERVING))
        self.check(inputs, expected)

    def test_a_router_that_just_started_hides_probing(self):
        # A request the router sent before its first scrape leaves its counter flat.
        inputs = engine(seconds((20, 23))) + router({20: 1}, up=scrapes(23), down=scrapes(3, 23))
        self.check(inputs, dict.fromkeys(range(24, 70, 3), SERVING))

    def test_a_newly_scraped_engine_waits_for_a_full_window(self):
        inputs = engine(seconds((30, 120)), start=30) + router({}, up=[], down=scrapes(3))
        expected = dict.fromkeys(range(31, 53, 4), SERVING)
        expected.update(dict.fromkeys(range(53, 120, 6), PROBING))
        self.check(inputs, expected)


class PromtoolAvailabilityTests(unittest.TestCase):
    """A hung Docker daemon costs the State cases one timeout, not one per case."""

    CASES = ("test_a_steady_routed_stream_reads_serving", "test_probing_outranks_backlogged")

    def setUp(self):
        for patch in (
            mock.patch.dict(FOUND, clear=True),
            mock.patch.object(EngineStateQueryTests, "docker_stalled", False),
            mock.patch.object(shutil, "which", side_effect={"docker": "/usr/bin/docker"}.get),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cases(self, run) -> tuple[unittest.TestResult, list[list[str]]]:
        """Run two State cases with `run` in place of `subprocess.run`, and return the result
        and the commands they ran."""
        result = unittest.TestResult()
        with mock.patch.object(subprocess, "run", side_effect=run) as patched:
            for name in self.CASES:
                EngineStateQueryTests(name).run(result)
        return result, [call.args[0] for call in patched.call_args_list]

    def test_an_image_check_that_times_out_skips_every_case(self):
        result, commands = self.run_cases(subprocess.TimeoutExpired("docker", 30))
        self.assertEqual(
            [reason for _, reason in result.skipped],
            [f"the {IMAGE} image and a promtool {MAJOR}.x are unavailable"] * 2,
        )
        self.assertEqual(commands, [["docker", "image", "inspect", IMAGE]])

    def test_a_promtool_run_that_times_out_skips_the_remaining_cases(self):
        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, 120)
            return subprocess.CompletedProcess(command, 0)

        result, commands = self.run_cases(run)
        self.assertEqual(
            [reason for _, reason in result.skipped],
            [f"Docker did not run promtool from {IMAGE} within 120 seconds"] * 2,
        )
        self.assertEqual(
            [command[:2] for command in commands], [["docker", "image"], ["docker", "run"]]
        )


if __name__ == "__main__":
    unittest.main()
