"""Evaluate the capacity trial's series checks with promtool, over a fleet and its neighbour."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from narwhal.deployment import trial
from narwhal.observability.stack import artifacts
from tests.observability.test_deployment_row_promql import series
from tests.observability.test_engine_state_promql import (
    RUN_TIMEOUT_S,
    STALLED,
    UNAVAILABLE,
    promtool,
)
from tests.observability.test_router_pair_promql import samples_test

# A ten-minute trial, evaluated at its end over its whole length.
EVAL_S = 600
WINDOW_S = 600
# The fixture's scrape interval, the promtool test's.
SCRAPE_S = 15
ACTIVE = 'job="narwhal-router",instance="a:8000",fleet="fleet-a"'
STANDBY = 'job="narwhal-router",instance="b:8000",fleet="fleet-a"'
OTHER = 'job="narwhal-router",instance="c:8000",fleet="fleet-b"'


def engine(iid: str, fleet: str = "fleet-a") -> str:
    return f'job="engines",instance="{iid}:8100",iid="{iid}",fleet="{fleet}"'


def trial_fleet(
    *,
    n2_up: str = "1x40",
    served: str = "0+2x40",
    decode: str | None = "0x10 0.3x30",
    active_role: str = "1x40",
    active_series: str = "1x40",
):
    """A fleet of two engines behind a router pair through a trial, beside another fleet.

    The active router reports both engines and both pools; `decode` is its decode pool's load,
    `active_series` the samples of its role and pool series, and `active_role` its active-role
    gauge. The standby reports only n1, as prefill, and only a decode pool, so a check that
    read the standby would see series the active router did not report.
    """
    inputs = []
    inputs += series("up", engine("n1"), "1x40")
    inputs += series("up", engine("n2"), n2_up)
    inputs += series("narwhal_served_total", ACTIVE, served)
    inputs += series("narwhal_served_total", STANDBY, "0x40")
    inputs += series("narwhal_decode_tokens_observed_total", ACTIVE, "0+256x40")
    inputs += series("narwhal_decode_tokens_observed_total", STANDBY, "0x40")
    inputs += series("vllm:prompt_tokens_total", engine("n1"), "0+16384x40")
    inputs += series("vllm:generation_tokens_total", engine("n2"), "0+256x40")
    inputs += series("narwhal_router_role", f'{ACTIVE},role="active"', active_role)
    inputs += series("narwhal_router_role", f'{ACTIVE},role="standby"', "0x40")
    inputs += series("narwhal_router_role", f'{STANDBY},role="active"', "0x40")
    inputs += series("narwhal_router_role", f'{STANDBY},role="standby"', "1x40")
    held = active_series
    inputs += series("narwhal_instance_role", f'{ACTIVE},iid="n1",role="prefill"', held)
    inputs += series("narwhal_instance_role", f'{ACTIVE},iid="n2",role="decode"', held)
    inputs += series("narwhal_pool_load", f'{ACTIVE},role="prefill"', "0x10 0.5x30")
    if decode is not None:
        inputs += series("narwhal_pool_load", f'{ACTIVE},role="decode"', decode)
    inputs += series("narwhal_instance_role", f'{STANDBY},iid="n1",role="prefill"', "1x40")
    inputs += series("narwhal_instance_role", f'{STANDBY},iid="n2",role="prefill"', "1x40")
    inputs += series("narwhal_pool_load", f'{STANDBY},role="decode"', "0.2x40")
    # The neighbour fleet serves, and its engine is down, without touching fleet-a's checks.
    inputs += series("up", engine("n3", "fleet-b"), "0x40")
    inputs += series("narwhal_served_total", OTHER, "0+5x40")
    inputs += series("narwhal_router_role", f'{OTHER},role="active"', "1x40")
    inputs += series("narwhal_pool_load", f'{OTHER},role="decode"', "0.9x40")
    return inputs


def expected(failing: dict[str, dict[str, float | None]] | None = None) -> list[list[str]]:
    """The samples each check returns at the trial's end: every sample 1 unless `failing`
    gives a check's samples by its meaning, where None means the check returns no sample."""
    found = []
    for check in trial.SERIES:
        if check.per == "iid":
            samples = {'{iid="n1"}': 1.0, '{iid="n2"}': 1.0}
        elif check.per == "role":
            samples = {'{role="decode"}': 1.0, '{role="prefill"}': 1.0}
        else:
            samples = {"{}": 1.0}
        for labels, value in (failing or {}).get(check.meaning, {}).items():
            if value is None:
                samples.pop(labels)
            else:
                samples[labels] = value
        text = trial.expression(check, "fleet-a", WINDOW_S, SCRAPE_S)
        found.append(samples_test(text, EVAL_S, samples))
    return found


class TrialSeriesTests(unittest.TestCase):
    docker_stalled = False

    def run_promtool(self, inputs: list[str], tests: list[list[str]]) -> None:
        if TrialSeriesTests.docker_stalled:
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
                "    promql_expr_test:",
                *(line for test in tests for line in test),
            ]
            path = Path(folder) / "trial_test.yml"
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
                TrialSeriesTests.docker_stalled = True
                self.skipTest(STALLED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_check_holds_for_a_fleet_that_served_the_trial(self):
        self.run_promtool(trial_fleet(), expected())

    def test_a_pool_whose_engines_stay_at_their_idle_pace_still_shows(self):
        self.run_promtool(trial_fleet(decode="0x40"), expected())

    def test_a_pool_only_the_standby_reports_fails_the_pool_load_check(self):
        meanings = {check.name: check.meaning for check in trial.SERIES}
        self.run_promtool(
            trial_fleet(decode=None),
            expected({meanings["pool-load"]: {'{role="decode"}': None}}),
        )

    def test_series_the_active_router_stopped_reporting_partway_fail_their_checks(self):
        # The active router's engine-role and pool series, and n2's scrape target, end halfway
        # through the window.
        meanings = {check.name: check.meaning for check in trial.SERIES}
        self.run_promtool(
            trial_fleet(active_series="1x20", decode="0x20", n2_up="1x20"),
            expected(
                {
                    meanings["engine"]: {'{iid="n2"}': 0.0},
                    meanings["role"]: {'{iid="n1"}': 0.0, '{iid="n2"}': 0.0},
                    meanings["pool-load"]: {'{role="decode"}': 0.0},
                }
            ),
        )

    def test_a_router_that_took_control_partway_holds_no_pool_or_role(self):
        meanings = {check.name: check.meaning for check in trial.SERIES}
        self.run_promtool(
            trial_fleet(active_role="0x20 1x20"),
            expected(
                {
                    meanings["role"]: {'{iid="n1"}': None, '{iid="n2"}': None},
                    meanings["pool-load"]: {'{role="decode"}': None, '{role="prefill"}': None},
                }
            ),
        )

    def test_a_down_engine_an_unreported_pool_and_no_served_requests_fail_their_checks(self):
        meanings = {check.name: check.meaning for check in trial.SERIES}
        self.run_promtool(
            trial_fleet(n2_up="1x20 0 1x19", served="0x40", decode=None),
            expected(
                {
                    meanings["engine"]: {'{iid="n2"}': 0.0},
                    meanings["request"]: {"{}": 0.0},
                    meanings["pool-load"]: {'{role="decode"}': None},
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
