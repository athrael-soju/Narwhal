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
ACTIVE = 'job="narwhal-router",instance="a:8000",fleet="fleet-a"'
STANDBY = 'job="narwhal-router",instance="b:8000",fleet="fleet-a"'
OTHER = 'job="narwhal-router",instance="c:8000",fleet="fleet-b"'


def engine(iid: str, fleet: str = "fleet-a") -> str:
    return f'job="engines",instance="{iid}:8100",iid="{iid}",fleet="{fleet}"'


def trial_fleet(*, n2_up: str = "1x40", served: str = "0+2x40", decode: str | None = "0x10 0.3x30"):
    """A fleet of two engines behind a router pair through a trial, beside another fleet."""
    inputs = []
    inputs += series("up", engine("n1"), "1x40")
    inputs += series("up", engine("n2"), n2_up)
    inputs += series("narwhal_served_total", ACTIVE, served)
    inputs += series("narwhal_served_total", STANDBY, "0x40")
    inputs += series("narwhal_decode_tokens_observed_total", ACTIVE, "0+256x40")
    inputs += series("narwhal_decode_tokens_observed_total", STANDBY, "0x40")
    inputs += series("vllm:prompt_tokens_total", engine("n1"), "0+16384x40")
    inputs += series("vllm:generation_tokens_total", engine("n2"), "0+256x40")
    for router in (ACTIVE, STANDBY):
        inputs += series("narwhal_instance_role", f'{router},iid="n1",role="prefill"', "1x40")
        inputs += series("narwhal_instance_role", f'{router},iid="n2",role="decode"', "1x40")
        inputs += series("narwhal_pool_load", f'{router},role="prefill"', "0x10 0.5x30")
        if decode is not None:
            inputs += series("narwhal_pool_load", f'{router},role="decode"', decode)
    # The neighbour fleet serves, and its engine is down, without touching fleet-a's checks.
    inputs += series("up", engine("n3", "fleet-b"), "0x40")
    inputs += series("narwhal_served_total", OTHER, "0+5x40")
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
        text = trial.expression(check, "fleet-a", WINDOW_S)
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
