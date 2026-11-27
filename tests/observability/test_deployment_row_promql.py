"""Evaluate the dashboard's Deployment row and the host-down alert with promtool."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from narwhal.observability.stack import artifacts
from tests.observability.test_engine_state_promql import (
    RUN_TIMEOUT_S,
    SHIPPED,
    STALLED,
    UNAVAILABLE,
    promtool,
)
from tests.observability.test_router_pair_promql import expr_test, samples_test

ROUTERS = ('instance="a:8000"', 'instance="b:8000"')
ALERTS = [
    rule
    for group in yaml.safe_load((artifacts.BASE / "prometheus-alerts.yml").read_text())["groups"]
    for rule in group["rules"]
]
SIDECAR = 'job="sidecars",instance="10.0.0.1:8200",fleet="fleet-a"'
# A sidecar host scraped for 10 minutes, down for 5 while it reboots, then back.
REBOOT = "1x40 0x20 1x20"


def deployment_panel(title: str, ref: str = "A") -> str:
    """The `ref` query of the Deployment row panel `title`, as the served dashboard runs it."""
    served = artifacts.narwhal_dashboard(SHIPPED, "http://127.0.0.1:18020/console")
    [panel] = [
        element["spec"]
        for element in served["spec"]["elements"].values()
        if element["spec"]["title"] == title
    ]
    [query] = [q for q in panel["data"]["spec"]["queries"] if q["spec"]["refId"] == ref]
    return str(query["spec"]["query"]["spec"]["expr"]).replace("$fleet", "fleet-a")


def series(name: str, labels: str, values: str) -> list[str]:
    return [f"      - series: {json.dumps(f'{name}{{{labels}}}')}", f"        values: {values!r}"]


class DeploymentRowTests(unittest.TestCase):
    docker_stalled = False

    def run_promtool(self, inputs: list[str], tests: list[str]) -> None:
        if DeploymentRowTests.docker_stalled:
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
                "  - interval: 15s",
                "    input_series:",
                *inputs,
                *tests,
            ]
            path = Path(folder) / "deployment_test.yml"
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
                DeploymentRowTests.docker_stalled = True
                self.skipTest(STALLED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_both_routers_of_a_pair_count_a_failed_stage_once(self):
        inputs = []
        for router in ROUTERS:
            base = f'job="narwhal-router",{router},fleet="fleet-a"'
            inputs += series(
                "narwhal_deployment_stage_state",
                f'{base},stage="preflight",stage_instance="node-1",state="failed"',
                "1x40",
            )
            inputs += series(
                "narwhal_deployment_stage_state",
                f'{base},stage="install",stage_instance="node-1",state="passed"',
                "1x40",
            )
            inputs += series("narwhal_fabric_edge_measured_gbps", f'{base},edge="n1-n2"', "80x40")
            inputs += series("narwhal_deployment_last_passed_timestamp_seconds", base, "0x40")
            inputs += series("narwhal_deployment_updated_timestamp_seconds", base, "50x40")
        # Another fleet's failed stage stays out of this fleet's row.
        inputs += series(
            "narwhal_deployment_stage_state",
            'job="narwhal-router",instance="c:8000",fleet="fleet-b",stage="install",'
            'stage_instance="node-9",state="failed"',
            "1x40",
        )
        inputs += series(
            "narwhal_deployment_updated_timestamp_seconds",
            'job="narwhal-router",instance="c:8000",fleet="fleet-b"',
            "50x40",
        )
        fleet = '{fleet="fleet-a"}'
        tests = [
            "    promql_expr_test:",
            *samples_test(deployment_panel("Stages not passed"), 300, {fleet: 1}),
            *samples_test(
                deployment_panel("Stage table"),
                300,
                {'{fleet="fleet-a",stage="preflight",stage_instance="node-1",state="failed"}': 1},
            ),
            *samples_test(
                deployment_panel("Fabric edges"), 300, {'{edge="n1-n2",fleet="fleet-a"}': 80}
            ),
            *samples_test(deployment_panel("Deployment age"), 300, {fleet: 300}),
        ]
        self.run_promtool(inputs, tests)

    def test_a_passed_run_shows_no_stage_left(self):
        inputs = series(
            "narwhal_deployment_stage_state",
            'job="narwhal-router",instance="a:8000",fleet="fleet-a",stage="install",'
            'stage_instance="node-1",state="passed"',
            "1x40",
        )
        inputs += series(
            "narwhal_deployment_updated_timestamp_seconds",
            'job="narwhal-router",instance="a:8000",fleet="fleet-a"',
            "50x40",
        )
        tests = ["    promql_expr_test:", *expr_test(deployment_panel("Stages not passed"), 300, 0)]
        self.run_promtool(inputs, tests)

    def test_a_router_left_with_an_older_status_does_not_show(self):
        # The last run's upload reached router a only; router b still exports the run before,
        # which stopped at another stage.
        inputs = []
        for router, updated, stage in (
            ("a:8000", "200x40", "monitoring"),
            ("b:8000", "100x40", "install"),
        ):
            base = f'job="narwhal-router",instance="{router}",fleet="fleet-a"'
            inputs += series("narwhal_deployment_updated_timestamp_seconds", base, updated)
            inputs += series(
                "narwhal_deployment_stage_state",
                f'{base},stage="{stage}",stage_instance="",state="failed"',
                "1x40",
            )
        tests = [
            "    promql_expr_test:",
            *samples_test(deployment_panel("Stages not passed"), 300, {'{fleet="fleet-a"}': 1}),
            *samples_test(
                deployment_panel("Stage table"),
                300,
                {'{fleet="fleet-a",stage="monitoring",state="failed"}': 1},
            ),
        ]
        self.run_promtool(inputs, tests)

    def alert_at(
        self, seconds: int, name: str, firing: list[dict[str, str]], value: str = ""
    ) -> list[str]:
        rules = {rule["alert"]: rule for rule in ALERTS}
        lines = [
            f"      - eval_time: {seconds}s",
            f"        alertname: {name}",
            "        exp_alerts:" + ("" if firing else " []"),
        ]
        for labels in firing:
            values = {**labels, **rules[name]["labels"]}
            lines += ["          - exp_labels:"]
            lines += [f"              {key}: {json.dumps(v)}" for key, v in values.items()]
            lines += ["            exp_annotations:"]
            for key, template in rules[name]["annotations"].items():
                text = template.replace("{{ $labels.instance }}", labels.get("instance", ""))
                text = text.replace("{{ $value }}", value)
                lines += [f"              {key}: {json.dumps(text)}"]
        return lines

    def test_router_alerts_hold_within_each_of_two_fleets(self):
        # In each fleet one router is active and ready. Fleet A's other router is down, and
        # fleet A's active router ejects an engine at a lower epoch than fleet B's.
        a = 'job="narwhal-router",instance="a:8000",fleet="fleet-a"'
        b = 'job="narwhal-router",instance="b:8000",fleet="fleet-b"'
        c = 'job="narwhal-router",instance="c:8000",fleet="fleet-b"'
        d = 'job="narwhal-router",instance="d:8000",fleet="fleet-a"'
        inputs = [
            *series("up", a, "0x40"),
            *series("up", b, "1x40"),
            *series("narwhal_router_role", b + ',role="active"', "1x40"),
            *series("narwhal_router_ready", b, "1x40"),
            *series("up", c, "1x40"),
            *series("narwhal_router_ready", c, "0x40"),
            *series("narwhal_router_lease_epoch", b, "9x40"),
            *series("up", d, "1x40"),
            *series("narwhal_router_role", d + ',role="active"', "1x40"),
            *series("narwhal_router_ready", d, "1x40"),
            *series("narwhal_router_lease_epoch", d, "3x40"),
            *series("narwhal_ejected_instances", d, "1x40"),
        ]
        down = {"job": "narwhal-router", "instance": "a:8000", "fleet": "fleet-a"}
        ejected = {"job": "narwhal-router", "instance": "d:8000", "fleet": "fleet-a"}
        tests = [
            "    alert_rule_test:",
            # Fleet A's own active router leaves its down router a standby to restart.
            *self.alert_at(300, "NarwhalRouterDown", []),
            *self.alert_at(300, "NarwhalStandbyRouterDown", [down]),
            # One ready router in each fleet is not two at once.
            *self.alert_at(300, "NarwhalMultipleRoutersReady", []),
            # Fleet B's higher epoch does not hide fleet A's lease holder.
            *self.alert_at(300, "NarwhalEngineEjected", [ejected], value="1"),
        ]
        self.run_promtool(inputs, tests)
        # Without router d, fleet A has no active router, and fleet B's does not stand in.
        tests = [
            "    alert_rule_test:",
            *self.alert_at(300, "NarwhalRouterDown", [down]),
            *self.alert_at(300, "NarwhalStandbyRouterDown", []),
        ]
        without = [
            line
            for pair in zip(inputs[::2], inputs[1::2], strict=True)
            if "d:8000" not in pair[0]
            for line in pair
        ]
        self.run_promtool(without, tests)

    def test_host_down_fires_while_a_sidecar_host_reboots_and_clears_after(self):
        inputs = series("up", SIDECAR, REBOOT)
        host = {"job": "sidecars", "instance": "10.0.0.1:8200", "fleet": "fleet-a"}
        tests = [
            "    alert_rule_test:",
            *self.alert_at(590, "NarwhalHostDown", []),
            *self.alert_at(720, "NarwhalHostDown", [host]),
            *self.alert_at(1000, "NarwhalHostDown", []),
        ]
        self.run_promtool(inputs, tests)

    def test_router_down_fires_while_the_standby_host_reboots_after_the_primary_stopped(self):
        # Prometheus runs on the primary router's host, which stays up: the primary router has
        # stopped, and the standby's host reboots, so no router of the pair is active. The
        # router targets come from a file, so Prometheus keeps scraping the standby through
        # its reboot.
        primary = {"job": "narwhal-router", "instance": "a:8000", "fleet": "fleet-a"}
        standby = {"job": "narwhal-router", "instance": "b:8000", "fleet": "fleet-a"}
        inputs = []
        for labels, values in ((primary, "0x80"), (standby, REBOOT)):
            inputs += series("up", ",".join(f'{k}="{v}"' for k, v in labels.items()), values)
        tests = [
            "    alert_rule_test:",
            *self.alert_at(590, "NarwhalRouterDown", [primary]),
            *self.alert_at(780, "NarwhalRouterDown", [primary, standby]),
            *self.alert_at(1000, "NarwhalRouterDown", [primary]),
        ]
        self.run_promtool(inputs, tests)

    def test_a_fleet_with_one_router_raises_no_pair_alert(self):
        # No standby: the only router is active and ready at lease epoch 0, and ejects an engine.
        router = 'job="narwhal-router",instance="a:8000",fleet="fleet-a"'
        labels = {"job": "narwhal-router", "instance": "a:8000", "fleet": "fleet-a"}
        inputs = [
            *series("up", router, "1x40"),
            *series("narwhal_router_role", router + ',role="active"', "1x40"),
            *series("narwhal_router_role", router + ',role="standby"', "0x40"),
            *series("narwhal_router_ready", router, "1x40"),
            *series("narwhal_router_lease_epoch", router, "0x40"),
            *series("narwhal_ejected_instances", router, "1x40"),
        ]
        tests = [
            "    alert_rule_test:",
            *self.alert_at(300, "NarwhalRouterDown", []),
            *self.alert_at(300, "NarwhalStandbyRouterDown", []),
            *self.alert_at(300, "NarwhalNoRouterReady", []),
            *self.alert_at(300, "NarwhalMultipleRoutersReady", []),
            *self.alert_at(300, "NarwhalEngineEjected", [labels], value="1"),
        ]
        self.run_promtool(inputs, tests)

    def test_a_fleets_only_router_that_admits_nothing_pages(self):
        # Fleet B's ready router does not stand in for fleet A's only router.
        router = 'job="narwhal-router",instance="a:8000",fleet="fleet-a"'
        other = 'job="narwhal-router",instance="c:8000",fleet="fleet-b"'
        inputs = [
            *series("up", router, "1x40"),
            *series("narwhal_router_role", router + ',role="active"', "1x40"),
            *series("narwhal_router_ready", router, "0x40"),
            *series("up", other, "1x40"),
            *series("narwhal_router_role", other + ',role="active"', "1x40"),
            *series("narwhal_router_ready", other, "1x40"),
        ]
        tests = [
            "    alert_rule_test:",
            *self.alert_at(300, "NarwhalNoRouterReady", [{"fleet": "fleet-a"}]),
            *self.alert_at(300, "NarwhalRouterDown", []),
        ]
        self.run_promtool(inputs, tests)

    def test_a_fleets_only_router_that_stops_pages_once(self):
        router = 'job="narwhal-router",instance="a:8000",fleet="fleet-a"'
        down = {"job": "narwhal-router", "instance": "a:8000", "fleet": "fleet-a"}
        tests = [
            "    alert_rule_test:",
            *self.alert_at(300, "NarwhalRouterDown", [down]),
            *self.alert_at(300, "NarwhalStandbyRouterDown", []),
            *self.alert_at(300, "NarwhalNoRouterReady", []),
        ]
        self.run_promtool(series("up", router, "0x40"), tests)

    def test_the_watchdog_fires_for_each_fleet_while_prometheus_runs(self):
        inputs = [
            *series("up", 'job="narwhal-router",instance="a:8000",fleet="fleet-a"', "0x40"),
            *series("up", 'job="narwhal-router",instance="c:8000",fleet="fleet-b"', "1x40"),
        ]
        tests = [
            "    alert_rule_test:",
            *self.alert_at(300, "NarwhalWatchdog", [{"fleet": "fleet-a"}, {"fleet": "fleet-b"}]),
        ]
        self.run_promtool(inputs, tests)


if __name__ == "__main__":
    unittest.main()
