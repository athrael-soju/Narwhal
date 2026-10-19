"""Evaluate the engines table State query with promtool against scraped series."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.observability import artifacts

SHIPPED = json.loads((artifacts.BASE / "grafana-narwhal.json").read_text())
IMAGE = re.search(r"image:\s*(prom/prometheus:\S+)", (artifacts.BASE / "compose.yml").read_text())[
    1
]
LENGTH = 130

ENGINE = {"job": "engines", "instance": "e0:9000", "iid": "p0"}
ROUTER = {"job": "narwhal-router", "instance": "r:8000"}
TOOL = {"job": "narwhal-tools", "instance": "t:9100"}

SERVING, PROBING, CALIBRATING = 0, 3, 4


def _docker_image() -> bool:
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, check=False, timeout=30
    )
    return found.returncode == 0


def promtool(folder: Path) -> list[str] | None:
    """Return the promtool command for files in `folder`, or None when none is available."""
    if shutil.which("promtool") is not None:
        return ["promtool"]
    # The pinned image runs promtool when it is already present; the test never pulls it.
    if _docker_image():
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{folder}:{folder}:ro",
            "--entrypoint",
            "promtool",
            IMAGE,
        ]
    return None


def state_query() -> str:
    table = artifacts.render_dashboard(SHIPPED)["spec"]["elements"]["panel-7"]["spec"]
    expr = next(
        query["spec"]["query"]["spec"]["expr"]
        for query in table["data"]["spec"]["queries"]
        if query["spec"]["refId"] == "S"
    )
    return expr.replace("$iid", ".*").replace("$router", ".*")


def series(name: str, labels: dict[str, str], samples: dict[int, str]) -> tuple[str, str]:
    """One series with `samples` at their seconds and no sample elsewhere."""
    text = ",".join(f'{key}="{value}"' for key, value in labels.items())
    cells = [samples.get(t, "_") for t in range(LENGTH)]
    return f"{name}{{{text}}}", " ".join(cells)


def scrapes(phase: int, until: int = LENGTH) -> range:
    """Scrape times of a target scraped every 5 seconds from `phase`."""
    return range(phase, min(until, LENGTH), 5)


def constant(times: range, value: str) -> dict[int, str]:
    return dict.fromkeys(times, value)


def counter(times: range, steps: dict[int, int]) -> dict[int, str]:
    """A counter scraped at `times` that has risen by `steps[t]` at each request time t."""
    out, total = {}, 0
    events = sorted(steps.items())
    for t in times:
        while events and events[0][0] <= t:
            total += events.pop(0)[1]
        out[t] = str(total)
    return out


def engine(running: set[int], start: int = 0, until: int = LENGTH) -> list[tuple[str, str]]:
    """An engine scraped every 5 s from `start`, running requests at the seconds in `running`."""
    times = scrapes(start, until)
    tokens = dict.fromkeys(sorted(running), 8)
    return [
        series("up", ENGINE, constant(times, "1")),
        series(
            "vllm:num_requests_running",
            {**ENGINE, "model_name": "m"},
            {t: "1" if t in running else "0" for t in times},
        ),
        series("vllm:num_requests_waiting", {**ENGINE, "model_name": "m"}, constant(times, "0")),
        series(
            "vllm:generation_tokens_total", {**ENGINE, "model_name": "m"}, counter(times, tokens)
        ),
    ]


def router(
    sent: dict[int, int], resident: set[int] = frozenset(), up: range | None = None, down=()
) -> list[tuple[str, str]]:
    """A router scraped every 5 s from second 3, offset from the engine's scrapes."""
    up = up if up is not None else scrapes(3)
    labels = {**ROUTER, "iid": "p0"}
    return [
        series("up", ROUTER, {**constant(up, "1"), **constant(down, "0")}),
        series("narwhal_instance_role", {**labels, "role": "decode"}, constant(up, "1")),
        series(
            "narwhal_resident_requests",
            {**labels, "phase": "decode"},
            {t: "1" if t in resident else "0" for t in up},
        ),
        series("narwhal_resident_requests", {**labels, "phase": "prefill"}, constant(up, "0")),
        series("narwhal_engine_requests_total", labels, counter(up, sent)),
    ]


def seconds(*spans: tuple[float, float]) -> set[int]:
    """Whole seconds inside any of the `[start, end)` request spans."""
    return {t for start, end in spans for t in range(LENGTH) if start <= t < end}


class EngineStateQueryTests(unittest.TestCase):
    def check(self, inputs: list[tuple[str, str]], expected: dict[int, int]) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command = promtool(Path(folder))
            if command is None:
                self.skipTest(f"promtool and the {IMAGE} image are unavailable")
            lines = ["rule_files: []", "evaluation_interval: 1s", "tests:", "  - interval: 1s"]
            lines.append("    input_series:")
            for name, values in inputs:
                if set(values.split()) != {"_"}:
                    lines += [f"      - series: {json.dumps(name)}", f"        values: {values!r}"]
            lines.append("    promql_expr_test:")
            for at, value in sorted(expected.items()):
                lines += [
                    f"      - expr: {json.dumps(state_query())}",
                    f"        eval_time: {at}s",
                    "        exp_samples:",
                    "          - labels: '{iid=\"p0\"}'",
                    f"            value: {value}",
                ]
            path = Path(folder) / "state_test.yml"
            path.write_text("\n".join(lines) + "\n")
            result = subprocess.run(
                [*command, "test", "rules", str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
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

    def test_a_steady_routed_stream_reads_serving(self):
        # Requests shorter than a scrape interval, back to back, never resident at a router scrape.
        starts = range(0, 120, 4)
        inputs = engine(seconds((0, 120))) + router(dict.fromkeys(starts, 1))
        self.check(inputs, dict.fromkeys(range(22, 120, 4), SERVING))

    def test_a_resident_decode_stream_reads_serving(self):
        inputs = engine(seconds((0, 120))) + router({0: 1}, resident=seconds((0, 120)))
        self.check(inputs, dict.fromkeys(range(22, 120, 7), SERVING))

    def test_direct_requests_read_probing_while_the_router_is_down(self):
        # Bursty direct traffic, as calibration sends: work, a gap, then work again.
        running = seconds((0, 12), (20, 30), (36, 61))
        inputs = engine(running) + router({}, up=range(0), down=scrapes(3))
        expected = dict.fromkeys(range(26, 82, 4), PROBING)
        expected.update(dict.fromkeys(range(87, 120, 4), SERVING))
        self.check(inputs, expected)

    def test_a_tool_names_its_operation_and_its_engines_return_to_serving(self):
        running = seconds((0, 61))
        tool_up = scrapes(2, 62)
        inputs = [
            *engine(running),
            *router({}, up=range(0), down=scrapes(3)),
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
        inputs = engine(seconds((30, 120)), start=30) + router({}, up=range(0), down=scrapes(3))
        expected = dict.fromkeys(range(31, 51, 4), SERVING)
        expected.update(dict.fromkeys(range(52, 120, 6), PROBING))
        self.check(inputs, expected)


if __name__ == "__main__":
    unittest.main()
