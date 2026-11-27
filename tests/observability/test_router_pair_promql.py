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

from narwhal.observability.stack import artifacts
from tests.observability.test_engine_state_promql import (
    RUN_TIMEOUT_S,
    SHIPPED,
    STALLED,
    UNAVAILABLE,
    promtool,
)

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
    # Requests that expired at their deadline in each scrape interval.
    expired: int = 0
    # Requests rejected at the router in-flight limit in each scrape interval.
    overloaded: int = 0
    # Attempts a standby refused while shadowing a live primary, which the load balancer
    # retries on the primary.
    standby_refused: int = 0
    # A new process starts its counters at these values: 0, or adopted totals before metrics 3.
    restart: tuple[int, int] | None = None
    ejected: int = 0
    # Decode engines in this router's pool view, and its membership generation.
    pool: int = 2
    generation: int = 3


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

        rejected = attempts = expired = overloaded = 0
        for phase in self.phases:
            if phase.restart is not None:
                self.offered, self.served = phase.restart
                rejected = attempts = expired = overloaded = 0
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
                self.offered += phase.served + phase.refused + phase.expired + phase.overloaded
                overloaded += phase.overloaded
                expired += phase.expired
                self.served += phase.served
                rejected += phase.refused
                attempts += phase.standby_refused
                put("narwhal_offered_total", self.labels, slot, self.offered)
                put("narwhal_served_total", self.labels, slot, self.served)
                not_ready = {**self.labels, "reason": "not_ready"}
                put("narwhal_rejected_total", not_ready, slot, rejected)
                put("narwhal_standby_refusals_total", not_ready, slot, attempts)
                inflight = {**self.labels, "reason": "inflight_limit"}
                put("narwhal_rejected_total", inflight, slot, overloaded)
                # The other outcomes stay at 0, as a router exports them from start.
                put("narwhal_refused_total", {**self.labels, "cause": "queue"}, slot, 0)
                put("narwhal_failed_total", {**self.labels, "reason": "engine_error"}, slot, 0)
                put("narwhal_expired_total", {**self.labels, "reason": "deadline"}, slot, expired)
                put("narwhal_cancelled_total", self.labels, slot, 0)
                put("narwhal_router_ready", self.labels, slot, phase.ready)
                put("narwhal_router_lease_epoch", self.labels, slot, phase.epoch)
                put("narwhal_ejected_instances", self.labels, slot, phase.ejected)
                decode = {**self.labels, "role": "decode"}
                put("narwhal_pool_instances", decode, slot, phase.pool)
                put("narwhal_membership_generation", self.labels, slot, phase.generation)
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
    count_attempts: bool = False,
) -> list[Router]:
    """A SIGTERM takeover at 600 s: router a drains, stops and restarts as a standby.

    The shipped HAProxy configuration sends a standby no requests. As a safety net for a load
    balancer that does, the restarted standby refuses 5 attempts in its first 10 s, and router b
    serves each retry. With `count_attempts`, the standby counts its refusals as offered and
    rejected requests, as routers did before they counted them apart.
    """
    a = [
        Phase(0, 600, "active", ready=1, epoch=7, served=10, restart=(HISTORY, HISTORY)),
        Phase(600, 610, "fenced", epoch=7, served=1),
        Phase(610, 625, "down"),
    ]
    if old_returns:
        # Its pool view and membership generation come from the fleet file until it takes over.
        restarted = {"pool": 3, "generation": 0}
        refusals = {"refused" if count_attempts else "standby_refused": 5}
        a += [
            Phase(625, 635, "standby", restart=(0, 0), **refusals, **restarted),
            Phase(635, LENGTH, "standby", **restarted),
        ]
    else:
        a += [Phase(625, LENGTH, "down")]
    b = [Phase(0, 605, "standby", ejected=standby_ejected, pool=3, generation=0)]
    if standby_takes_over:
        adopted = (HISTORY + 1200 + 2, HISTORY + 1200 + 2) if adopt else None
        b += [
            Phase(605, LENGTH, "active", ready=1, epoch=8, served=10, restart=adopted),
        ]
    else:
        b += [Phase(605, LENGTH, "standby")]
    return [Router(A, a), Router(B, b)]


