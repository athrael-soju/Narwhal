"""Installed command discovery and environment-specific version reporting."""

import contextlib
import importlib
import io
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from importlib import metadata
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]


class CLIIdentityTests(unittest.TestCase):
    def test_copied_engine_launcher_runs_with_only_the_standard_library(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "launch_engine.py"
            launcher.write_bytes((ROOT / "src/narwhal/deployment/launch_engine.py").read_bytes())
            for arguments in (["--help"], ["_cache-probe", "--help"]):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", str(launcher), *arguments],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)
                    self.assertEqual(result.stderr, "")

    def test_root_help_lists_every_installed_command_with_its_purpose(self):
        from narwhal.dev.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        inventory = output.getvalue().split("Installed commands:\n", 1)[1]
        descriptions = dict(line.strip().split(maxsplit=1) for line in inventory.splitlines())
        self.assertEqual(set(descriptions), set(COMMANDS))
        for command, purpose in descriptions.items():
            with self.subTest(command=command):
                self.assertGreater(len(purpose.split()), 3)

    def test_all_commands_report_distribution_version_before_runtime_work(self):
        for command, entry in COMMANDS.items():
            module, function = entry.split(":")
            main = getattr(importlib.import_module(module), function)
            for available in (True, False):
                with self.subTest(command=command, metadata_available=available):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        mock.patch(
                            "narwhal.cli_support.metadata.version",
                            return_value="7.8.9+operator",
                            side_effect=None if available else metadata.PackageNotFoundError,
                        ) as version,
                        mock.patch(
                            "narwhal.config.FleetConfig.load",
                            side_effect=AssertionError("version read a fleet"),
                        ),
                        mock.patch(
                            "socket.socket", side_effect=AssertionError("version opened a socket")
                        ),
                        mock.patch(
                            "subprocess.Popen",
                            side_effect=AssertionError("version started a subprocess"),
                        ),
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                        self.assertRaises(SystemExit) as stopped,
                    ):
                        main(["--version"])
                    self.assertEqual(stopped.exception.code, 0)
                    value = (
                        "7.8.9+operator"
                        if available
                        else "unknown (distribution metadata unavailable)"
                    )
                    self.assertEqual(stdout.getvalue(), f"narwhal-inference {value}\n")
                    self.assertEqual(stderr.getvalue(), "")
                    version.assert_called_once_with("narwhal-inference")

    def test_help_and_version_import_each_entry_point_on_cpu(self):
        script = """
import importlib
import importlib.abc
import sys

class BlockGPU(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'vllm', 'transformers', 'nixl', 'cupy'}:
            raise AssertionError(f'CLI imported GPU runtime: {fullname}')

sys.meta_path.insert(0, BlockGPU())
module, function = sys.argv[1].split(':')
main = getattr(importlib.import_module(module), function)
for flag in ('--help', '--version'):
    try:
        main([flag])
    except SystemExit as stopped:
        assert stopped.code == 0, (flag, stopped.code)
    else:
        raise AssertionError(f'{flag} entered the command body')
"""
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        for command, entry in COMMANDS.items():
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-c", script, entry],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--version", result.stdout)
                self.assertTrue(
                    result.stdout.rstrip().splitlines()[-1].startswith("narwhal-inference ")
                )
                self.assertEqual(result.stderr, "")
