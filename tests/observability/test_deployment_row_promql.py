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
        # Another fleet's failed stage stays out of this fleet's row.
        inputs += series(
            "narwhal_deployment_stage_state",
            'job="narwhal-router",instance="c:8000",fleet="fleet-b",stage="install",'
            'stage_instance="node-9",state="failed"',
            "1x40",
        )
        tests = [
            "    promql_expr_test:",
            *expr_test(deployment_panel("Stages not passed"), 300, 1),
            *samples_test(
                deployment_panel("Stage table"),
                300,
                {'{stage="preflight",stage_instance="node-1",state="failed"}': 1},
            ),
            *samples_test(deployment_panel("Fabric edges"), 300, {'{edge="n1-n2"}': 80}),
            *expr_test(deployment_panel("Deployment age"), 300, 300),
        ]
        self.run_promtool(inputs, tests)

    def test_a_passed_run_shows_no_stage_left(self):
        inputs = series(
            "narwhal_deployment_stage_state",
            'job="narwhal-router",instance="a:8000",fleet="fleet-a",stage="install",'
            'stage_instance="node-1",state="passed"',
            "1x40",
        )
        tests = ["    promql_expr_test:", *expr_test(deployment_panel("Stages not passed"), 300, 0)]
        self.run_promtool(inputs, tests)

    def alert_at(self, seconds: int, name: str, firing: list[dict[str, str]]) -> list[str]:
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
                text = template.replace("{{ $labels.instance }}", labels["instance"])
                lines += [f"              {key}: {json.dumps(text)}"]
        return lines

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

    def test_router_down_fires_while_both_router_hosts_reboot(self):
        # Prometheus reads the router targets from a file, so it keeps scraping both routers
        # while their hosts are down.
        inputs = []
        down = []
        for router in ("a:8000", "b:8000"):
            labels = {"job": "narwhal-router", "instance": router, "fleet": "fleet-a"}
            inputs += series("up", ",".join(f'{k}="{v}"' for k, v in labels.items()), REBOOT)
            down.append(labels)
        tests = [
            "    alert_rule_test:",
            *self.alert_at(590, "NarwhalRouterDown", []),
            *self.alert_at(780, "NarwhalRouterDown", down),
            *self.alert_at(1000, "NarwhalRouterDown", []),
        ]
        self.run_promtool(inputs, tests)


if __name__ == "__main__":
    unittest.main()
