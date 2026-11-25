"""Evaluate the shipped router alerts and pair queries with promtool across a router takeover."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from tests.observability.test_engine_state_promql import (
    RUN_TIMEOUT_S,
    STALLED,
    UNAVAILABLE,
    promtool,
)
from tools.observability import artifacts

STEP = 5
LENGTH = 1200
SLOTS = LENGTH // STEP
A = {"job": "narwhal-router", "instance": "a:8000"}
B = {"job": "narwhal-router", "instance": "b:8000"}
# The active router's process has served this many requests before the test window.
HISTORY = 201_439
ROLES = ("active", "standby", "fenced")


@dataclass
class Phase:
    """One span of a router target's scrapes, from `start` to `end` seconds."""

    start: int
    end: int
    role: str
    ready: int = 0
    epoch: int = 0
    # Requests served, and requests refused as not ready, in each scrape interval.
    served: int = 0
    refused: int = 0
    # A new process starts its counters at these values: 0, or adopted totals before metrics 3.
    restart: tuple[int, int] | None = None
    ejected: int = 0


@dataclass
class Router:
    labels: dict[str, str]
    phases: list[Phase]
    offered: int = 0
    served: int = 0
    series: dict[str, list[str]] = field(default_factory=dict)

    def cells(self) -> dict[str, list[str]]:
        """Return each series' samples, `_` where the target has none."""

        def put(name: str, labels: dict[str, str], slot: int, value: object) -> None:
            key = name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
            self.series.setdefault(key, ["_"] * SLOTS)[slot] = str(value)

        rejected = 0
        for phase in self.phases:
            if phase.restart is not None:
                self.offered, self.served = phase.restart
                rejected = 0
            for slot in range(phase.start // STEP, min(phase.end, LENGTH) // STEP):
                put("up", self.labels, slot, 0 if phase.role == "down" else 1)
                if phase.role == "down":
                    # A failed scrape ends the target's other series with a staleness marker.
                    for key, cells in self.series.items():
                        if (
                            not key.startswith("up{")
                            and slot
                            and cells[slot - 1] not in ("_", "stale")
                        ):
                            cells[slot] = "stale"
                    continue
                self.offered += phase.served + phase.refused
                self.served += phase.served
                rejected += phase.refused
                put("narwhal_offered_total", self.labels, slot, self.offered)
                put("narwhal_served_total", self.labels, slot, self.served)
                not_ready = {**self.labels, "reason": "not_ready"}
                put("narwhal_rejected_total", not_ready, slot, rejected)
                put("narwhal_router_ready", self.labels, slot, phase.ready)
                put("narwhal_router_lease_epoch", self.labels, slot, phase.epoch)
                put("narwhal_ejected_instances", self.labels, slot, phase.ejected)
                for role in ROLES:
                    labels = {**self.labels, "role": role}
                    put("narwhal_router_role", labels, slot, int(role == phase.role))
        return self.series


def takeover(
    *,
    old_returns: bool = True,
    standby_takes_over: bool = True,
    adopt: bool = False,
    standby_ejected: int = 0,
) -> list[Router]:
    """A SIGTERM takeover at 600 s: router a drains, stops and restarts as a standby."""
    a = [
        Phase(0, 600, "active", ready=1, epoch=7, served=10, restart=(HISTORY, HISTORY)),
        Phase(600, 610, "fenced", epoch=7, served=1),
        Phase(610, 625, "down"),
    ]
    if old_returns:
        # The restarted process refuses 3 requests during the load balancer's 8 s grace.
        a += [
            Phase(625, 630, "standby", refused=3, restart=(0, 0)),
            Phase(630, LENGTH, "standby"),
        ]
    else:
        a += [Phase(625, LENGTH, "down")]
    b = [Phase(0, 605, "standby", ejected=standby_ejected)]
    if standby_takes_over:
        adopted = (HISTORY + 1200 + 2, HISTORY + 1200 + 2) if adopt else None
        b += [
            Phase(605, LENGTH, "active", ready=1, epoch=8, served=10, restart=adopted),
        ]
    else:
        b += [Phase(605, LENGTH, "standby")]
    return [Router(A, a), Router(B, b)]


class PromtoolCase(unittest.TestCase):
    # Set when Docker fails to run promtool in time, so the remaining cases skip at once.
    docker_stalled = False

    def run_promtool(self, routers: list[Router], tests: list[str]) -> None:
        if PromtoolCase.docker_stalled:
            self.skipTest(STALLED)
        with tempfile.TemporaryDirectory() as folder:
            command = promtool(Path(folder))
            if command is None:
                self.skipTest(UNAVAILABLE)
            shutil.copy(artifacts.BASE / "prometheus-alerts.yml", Path(folder) / "alerts.yml")
            lines = [
                "rule_files: [alerts.yml]",
                "evaluation_interval: 15s",
                "tests:",
                f"  - interval: {STEP}s",
                "    input_series:",
            ]
            for router in routers:
                for name, cells in router.cells().items():
                    lines += [
                        f"      - series: {json.dumps(name)}",
                        f"        values: {' '.join(cells)!r}",
                    ]
            lines += tests
            path = Path(folder) / "pair_test.yml"
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
                PromtoolCase.docker_stalled = True
                self.skipTest(STALLED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def expr_test(expr: str, at: int, value: float | None) -> list[str]:
    """Expect `expr` at `at` seconds to return one unlabelled `value`, or nothing for None."""
    lines = [
        f"      - expr: {json.dumps(expr)}",
        f"        eval_time: {at}s",
        "        exp_samples:" + (" []" if value is None else ""),
    ]
    if value is not None:
        lines += ["          - labels: '{}'", f"            value: {value}"]
    return lines


def within(expr: str, low: float, high: float) -> str:
    """1 when `expr` lies strictly between `low` and `high`, and 0 otherwise."""
    return f"(({expr}) > bool {low}) * (({expr}) < bool {high})"


def annotations(name: str, labels: dict[str, str], value: str) -> dict[str, str]:
    """Return the shipped annotations of alert `name`, rendered for `labels` and `value`."""
    rules = (artifacts.BASE / "prometheus-alerts.yml").read_text().split("- alert: ")
    rule = next(rule for rule in rules if rule.startswith(f"{name}\n"))
    out = {}
    for key in ("summary", "description"):
        found = re.search(rf"^ +{key}: (\".*\")$", rule, re.M)
        if found:
            text = json.loads(found[1]).replace("{{ $value }}", value)
            out[key] = re.sub(r"\{\{ \$labels\.(\w+) \}\}", lambda m: labels[m[1]], text)
    return out


def alert_test(at: int, name: str, firing: list[dict[str, str]], value: str = "") -> list[str]:
    """Expect alert `name` at `at` seconds to fire with exactly the `firing` labels."""
    lines = [
        f"      - eval_time: {at}s",
        f"        alertname: {name}",
        "        exp_alerts:" + ("" if firing else " []"),
    ]
    for labels in firing:
        lines.append("          - exp_labels:")
        lines += [f"              {key}: {json.dumps(item)}" for key, item in labels.items()]
        lines.append("            exp_annotations:")
        lines += [
            f"              {key}: {json.dumps(item)}"
            for key, item in annotations(name, labels, value).items()
        ]
    return lines


ROUTER_ALERTS = (
    "NarwhalRouterDown",
    "NarwhalStandbyRouterDown",
    "NarwhalNoRouterReady",
    "NarwhalMultipleRoutersReady",
)


class CounterContinuityTests(PromtoolCase):
    """Request rates summed over a pair stay at the offered rate across a takeover."""

    OFFERED = 'sum(rate(narwhal_offered_total{job="narwhal-router"}[1m]))'
    TOTAL = 'sum(increase(narwhal_offered_total{job="narwhal-router"}[19m]))'

    def test_the_pair_rate_has_no_spike_when_the_old_router_restarts_as_standby(self):
        tests = ["    promql_expr_test:"]
        for at in range(90, LENGTH, 30):
            tests += expr_test(within(self.OFFERED, 1.0, 3.0), at, 1)
        # 2 requests a second for 19 minutes, plus the drain and the 3 refusals.
        tests += expr_test(within(self.TOTAL, 2200, 2400), LENGTH - 5, 1)
        tests += expr_test(
            within('sum(increase(narwhal_rejected_total{reason="not_ready"}[5m]))', 2, 5),
            900,
            1,
        )
        self.run_promtool(takeover(), tests)

    def test_adopted_totals_in_the_counters_would_spike_the_pair_rate(self):
        """The counter jump a takeover made before metrics version 3, read by the same query."""
        tests = ["    promql_expr_test:"]
        tests += expr_test(within(self.OFFERED, 1000, 1e9), 630, 1)
        tests += expr_test(within(self.TOTAL, 100_000, 1e9), LENGTH - 5, 1)
        self.run_promtool(takeover(adopt=True), tests)


class RouterAlertTests(PromtoolCase):
    def check_alerts(self, routers: list[Router], expected: dict[int, dict[str, list]]) -> None:
        tests = ["    alert_rule_test:"]
        for at, alerts in expected.items():
            for name in ROUTER_ALERTS:
                value = "2" if name == "NarwhalMultipleRoutersReady" else ""
                tests += alert_test(at, name, alerts.get(name, []), value)
        self.run_promtool(routers, tests)

    def test_a_takeover_with_an_immediate_standby_restart_raises_no_router_alert(self):
        self.check_alerts(takeover(), {at: {} for at in (615, 660, 750, 900, 1195)})

    def test_the_active_router_stopping_without_a_takeover_pages(self):
        down = {**A, "alertname": "NarwhalRouterDown", "severity": "page"}
        none_ready = {"alertname": "NarwhalNoRouterReady", "severity": "page"}
        self.check_alerts(
            takeover(old_returns=False, standby_takes_over=False),
            {
                645: {},
                675: {"NarwhalNoRouterReady": [none_ready]},
                780: {"NarwhalRouterDown": [down], "NarwhalNoRouterReady": [none_ready]},
            },
        )

    def test_a_stopped_router_after_a_takeover_warns_that_the_pair_has_no_standby(self):
        standby = {**A, "alertname": "NarwhalStandbyRouterDown", "severity": "warn"}
        self.check_alerts(
            takeover(old_returns=False),
            {720: {}, 780: {"NarwhalStandbyRouterDown": [standby]}},
        )

    def test_two_ready_routers_page(self):
        routers = [
            Router(A, [Phase(0, LENGTH, "active", ready=1, epoch=7)]),
            Router(
                B,
                [
                    Phase(0, 300, "standby"),
                    Phase(300, LENGTH, "active", ready=1, epoch=8),
                ],
            ),
        ]
        both = {"alertname": "NarwhalMultipleRoutersReady", "severity": "page"}
        self.check_alerts(routers, {315: {}, 360: {"NarwhalMultipleRoutersReady": [both]}})

    def test_a_single_router_that_stops_pages(self):
        routers = [
            Router(A, [Phase(0, 300, "active", ready=1), Phase(300, LENGTH, "down")]),
        ]
        down = {**A, "alertname": "NarwhalRouterDown", "severity": "page"}
        none_ready = {"alertname": "NarwhalNoRouterReady", "severity": "page"}
        self.check_alerts(
            routers,
            {
                345: {},
                375: {"NarwhalNoRouterReady": [none_ready]},
                450: {"NarwhalRouterDown": [down], "NarwhalNoRouterReady": [none_ready]},
            },
        )

    def test_gauge_alerts_read_the_lease_holder(self):
        """A standby's own breaker view raises no alert until it holds the lease."""
        routers = takeover(standby_ejected=1)
        routers[1].phases[1].ejected = 1
        ejected = {**B, "alertname": "NarwhalEngineEjected", "severity": "page"}
        tests = ["    alert_rule_test:"]
        tests += alert_test(590, "NarwhalEngineEjected", [])
        tests += alert_test(690, "NarwhalEngineEjected", [ejected], "1")
        self.run_promtool(routers, tests)

    def test_pair_share_alerts_divide_by_the_requests_of_both_routers(self):
        """The restarted standby's 3 refusals are 100% of its own requests but not of the pair's."""
        tests = ["    alert_rule_test:"]
        for at in (700, 900):
            tests += alert_test(at, "NarwhalRejectedRising", [])
        self.run_promtool(takeover(), tests)


if __name__ == "__main__":
    unittest.main()