def wave_hold() -> list[Router]:
    """A whole-wave hold on the active router from 300 s to 400 s, then in-flight limit refusals.

    During the hold the active router refuses 2 requests a second as not ready. From 400 s it
    serves again and rejects 0.4 requests a second at its in-flight limit.
    """
    a = [
        Phase(0, 300, "active", ready=1, epoch=7, served=10, restart=(HISTORY, HISTORY)),
        Phase(300, 400, "active", epoch=7, refused=10),
        Phase(400, LENGTH, "active", ready=1, epoch=7, served=8, overloaded=2),
    ]
    return [Router(A, a), Router(B, [Phase(0, LENGTH, "standby", pool=3, generation=0)])]


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


def samples_test(expr: str, at: int, expected: dict[str, float]) -> list[str]:
    """Expect `expr` at `at` seconds to return exactly the `expected` labelled samples."""
    lines = [
        f"      - expr: {json.dumps(expr)}",
        f"        eval_time: {at}s",
        "        exp_samples:" + ("" if expected else " []"),
    ]
    for labels, value in sorted(expected.items()):
        lines += [f"          - labels: {json.dumps(labels)}", f"            value: {value}"]
    return lines


def panel_query(panel: str, ref: str, window: str = "1m") -> str:
    """The panel query as Grafana runs it over every router, with `window` for its ranges."""
    element = artifacts.render_dashboard(SHIPPED)["spec"]["elements"][panel]["spec"]
    expr = next(
        query["spec"]["query"]["spec"]["expr"]
        for query in element["data"]["spec"]["queries"]
        if query["spec"]["refId"] == ref
    )
    return (
        expr.replace("$router", ".*")
        .replace("$iid", ".*")
        .replace("$__rate_interval", window)
        .replace("$__range", window)
    )


def marker_query(layer: str, step: int) -> str:
    """The annotation layer's query as Grafana runs it at `step` seconds."""
    annotations = artifacts.render_dashboard(SHIPPED)["spec"]["annotations"]
    spec = next(item["spec"] for item in annotations if item["spec"]["name"] == layer)
    return spec["query"]["spec"]["expr"].replace("$router", ".*").replace("$__interval", f"{step}s")


def series_labels(name: str, router: dict[str, str], **extra: str) -> str:
    labels = {"__name__": name, **router, **extra}
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


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
        # 2 requests a second for 19 minutes, plus the drain.
        tests += expr_test(within(self.TOTAL, 2200, 2400), LENGTH - 5, 1)
        tests += expr_test('sum(increase(narwhal_rejected_total{reason="not_ready"}[5m]))', 900, 0)
        # The standby counts the 10 attempts it refused apart.
        attempts = 'sum(increase(narwhal_standby_refusals_total{reason="not_ready"}[5m]))'
        tests += expr_test(within(attempts, 9, 13), 900, 1)
        self.run_promtool(takeover(), tests)

    def test_attempts_counted_as_requests_would_raise_the_rejected_share(self):
        """The standby's refusals, counted as rejected requests, read as a dropped share."""
        share = (
            'sum(rate(narwhal_rejected_total{job="narwhal-router"}[5m]))'
            ' / sum(rate(narwhal_offered_total{job="narwhal-router"}[5m]))'
        )
        tests = ["    promql_expr_test:", *expr_test(within(share, 0.01, 1), 700, 1)]
        self.run_promtool(takeover(count_attempts=True), tests)

    def test_adopted_totals_in_the_counters_would_spike_the_pair_rate(self):
        """The counter jump a takeover made before metrics version 3, read by the same query."""
        tests = ["    promql_expr_test:"]
        tests += expr_test(within(self.OFFERED, 1000, 1e9), 630, 1)
        tests += expr_test(within(self.TOTAL, 100_000, 1e9), LENGTH - 5, 1)
        self.run_promtool(takeover(adopt=True), tests)


