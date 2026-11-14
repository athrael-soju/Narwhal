import dataclasses
import unittest
from unittest.mock import patch

from narwhal.backends import load
from narwhal.deployment.launch_engine import cli
from narwhal.deployment.launch_engine.backend import launcher
from tests.backends import contract, test_contract

BARE = dataclasses.replace(load("vllm"), launcher=None)
# Tests that read the launcher, one from each contract class.
LAUNCH_TESTS = (
    ("VllmContractTests", "test_launcher_offline_surface"),
    ("VllmFlowTests", "test_launch_ports_must_be_distinct"),
    ("VllmCharacterizationTests", "test_launch_command_and_environment"),
)


class OptionalLauncherTests(unittest.TestCase):
    def run_contract(self, case: str, test: str) -> unittest.TestResult:
        result = unittest.TestResult()
        with patch.object(test_contract, "load", return_value=BARE):
            getattr(test_contract, case)(test).run(result)
        return result

    def test_launch_tools_refuse_a_backend_with_no_launcher(self):
        with (
            patch("narwhal.backends.load", return_value=BARE),
            self.assertRaisesRegex(ValueError, "backend 'vllm' has no engine launcher"),
        ):
            launcher("vllm")

    def test_capture_actions_and_launch_tests_leave_the_backend_out(self):
        with patch.object(cli, "load_backend", return_value=BARE):
            self.assertEqual(cli.capture_backends(), [])
        with patch.object(contract, "load_backend", return_value=BARE):
            self.assertEqual(contract.launched(), [])

    def test_the_contract_suite_skips_its_launch_tests(self):
        for case, test in LAUNCH_TESTS:
            with self.subTest(case=case, test=test):
                result = self.run_contract(case, test)
                self.assertEqual((result.failures, result.errors), ([], []))
                self.assertEqual(len(result.skipped), 1)

    def test_the_contract_suite_keeps_its_other_tests(self):
        for case, test in (
            ("VllmContractTests", "test_descriptor_composes_its_parts"),
            ("VllmCharacterizationTests", "test_handoff_request_bodies_and_order"),
        ):
            with self.subTest(case=case, test=test):
                result = self.run_contract(case, test)
                self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                self.assertEqual(result.skipped, [])
