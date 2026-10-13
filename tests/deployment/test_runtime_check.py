"""One runtime check flow serves every backend; each backend supplies its probe."""

import argparse
import contextlib
import dataclasses
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from narwhal.backends import load, names
from narwhal.backends.sglang import runtime as sglang_runtime
from narwhal.deployment.launch_engine.check import check
from tests.backends.contract import Stages, fixtures, prepared

CUSTOM_CODE = json.dumps({"auto_map": {"AutoTokenizer": "custom.Tokenizer"}})


class RuntimeCheckFlowTests(unittest.TestCase):
    def checked(self, name: str, runtime: dict, *, native: bool = False) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        fixture = fixtures(name)
        fixture = dataclasses.replace(fixture, launch_runtime={**fixture.launch_runtime, **runtime})
        run, plan, _ = prepared(root, load(name).launcher(), fixture, native=native)
        (Path(plan["model_dir"]) / "tokenizer_config.json").write_text(CUSTOM_CODE)
        with (
            patch(
                "narwhal.deployment.stages.run",
                side_effect=Stages(plan, fixture.check_output(plan)),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            check(run, plan)
        return run

    def test_the_model_metadata_check_reads_the_tokenizer_setting_on_every_backend(self):
        for name in names():
            with self.subTest(backend=name):
                with self.assertRaisesRegex(ValueError, "^Model metadata requires --trust-remote"):
                    self.checked(name, {})
                # A separate tokenizer leaves the model directory's tokenizer files unused.
                run = self.checked(name, {"tokenizer": "/model/tokenizer"})
                self.assertTrue((run / "checked.json").exists())

    def test_a_native_check_requires_the_planned_model_path(self):
        for name in names():
            with self.subTest(backend=name):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                run, plan, _ = prepared(
                    Path(folder.name), load(name).launcher(), fixtures(name), native=True
                )
                plan["model_path"] = str(Path(folder.name) / "missing")
                with self.assertRaisesRegex(ValueError, "^native model path is missing"):
                    check(run, plan)


def sglang_modules() -> dict[str, ModuleType]:
    sglang = ModuleType("sglang")
    sglang.__version__ = "0.5.21"  # type: ignore[attr-defined]
    server_args = ModuleType("sglang.srt.server_args")

    class ServerArgs:
        @staticmethod
        def add_cli_args(parser: argparse.ArgumentParser) -> None:
            for option in (
                "--model-path",
                "--tokenizer-path",
                "--kv-events-config",
                "--disaggregation-bootstrap-port",
            ):
                parser.add_argument(option)
            for flag in ("--trust-remote-code", "--disable-radix-cache"):
                parser.add_argument(flag, action="store_true")

    server_args.ServerArgs = ServerArgs  # type: ignore[attr-defined]
    return {
        "sglang": sglang,
        "sglang.srt": ModuleType("sglang.srt"),
        "sglang.srt.server_args": server_args,
    }


class SglangProbeTests(unittest.TestCase):
    def test_the_probe_checks_a_separate_tokenizer_for_shipped_code(self):
        with tempfile.TemporaryDirectory() as folder:
            tokenizer = Path(folder)
            (tokenizer / "tokenizer_config.json").write_text(CUSTOM_CODE)
            args = ["--model-path", "/model", "--tokenizer-path", str(tokenizer)]
            with patch.dict(sys.modules, sglang_modules()):
                with self.assertRaisesRegex(ValueError, "^Tokenizer metadata requires"):
                    sglang_runtime.settings({}, args)
                trusted = sglang_runtime.settings({}, [*args, "--trust-remote-code"])
                self.assertEqual(trusted["sglang_version"], "0.5.21")
                (tokenizer / "tokenizer_config.json").write_text("{}")
                self.assertTrue(sglang_runtime.settings({}, args)["prefix_caching"])


if __name__ == "__main__":
    unittest.main()
