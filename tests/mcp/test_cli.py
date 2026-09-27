"""Check startup selection and failure before a transport is opened."""

import contextlib
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.mcp.cli import main


@unittest.skipUnless(importlib.util.find_spec("mcp"), "requires the optional mcp extra")
class StartupTests(unittest.TestCase):
    def test_explicit_registry_takes_precedence_and_environment_is_fallback(self):
        for argv, selected in (
            (["--registry", "explicit.json"], "explicit.json"),
            ([], "environment.json"),
        ):
            with (
                self.subTest(argv=argv),
                patch("narwhal.mcp.cli.logging.basicConfig"),
                patch.dict(os.environ, {"NARWHAL_MANAGEMENT_REGISTRY": "environment.json"}),
                patch("narwhal.deployment.management_registry.load_registry") as load,
                patch(
                    "narwhal.deployment.management_coordinator.OperationCoordinator"
                ) as coordinator,
                patch("narwhal.mcp.inspection.inspection_adapters", return_value=()) as adapters,
                patch("narwhal.mcp.operations.operation_adapters", return_value=()) as operations,
                patch(
                    "narwhal.mcp.observability.observability_adapters", return_value=()
                ) as observe,
                patch("narwhal.mcp.server.serve", new_callable=AsyncMock) as serve,
            ):
                self.assertEqual(main(argv), 0)
                load.assert_called_once_with(Path(selected))
                adapters.assert_called_once_with(load.return_value)
                coordinator.assert_called_once_with(load.return_value, registry_path=Path(selected))
                coordinator.return_value.reconcile_startup.assert_called_once_with()
                operations.assert_called_once_with(load.return_value, coordinator.return_value)
                observe.assert_called_once_with(load.return_value)
                serve.assert_awaited_once_with(())

    def test_missing_selection_and_invalid_registry_never_open_transport(self):
        for missing in (True, False):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(missing=missing),
                patch("narwhal.mcp.cli.logging.basicConfig"),
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "narwhal.deployment.management_registry.load_registry",
                    side_effect=OSError("private site path and token"),
                ),
                patch("narwhal.mcp.server.serve", new_callable=AsyncMock) as serve,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(main([] if missing else ["--registry", "secret.json"]), 2)
                serve.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("registry", stderr.getvalue())
            self.assertNotIn("secret", stderr.getvalue())
            self.assertNotIn("private site", stderr.getvalue())
