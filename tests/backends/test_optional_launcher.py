import dataclasses
import unittest
from unittest.mock import patch

from narwhal.backends import load
from narwhal.deployment.launch_engine import cli
from narwhal.deployment.launch_engine.backend import launcher
from tests.backends import contract, test_contract

BARE = dataclasses.replace(load("vllm"), launcher=None)
# The contract tests that read the launcher.
LAUNCH_TESTS = {
    "VllmContractTests": {
        "test_launcher_offline_surface",
        "test_extra_args_reject_every_flag_a_neutral_setting_maps",
        "test_launcher_discovers_a_valid_runtime_for_each_connector",
        "test_launcher_rejects_invalid_discovery_settings",
    },
    "VllmCharacterizationTests": {
        "test_launch_command_and_environment",
        "test_runtime_check_result",
        "test_attestation_document",
    },
}


class OptionalLauncherTests(unittest.TestCase):
    def skipped(self, case: str) -> tuple[set[str], int]:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(getattr(test_contract, case))
        result = unittest.TestResult()
        with patch.object(test_contract, "load", return_value=BARE):
            suite.run(result)
        self.assertEqual((result.failures, result.errors), ([], []))
        return {test.id().rsplit(".", 1)[1] for test, _ in result.skipped}, result.testsRun

    def test_launch_tools_refuse_a_backend_with_no_launcher(self):
        with self.assertRaisesRegex(ValueError, "backend 'vllm' has no engine launcher"):
            BARE.engine_launcher()
        with (
            patch("narwhal.backends.load", return_value=BARE),
            self.assertRaisesRegex(ValueError, "backend 'vllm' has no engine launcher"),
        ):
            launcher("vllm")

    def test_capture_actions_and_launch_tests_leave_the_backend_out(self):
        with patch.object(cli, "load_backend", return_value=BARE):
            self.assertEqual(cli.capture_backends(), [])
        with patch.object(contract, "load_backend", return_value=BARE):
            self.assertEqual(contract.with_launcher(), [])

    def test_the_contract_suite_skips_only_its_launch_tests(self):
        for case, tests in LAUNCH_TESTS.items():
            with self.subTest(case=case):
                self.assertEqual(self.skipped(case)[0], tests)
        skipped, ran = self.skipped("VllmFlowTests")
        self.assertEqual(len(skipped), ran)