class DashboardTakeoverTests(PromtoolCase):
    """The dashboard across a SIGTERM takeover with an immediate standby restart."""

    def check(self, tests: list[str]) -> None:
        self.run_promtool(takeover(), ["    promql_expr_test:", *tests])

    def test_counter_panels_sum_the_pair_without_a_spike(self):
        offered = panel_query("panel-11", "A")
        tests = []
        for at in range(90, LENGTH, 30):
            tests += expr_test(within(offered, 1.0, 3.0), at, 1)
        headline = panel_query("panel-105", "B", "19m")
        tests += expr_test(within(headline, 2200, 2400), LENGTH - 5, 1)
        self.check(tests)

    def test_the_standby_refusals_raise_no_rejected_request_or_dropped_share(self):
        rejected = panel_query("panel-105", "G", "19m")
        dropped = panel_query("panel-105", "E", "19m")
        rate = panel_query("panel-11", "E")
        tests = expr_test(rejected, LENGTH - 5, 0) + expr_test(dropped, LENGTH - 5, 0)
        for at in range(630, 700, 5):
            tests += expr_test(rate, at, 0)
        self.check(tests)

    def test_request_outcomes_draw_not_ready_rejections_apart_from_capacity(self):
        capacity = panel_query("panel-11", "E")
        not_ready = panel_query("panel-11", "I")
        tests = []
        for at in range(365, 400, 5):
            # The hold's refusals leave the capacity series at zero.
            tests += expr_test(capacity, at, 0)
            tests += expr_test(within(not_ready, 1.9, 2.1), at, 1)
        for at in range(465, 600, 30):
            tests += expr_test(within(capacity, 0.35, 0.45), at, 1)
            tests += expr_test(not_ready, at, 0)
        self.run_promtool(wave_hold(), ["    promql_expr_test:", *tests])

    def test_gauge_panels_and_the_router_stat_read_the_active_router(self):
        pool = panel_query("panel-10", "A")
        stat = panel_query("panel-1", "A")
        tests = []
        for at, router in ((300, A), (600, A), (615, B), (900, B)):
            # The draining router holds the newest lease until the standby claims the next one.
            decode = series_labels("narwhal_pool_instances", router, role="decode")
            tests += samples_test(pool, at, {decode: 2})
        for at, router in ((300, A), (615, B), (900, B)):
            tests += samples_test(stat, at, {series_labels("narwhal_router_ready", router): 1})
        self.check(tests)

    def test_router_roles_show_each_role_and_lease_epoch(self):
        roles = panel_query("panel-62", "A")
        epochs = panel_query("panel-62", "B")
        a, b = '{instance="a:8000"}', '{instance="b:8000"}'
        active, standby, fenced, down = 1, 2, 3, 4
        tests = []
        for at, expected in (
            (300, {a: active, b: standby}),
            (605, {a: fenced, b: active}),
            (615, {a: down, b: active}),
            (900, {a: standby, b: active}),
        ):
            tests += samples_test(roles, at, expected)
        tests += samples_test(epochs, 300, {a: 7, b: 0})
        tests += samples_test(epochs, 900, {a: 0, b: 8})
        self.check(tests)

    def test_the_takeover_marker_marks_the_new_lease_once(self):
        claimed = series_labels("narwhal_router_lease_epoch", B)
        tests = []
        for step, mark in ((15, 615), (60, 660)):
            query = marker_query("Router takeovers", step)
            for at in range(step, LENGTH, step):
                tests += samples_test(query, at, {claimed: 8} if at == mark else {})
        self.check(tests)

    def test_the_takeover_marker_marks_a_single_router_that_claims_a_new_epoch(self):
        """The marker compares each point with the one before, so it needs both samples."""
        restarted = [
            Router(
                A,
                [
                    Phase(0, 305, "active", ready=1, epoch=7),
                    Phase(305, 310, "down"),
                    Phase(310, LENGTH, "active", ready=1, epoch=8),
                ],
            )
        ]
        claimed = series_labels("narwhal_router_lease_epoch", A)
        tests = []
        for step, mark in ((15, 315), (60, 360)):
            query = marker_query("Router takeovers", step)
            for at in range(step, LENGTH, step):
                tests += samples_test(query, at, {claimed: 8} if at == mark else {})
        # A restart spanning a point leaves no sample there, so no point is marked.
        gap = [
            Router(
                A,
                [
                    Phase(0, 295, "active", ready=1, epoch=7),
                    Phase(295, 305, "down"),
                    Phase(305, LENGTH, "active", ready=1, epoch=8),
                ],
            )
        ]
        quiet = []
        query = marker_query("Router takeovers", 15)
        for at in range(15, LENGTH, 15):
            quiet += samples_test(query, at, {})
        self.run_promtool(restarted, ["    promql_expr_test:", *tests])
        self.run_promtool(gap, ["    promql_expr_test:", *quiet])

    def test_two_ready_routers_are_both_followed(self):
        routers = [
            Router(A, [Phase(0, LENGTH, "active", ready=1, epoch=7)]),
            Router(B, [Phase(0, 300, "standby"), Phase(300, LENGTH, "active", ready=1, epoch=8)]),
        ]
        stat = panel_query("panel-1", "A")
        pool = panel_query("panel-10", "A")
        ready = {series_labels("narwhal_router_ready", r): 1 for r in (A, B)}
        decode = {series_labels("narwhal_pool_instances", r, role="decode"): 2 for r in (A, B)}
        tests = samples_test(stat, 450, ready) + samples_test(pool, 450, decode)
        self.run_promtool(routers, ["    promql_expr_test:", *tests])

    def test_the_membership_marker_skips_the_takeover_and_the_restarted_standby(self):
        """The restarted router's generation changes after a step at which it was ready."""
        tests = []
        for step in (15, 45, 60):
            query = marker_query("Membership changes", step)
            for at in range(step, LENGTH, step):
                tests += samples_test(query, at, {})
        self.check(tests)


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

    def test_the_active_router_stopping_without_a_takeover_pages_once(self):
        down = {**A, "alertname": "NarwhalRouterDown", "severity": "page"}
        self.check_alerts(
            takeover(old_returns=False, standby_takes_over=False),
            {645: {}, 690: {}, 720: {}, 780: {"NarwhalRouterDown": [down]}},
        )

    def test_a_pair_whose_routers_answer_but_neither_is_ready_pages(self):
        routers = [
            Router(A, [Phase(0, 300, "active", ready=1, epoch=7), Phase(300, LENGTH, "fenced")]),
            Router(B, [Phase(0, LENGTH, "standby")]),
        ]
        none_ready = {"alertname": "NarwhalNoRouterReady", "severity": "page"}
        self.check_alerts(routers, {345: {}, 375: {"NarwhalNoRouterReady": [none_ready]}})

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

    def test_a_single_router_that_stops_pages_once(self):
        routers = [
            Router(A, [Phase(0, 300, "active", ready=1), Phase(300, LENGTH, "down")]),
        ]
        down = {**A, "alertname": "NarwhalRouterDown", "severity": "page"}
        self.check_alerts(routers, {375: {}, 450: {"NarwhalRouterDown": [down]}})

    def test_a_single_router_that_answers_but_is_not_ready_pages(self):
        # A fleet's only router that admits nothing is an outage, as for a pair.
        routers = [Router(A, [Phase(0, 300, "active", ready=1), Phase(300, LENGTH, "active")])]
        none_ready = {"alertname": "NarwhalNoRouterReady", "severity": "page"}
        self.check_alerts(
            routers,
            {
                345: {},
                375: {"NarwhalNoRouterReady": [none_ready]},
                900: {"NarwhalNoRouterReady": [none_ready]},
            },
        )

    def test_the_last_samples_of_a_router_no_longer_scraped_count_for_nothing(self):
        """A recreated Prometheus answers the previous origin's samples for 5 minutes."""
        old = {"job": "narwhal-router", "instance": "old:8000"}
        previous = Router(old, [Phase(0, 300, "active", ready=1, epoch=7)])
        routers = [previous, Router(A, [Phase(300, LENGTH, "active", ready=1, epoch=8)])]
        # Without a failed scrape, no staleness marker ends the previous origin's series.
        self.check_alerts(routers, {330: {}, 390: {}, 450: {}})

    def test_the_last_down_sample_of_a_router_no_longer_scraped_raises_no_alert(self):
        """A recreated Prometheus answers the previous origin's `up` of 0 for 5 minutes."""
        old = {"job": "narwhal-router", "instance": "old:8000"}
        previous = Router(old, [Phase(0, 280, "standby"), Phase(280, 300, "down")])
        pair = [
            Router(A, [Phase(300, LENGTH, "active", ready=1, epoch=8)]),
            Router(B, [Phase(300, LENGTH, "standby")]),
        ]
        self.check_alerts([previous, *pair], {at: {} for at in (450, 500, 580)})
        # With no active router, the stale `up` of 0 would page as a stopped router. The fenced
        # router admits nothing, which pages as no router ready, and only that.
        single = [Router(A, [Phase(300, LENGTH, "fenced")])]
        none_ready = {"alertname": "NarwhalNoRouterReady", "severity": "page"}
        self.check_alerts(
            [previous, *single],
            {at: {"NarwhalNoRouterReady": [none_ready]} for at in (450, 500, 580)},
        )

    def test_gauge_alerts_ignore_the_lease_epoch_of_a_router_no_longer_scraped(self):
        """The previous origin's higher epoch stays in the lookback without hiding the holder."""
        old = {"job": "narwhal-router", "instance": "old:8000"}
        previous = Router(old, [Phase(0, 300, "active", ready=1, epoch=9)])
        holder = Router(A, [Phase(300, LENGTH, "active", ready=1, epoch=8, ejected=1)])
        ejected = {**A, "alertname": "NarwhalEngineEjected", "severity": "page"}
        tests = ["    alert_rule_test:"]
        tests += alert_test(450, "NarwhalEngineEjected", [ejected], "1")
        tests += alert_test(580, "NarwhalEngineEjected", [ejected], "1")
        self.run_promtool([previous, holder], tests)

    def test_gauge_alerts_read_the_lease_holder(self):
        """A standby's own breaker view raises no alert until it holds the lease."""
        routers = takeover(standby_ejected=1)
        routers[1].phases[1].ejected = 1
        ejected = {**B, "alertname": "NarwhalEngineEjected", "severity": "page"}
        tests = ["    alert_rule_test:"]
        tests += alert_test(590, "NarwhalEngineEjected", [])
        tests += alert_test(690, "NarwhalEngineEjected", [ejected], "1")
        self.run_promtool(routers, tests)

    def test_share_alerts_divide_the_pair_s_outcomes_by_the_pair_s_requests(self):
        """Router b's own share is 100%; the pair's is 0.5%, below the 1% threshold."""
        routers = [
            Router(A, [Phase(0, LENGTH, "active", ready=1, epoch=7, served=200)]),
            Router(B, [Phase(0, LENGTH, "fenced", refused=1, expired=1)]),
        ]
        tests = ["    alert_rule_test:"]
        for at in (400, 900):
            for name in ("NarwhalRejectedRising", "NarwhalExpiredRising"):
                tests += alert_test(at, name, [])
        self.run_promtool(routers, tests)

    def test_a_standby_waiting_for_a_killed_primary_s_lease_counts_rejected_requests(self):
        """With no live primary, the standby's 503s reach the client and count as rejected."""
        routers = [
            Router(
                A,
                [
                    Phase(0, 600, "active", ready=1, epoch=7, served=10),
                    Phase(600, LENGTH, "down"),
                ],
            ),
            Router(
                B,
                [
                    Phase(0, 600, "standby"),
                    # The lease TTL runs out 30 s after the kill; until then every request fails.
                    Phase(600, 630, "standby", refused=10),
                    Phase(630, LENGTH, "active", ready=1, epoch=8, served=10),
                ],
            ),
        ]
        rules = (artifacts.BASE / "prometheus-alerts.yml").read_text()
        rule = re.search(r"alert: NarwhalRejectedRising\n\s+expr: (.+)\n", rules)[1]
        rejected = panel_query("panel-105", "G", "5m")
        dropped = panel_query("panel-105", "E", "5m")
        tests = ["    promql_expr_test:"]
        # 6 scrape intervals of 10 refusals, with increase() extrapolating to the window.
        tests += expr_test(within(rejected, 55, 70), 700, 1)
        tests += expr_test(within(dropped, 0.05, 1), 700, 1)
        tests += samples_test(within(rule, 0.01, 1), 700, {'{job="narwhal-router"}': 1})
        tests += expr_test('sum(narwhal_standby_refusals_total{reason="not_ready"})', 700, 0)
        self.run_promtool(routers, tests)

    def test_the_standby_refusals_raise_no_rejected_alert(self):
        """The load balancer retries each refused attempt, so no client request is rejected."""
        tests = ["    alert_rule_test:"]
        for at in (700, 900, 1000):
            tests += alert_test(at, "NarwhalRejectedRising", [])
        self.run_promtool(takeover(), tests)


if __name__ == "__main__":
    unittest.main()
