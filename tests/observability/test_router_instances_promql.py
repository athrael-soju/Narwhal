"""Evaluate the engine table, the role history and the Router stat over several router instances.

A router whose origin changed leaves its last samples in Prometheus for the lookback window,
and a router pair scraped directly reports two instances. The panels follow the router that
is scraped now and ready, and list the engines it lists.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.observability import test_engine_state_promql as state_tests
from tests.observability.test_engine_state_promql import (
    RUN_TIMEOUT_S,
    SHIPPED,
    STALLED,
    UNAVAILABLE,
    promtool,
)
from tools.observability import artifacts

LENGTH = 130
# The range end and length that stand in for Grafana's end() and $__range.
END = 125

Samples = dict[float, str]


def panel_query(panel: str, ref: str) -> str:
    element = artifacts.render_dashboard(SHIPPED)["spec"]["elements"][panel]["spec"]
    expr = next(
        query["spec"]["query"]["spec"]["expr"]
        for query in element["data"]["spec"]["queries"]
        if query["spec"]["refId"] == ref
    )
    return (
        expr.replace("$iid", ".*")
        .replace("$router", ".*")
        .replace("@ end()", f"@ {END}")
        .replace("$__range", f"{END}s")
    )


def at_step(expr: str, second: int) -> str:
    """Return `expr`, a range query of the panel, as its value at `second` of a range that ends
    at END. Evaluate it at END, so the range's later samples exist when it reads them."""
    return f"last_over_time(({expr})[1s:1s] offset {END - second}s)"


def scrapes(start: float, stop: float = LENGTH) -> list[float]:
    """Scrape times every 5 seconds from `start` until before `stop`."""
    return [float(t) for t in range(int(start), int(min(stop, LENGTH)), 5)]


def router(instance: str, start: float, stop: float, roles: dict[str, str], ready: str = "1"):
    """The series of one router scraped from `start` until `stop`, listing `roles`."""
    times = scrapes(start, stop)
    labels = f'job="narwhal-router",instance="{instance}"'
    out = {
        f"up{{{labels}}}": dict.fromkeys(times, "1"),
        f"narwhal_router_ready{{{labels}}}": dict.fromkeys(times, ready),
    }
    for iid, role in roles.items():
        for name in ("prefill", "decode"):
            series = f'narwhal_instance_role{{{labels},iid="{iid}",role="{name}"}}'
            out[series] = dict.fromkeys(times, "1" if name == role else "0")
    return out


def engines(*iids: str) -> dict[str, Samples]:
    return {
        f'up{{job="engines",instance="{iid}:8000",iid="{iid}",backend="vllm"}}': dict.fromkeys(
            scrapes(0), "1"
        )
        for iid in iids
    }


# The router moved from r0 to r1 at 40 s; r0's samples stay within the lookback window.
# n4 left before the move, and its engine target still reads up.
MOVED = {
    **router("r0:8000", 0, 40, {"n1": "prefill", "n2": "decode", "n4": "decode"}),
    **router("r1:8000", 42, LENGTH, {"n1": "prefill", "n2": "decode", "n3": "decode"}),
    **engines("n1", "n2", "n3", "n4"),
}
PREFILL, DECODE = 1, 2
READY = '{{__name__="narwhal_router_ready",instance="{}:8000",job="narwhal-router"}}'


