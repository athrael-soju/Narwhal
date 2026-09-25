"""Keep operator input and runtime failures within the installed CLI contract."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal import cli
from narwhal.deployment import launch_engine
from narwhal.diagnostics import check
from narwhal.engines import attestation
from narwhal.profiling import probe


class CliFailureTests(unittest.TestCase):
    def test_fleet_input_failures_identify_command_operation_and_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = [root / name for name in ("missing.json", "malformed.json", "array.json")]
            paths[1].write_text("{")
            paths[2].write_text("[]")
            for command, invoke in (
                ("narwhal-serve", cli.serve),
                ("narwhal-check", check.main),
                ("narwhal-profile", probe.main),
            ):
                for path in paths:
                    with (
                        self.subTest(command=command, path=path),
                        contextlib.redirect_stdout(io.StringIO()) as stdout,
                        contextlib.redirect_stderr(io.StringIO()) as stderr,
                    ):
                        args = ["--fleet", str(path)]
                        if command == "narwhal-serve":
                            args.extend(["--port", "0"])
                        self.assertEqual(invoke(args), 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(f"{command}: load fleet {path}:", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())

    def test_serve_validates_port_before_socket_operations(self):
        for port in (-1, 65536):
            with (
                self.subTest(port=port),
                patch.object(cli, "_port_in_use") as bind,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                self.assertRaises(SystemExit) as raised,
            ):
                cli.serve(["--fleet", "unused", "--port", str(port)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(str(port), stderr.getvalue())
            bind.assert_not_called()

    def test_http_failures_report_the_request_and_operation(self):
        request = httpx.Request("GET", "http://127.0.0.1:1/health")
        error = httpx.ConnectError("connection refused", request=request)
        for command, module in (("narwhal-check", check), ("narwhal-profile", probe)):
            with (
                tempfile.TemporaryDirectory() as folder,
                self.subTest(command=command),
                patch.object(
                    module.FleetConfig,
                    "load",
                    return_value=SimpleNamespace(profiles_path=Path(folder) / "profiles.json"),
                ),
                patch.object(module, "run", AsyncMock(side_effect=error)),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(module.main(["--fleet", "fixture.json"]), 1)
            self.assertIn(command, stderr.getvalue())
            self.assertIn("fixture.json", stderr.getvalue())
            self.assertIn(str(request.url), stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_attestation_file_and_http_failures_use_separate_categories(self):
        args = ["--document", "missing.json", "--engine-base", "http://127.0.0.1:1"]
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(attestation.main(args), 2)
        self.assertIn("load document missing.json", stderr.getvalue())
        with (
            patch.object(attestation.AttestationDocument, "load"),
            patch.object(
                attestation,
                "fetch_engine_identity",
                AsyncMock(side_effect=httpx.ConnectError("connection refused")),
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(attestation.main(args), 1)
        self.assertIn("attest engine http://127.0.0.1:1", stderr.getvalue())

    def test_engine_configuration_failures_keep_the_input_field_or_path(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "missing"
            for args, expected in (
                (["check", "--run", str(run)], str(run / "launch.json")),
                (["prepare", "--out", str(run)], "NARWHAL_ENGINE_LAUNCH_CONFIG"),
                (["start-shared", "--run", str(run), "--ready-seconds", "-1"], "-1"),
            ):
                with (
                    self.subTest(args=args),
                    patch.dict(os.environ, {}, clear=True),
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                    self.assertRaises(SystemExit) as raised,
                ):
                    launch_engine.main(args)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(expected, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_unexpected_programming_error_retains_its_traceback(self):
        with (
            patch.object(check.FleetConfig, "load"),
            patch.object(check, "run", AsyncMock(side_effect=AttributeError("implementation"))),
            self.assertRaisesRegex(AttributeError, "implementation"),
        ):
            check.main(["--fleet", "fixture.json"])
