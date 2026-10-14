"""Cache capture, live model dimensions and model formats are optional launcher capabilities."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import load
from narwhal.deployment.launch_engine import cli
from narwhal.deployment.launch_engine.backend import (
    CacheCapture,
    EngineLauncher,
    LiveModelDimensions,
    ModelFormats,
)
from narwhal.deployment.launch_engine.check import check
from tests.backends.contract import Stages, fixtures, prepared

CAPABILITIES = (CacheCapture, LiveModelDimensions, ModelFormats)


class LaunchCapabilityTests(unittest.TestCase):
    def prepared(self, name: str, *, native: bool = False, **environment: str):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return prepared(
            Path(folder.name), load(name).launcher(), fixtures(name), native=native, **environment
        )

    def test_the_interface_holds_no_optional_step(self):
        required = EngineLauncher.__abstractmethods__
        for capability in CAPABILITIES:
            self.assertFalse(required & capability.__abstractmethods__, capability.__name__)

    def test_sglang_declares_no_optional_capability(self):
        self.assertIsInstance(load("vllm").launcher(), CAPABILITIES)
        launcher = load("sglang").launcher()
        for capability in CAPABILITIES:
            self.assertNotIsInstance(launcher, capability)

    def test_the_cli_lists_capture_actions_for_backends_with_cache_capture(self):
        with contextlib.redirect_stdout(io.StringIO()) as shown, self.assertRaises(SystemExit):
            cli.main(["--help"])
        for action in cli.CAPTURE_ACTIONS:
            self.assertIn(action, shown.getvalue())
        self.assertIn("vLLM)", shown.getvalue())
        self.assertNotIn("SGLang", shown.getvalue())
        with (
            patch.object(cli, "capture_backends", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()) as shown,
            self.assertRaises(SystemExit),
        ):
            cli.main(["--help"])
        for action in cli.CAPTURE_ACTIONS:
            self.assertNotIn(action, shown.getvalue())

    def test_a_capture_action_names_the_backends_that_support_it(self):
        run, _, _ = self.prepared("sglang")
        with (
            contextlib.redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as exited,
        ):
            cli.main(["measure-cache", "--run", str(run)])
        self.assertEqual(exited.exception.code, 1)
        self.assertIn(
            "SGLang has no cache capture; measure-cache applies to vLLM", error.getvalue()
        )
        self.assertFalse((run / "cache-layout.json").exists())

    def test_a_backend_without_model_formats_takes_a_model_directory(self):
        with (
            tempfile.NamedTemporaryFile(suffix=".bin") as model,
            self.assertRaisesRegex(ValueError, "^SGLang launches take a model directory"),
        ):
            self.prepared("sglang", native=True, NARWHAL_MODEL_PATH=model.name)

    def test_model_dimensions_come_from_the_model_configuration_without_live_dimensions(self):
        run, plan, _ = self.prepared("sglang")
        with (
            contextlib.redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as exited,
        ):
            cli.main(["model-dimensions", "--run", str(run)])
        self.assertEqual(exited.exception.code, 1)
        self.assertIn("checked.json", error.getvalue())
        self.assertFalse((run / "model-dimensions.json").exists())
        stages = Stages(plan, fixtures("sglang").check_output(plan))
        with (
            patch("narwhal.deployment.stages.run", side_effect=stages),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            check(run, plan)
            self.assertEqual(cli.main(["model-dimensions", "--run", str(run)]), 0)
        record = json.loads((run / "model-dimensions.json").read_text())
        self.assertEqual(record["model_architecture"], "FixtureForCausalLM")


if __name__ == "__main__":
    unittest.main()