class RouterInstanceQueryTests(unittest.TestCase):
    def check(self, inputs: dict[str, Samples], tests: list[tuple[str, int, dict[str, float]]]):
        """Evaluate each (expression, second, {labels: value}) against `inputs`."""
        if state_tests.EngineStateQueryTests.docker_stalled:
            self.skipTest(STALLED)
        lines = [
            "rule_files: []",
            "evaluation_interval: 1s",
            "tests:",
            "  - interval: 1s",
            "    input_series:",
        ]
        for name, samples in inputs.items():
            cells = ["_"] * LENGTH
            for at, value in samples.items():
                cells[round(at)] = value
            lines += [f"      - series: {json.dumps(name)}", f"        values: {' '.join(cells)!r}"]
        lines.append("    promql_expr_test:")
        for expr, at, expected in tests:
            lines += [f"      - expr: {json.dumps(expr)}", f"        eval_time: {at}s"]
            if not expected:
                lines.append("        exp_samples: []")
                continue
            lines.append("        exp_samples:")
            for labels, value in sorted(expected.items()):
                lines += [
                    f"          - labels: {json.dumps(labels)}",
                    f"            value: {value}",
                ]
        with tempfile.TemporaryDirectory() as folder:
            command = promtool(Path(folder))
            if command is None:
                self.skipTest(UNAVAILABLE)
            path = Path(folder) / "routers_test.yml"
            path.write_text("\n".join(lines) + "\n")
            try:
                result = subprocess.run(
                    [*command, "test", "rules", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=RUN_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                if command[0] != "docker":
                    raise
                state_tests.EngineStateQueryTests.docker_stalled = True
                self.skipTest(STALLED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_held_router_neither_doubles_roles_nor_lists_a_departed_engine(self):
        roles = {'{iid="n1"}': PREFILL, '{iid="n2"}': DECODE, '{iid="n3"}': DECODE}
        serving = dict.fromkeys(roles, 0)
        backends = {f'{{backend="vllm",iid="{iid}"}}': 1 for iid in ("n1", "n2", "n3")}
        self.check(
            MOVED,
            [
                (panel_query("panel-7", "A"), 60, roles),
                (panel_query("panel-7", "S"), 60, serving),
                (panel_query("panel-7", "B"), 60, backends),
                # Before the move, r0 lists n4.
                (
                    panel_query("panel-7", "A"),
                    30,
                    {'{iid="n1"}': PREFILL, '{iid="n2"}': DECODE, '{iid="n4"}': DECODE},
                ),
            ],
        )

    def test_the_router_stat_shows_the_routers_scraped_now(self):
        down = {'up{job="narwhal-router",instance="r2:8000"}': dict.fromkeys(scrapes(0), "0")}
        admission = panel_query("panel-1", "A")
        self.check(
            {**MOVED, **down},
            [
                (
                    admission,
                    60,
                    {
                        READY.format("r1"): 1,
                        '{instance="r2:8000",job="narwhal-router"}': -1,
                    },
                ),
                (
                    admission,
                    30,
                    {
                        READY.format("r0"): 1,
                        '{instance="r2:8000",job="narwhal-router"}': -1,
                    },
                ),
            ],
        )

    def test_a_ready_router_outranks_a_standby_that_lists_other_roles(self):
        pair = {
            **router("r1:8000", 0, LENGTH, {"n1": "prefill", "n2": "decode"}),
            **router("r2:8000", 0, LENGTH, {"n1": "decode", "n2": "decode"}, ready="0"),
            **engines("n1", "n2"),
        }
        roles = {'{iid="n1"}': PREFILL, '{iid="n2"}': DECODE}
        self.check(
            pair,
            [
                (panel_query("panel-7", "A"), 60, roles),
                (at_step(panel_query("panel-8", "A"), 60), END, roles),
            ],
        )

    def test_role_history_follows_the_active_router_at_each_step(self):
        members = {'{iid="n1"}': PREFILL, '{iid="n2"}': DECODE}
        history = panel_query("panel-8", "A")
        self.check(
            MOVED,
            [
                (at_step(history, 30), END, members),
                (at_step(history, 60), END, {**members, '{iid="n3"}': DECODE}),
                (at_step(history, 100), END, {**members, '{iid="n3"}': DECODE}),
            ],
        )

    def test_a_departed_engine_keeps_its_lane_until_its_last_sample(self):
        departed = panel_query("panel-8", "D")
        self.check(
            MOVED,
            [
                (at_step(departed, 30), END, {'{iid="n4"}': DECODE}),
                (at_step(departed, 60), END, {}),
                (at_step(departed, 100), END, {}),
            ],
        )

    def test_the_engine_table_has_no_filler_rows(self):
        table = artifacts.render_dashboard(SHIPPED)["spec"]["elements"]["panel-7"]["spec"]
        refs = [query["spec"]["refId"] for query in table["data"]["spec"]["queries"]]
        self.assertNotIn("Z", refs)
        self.assertNotIn(
            "styleField", table["vizConfig"]["spec"]["fieldConfig"]["defaults"]["custom"]
        )

    def test_an_engine_that_left_has_no_row_while_its_target_still_reads_up(self):
        # n5 left; Prometheus still scrapes its target until discovery drops it.
        fleet = {**router("r1:8000", 0, LENGTH, {"n1": "prefill"}), **engines("n1", "n5")}
        self.check(
            fleet,
            [
                (panel_query("panel-7", "A"), 60, {'{iid="n1"}': PREFILL}),
                (panel_query("panel-7", "S"), 60, {'{iid="n1"}': 0}),
                (panel_query("panel-7", "B"), 60, {'{backend="vllm",iid="n1"}': 1}),
            ],
        )

    def test_without_a_router_the_table_lists_the_scraped_engines(self):
        fleet = {
            'up{job="narwhal-router",instance="r1:8000"}': dict.fromkeys(scrapes(0), "0"),
            **engines("n1"),
        }
        self.check(fleet, [(panel_query("panel-7", "S"), 60, {'{iid="n1"}': 0})])

    def test_fleet_events_reads_no_alerts_only_for_a_range_without_one(self):
        alert = 'ALERTS{alertname="NarwhalEngineDown",alertstate="firing",severity="page",iid="n1"}'
        empty = panel_query("panel-37", "B")
        self.check(
            {alert: dict.fromkeys(range(50, 61), "1"), **engines("n1")},
            [(empty, END, {})],
        )
        self.check(engines("n1"), [(empty, END, {"{}": 0})])

    def test_leaving_outranks_draining_in_the_table_and_the_role_history(self):
        labels = 'job="narwhal-router",instance="r1:8000"'
        times = scrapes(0)
        fleet = {
            **router("r1:8000", 0, LENGTH, {"n1": "prefill", "n2": "decode"}),
            **engines("n1", "n2"),
        }
        for iid, leaving in (("n1", "1"), ("n2", "0")):
            fleet[f'narwhal_engine_draining{{{labels},iid="{iid}"}}'] = dict.fromkeys(times, "1")
            fleet[f'narwhal_engine_leaving{{{labels},iid="{iid}"}}'] = dict.fromkeys(times, leaving)
        table = state_codes("panel-7")
        history = state_codes("panel-8")
        self.check(
            fleet,
            [
                (
                    panel_query("panel-7", "S"),
                    60,
                    {'{iid="n1"}': table["Leaving"], '{iid="n2"}': table["Draining"]},
                ),
                (
                    at_step(panel_query("panel-8", "A"), 60),
                    END,
                    {'{iid="n1"}': history["Leaving"], '{iid="n2"}': history["Draining"]},
                ),
            ],
        )

    def test_available_engines_reads_starting_respond_and_no_response(self):
        times = scrapes(0)

        def series(name: str, iid: str, value: str) -> dict[str, Samples]:
            labels = f'job="fleet-control",instance="control:8000",iid="{iid}",backend="vllm"'
            return {f"narwhal_control_engine_{name}{{{labels}}}": dict.fromkeys(times, value)}

        exported = {
            **series("available", "n1", "0"),
            **series("starting", "n1", "1"),
            **series("available", "n2", "1"),
            **series("starting", "n2", "0"),
            **series("available", "n3", "0"),
            **series("starting", "n3", "0"),
        }
        older = {**series("available", "n1", "1"), **series("available", "n2", "0")}
        self.check(
            exported,
            [
                (
                    panel_query("panel-61", "A"),
                    60,
                    {
                        '{backend="vllm",iid="n1"}': 2,
                        '{backend="vllm",iid="n2"}': 1,
                        '{backend="vllm",iid="n3"}': 0,
                    },
                )
            ],
        )
        # A fleet control that exports no starting series still lists its engines.
        self.check(
            older,
            [
                (
                    panel_query("panel-61", "A"),
                    60,
                    {'{backend="vllm",iid="n1"}': 1, '{backend="vllm",iid="n2"}': 0},
                )
            ],
        )

    def marks(self, layer: str, inputs: dict[str, Samples], step: int, expected: dict):
        """Evaluate `layer` at each `step`, expecting {second: {labels: value}} and no other."""
        query = marker_query(layer, step)
        self.check(
            inputs,
            [(query, at, expected.get(at, {})) for at in range(step, LENGTH, step)],
        )

    def test_a_membership_change_is_marked_once_at_any_step(self):
        labels = 'job="narwhal-router",instance="r1:8000"'
        generation = {
            t: "0" if t < 40 else "1" if t < 85 else "2" if t < 100 else "0" for t in scrapes(0)
        }
        fleet = {
            **router("r1:8000", 0, LENGTH, {}),
            f"narwhal_membership_generation{{{labels}}}": generation,
        }
        mark = '{__name__="narwhal_membership_generation",instance="r1:8000",job="narwhal-router"}'
        self.marks("Membership changes", fleet, 15, {45: {mark: 1}, 90: {mark: 2}, 105: {mark: 0}})
        # At 60 s steps each step compares with the one before, so no change is skipped.
        self.marks("Membership changes", fleet, 60, {60: {mark: 1}, 120: {mark: 0}})
        self.marks("Membership changes", fleet, 30, {60: {mark: 1}, 90: {mark: 2}, 120: {mark: 0}})

    def test_a_takeover_is_not_marked_and_a_later_join_is(self):
        old = 'job="narwhal-router",instance="r1:8000"'
        new = 'job="narwhal-router",instance="r2:8000"'
        times = scrapes(0)
        fleet = {
            **router("r1:8000", 0, 55, {}),
            f"narwhal_membership_generation{{{old}}}": dict.fromkeys(scrapes(0, 55), "3"),
            f"up{{{new}}}": dict.fromkeys(times, "1"),
            # The standby is not ready until it takes over at 60 s and adopts generation 3.
            f"narwhal_router_ready{{{new}}}": {t: "0" if t < 60 else "1" for t in times},
            f"narwhal_membership_generation{{{new}}}": {
                t: "0" if t < 60 else "3" if t < 90 else "4" for t in times
            },
        }
        mark = '{__name__="narwhal_membership_generation",instance="r2:8000",job="narwhal-router"}'
        self.marks("Membership changes", fleet, 15, {90: {mark: 4}})
        self.marks("Membership changes", fleet, 60, {120: {mark: 4}})

    def test_an_alert_that_starts_between_long_steps_is_marked(self):
        alert = 'ALERTS{alertname="NarwhalEngineDown",alertstate="firing",severity="page",iid="n1"}'
        fleet = {alert: {float(t): "1" for t in range(70, LENGTH)}}
        mark = (
            '{__name__="ALERTS",alertname="NarwhalEngineDown",alertstate="firing",'
            'iid="n1",severity="page"}'
        )
        self.marks("Narwhal pages", fleet, 15, {75: {mark: 1}})
        self.marks("Narwhal pages", fleet, 60, {120: {mark: 1}})


def marker_query(layer: str, step: int) -> str:
    """Return the annotation layer's query as Grafana runs it at `step` seconds."""
    annotations = artifacts.render_dashboard(SHIPPED)["spec"]["annotations"]
    spec = next(item["spec"] for item in annotations if item["spec"]["name"] == layer)
    expr = spec["query"]["spec"]["expr"]
    return expr.replace("$router", ".*").replace("$__interval", f"{step}s")


def state_codes(panel: str) -> dict[str, int]:
    """Map each state name to its code in the panel's value mappings."""
    spec = SHIPPED["spec"]["elements"][panel]["spec"]["vizConfig"]["spec"]["fieldConfig"]
    if panel == "panel-7":
        [override] = [o for o in spec["overrides"] if o["matcher"]["options"] == "State"]
        options = override["properties"][0]["value"][0]["options"]
    else:
        options = spec["defaults"]["mappings"][0]["options"]
    return {entry["text"]: int(code) for code, entry in options.items()}
