"""Keep public parser help and operator reference tables aligned."""

import argparse
import importlib
import io
import re
import tomllib
import unittest
from contextlib import redirect_stdout, suppress
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import launch_engine
from tests.fixtures import ROOT

REFERENCES = {
    "narwhal": "Dev.md",
    "narwhal-engine": "Engine.md",
    "narwhal-check": "Check.md",
    "narwhal-attest": "Attest.md",
    "narwhal-serve": "Serve.md",
    "narwhal-profile": "Profile.md",
}


class ParserCaptured(Exception):
    pass


def command_parser(entry_point):
    module, function = entry_point.split(":")
    entry = getattr(importlib.import_module(module), function)
    captured = []

    def capture(parser, *args, **kwargs):
        captured.append(parser)
        raise ParserCaptured

    with (
        patch.object(argparse.ArgumentParser, "parse_args", autospec=True, side_effect=capture),
        suppress(ParserCaptured),
    ):
        entry([])
    return captured[0]


def public_parsers(parser):
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                if not name.startswith("_"):
                    yield from public_parsers(child)


class CliReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        cls.scripts = project["scripts"]

    def test_public_options_have_help_and_reference_rows(self):
        self.assertEqual(set(self.scripts), set(REFERENCES))
        for command, entry in self.scripts.items():
            with self.subTest(command=command):
                parser = command_parser(entry)
                options = set()
                for public in public_parsers(parser):
                    for action in public._actions:
                        for option in action.option_strings:
                            if option in {"-h", "--help", "--version"}:
                                continue
                            self.assertTrue(action.help, f"{public.prog}: {option}")
                            self.assertNotEqual(action.help, argparse.SUPPRESS)
                            options.add(option)
                reference = (ROOT / "docs/cli" / REFERENCES[command]).read_text()
                if command == "narwhal":
                    reference += (ROOT / "docs/Config-Inspection.md").read_text()
                    reference += (ROOT / "docs/Diagnostic-Bundles.md").read_text()
                documented = set(re.findall(r"^\|\s*`(--[\w-]+)[ `]", reference, re.MULTILINE))
                self.assertEqual(options, documented - {"--version"}, command)
                if "--version" in parser._option_string_actions:
                    self.assertIn("--version", reference)

    def test_internal_engine_actions_are_hidden_and_still_dispatch(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
            launch_engine.main(["--help"])
        self.assertEqual(exit_status.exception.code, 0)
        for internal in ("_cache-probe", "_model-dimensions", "==SUPPRESS=="):
            self.assertNotIn(internal, output.getvalue())

        with patch.object(launch_engine, "runtime_cache_probe") as probe:
            with patch.object(launch_engine.os, "umask"):
                self.assertEqual(launch_engine.main(["_cache-probe", "--plan", "plan.json"]), 0)
            probe.assert_called_once_with(Path("plan.json"))
        output = io.StringIO()
        with (
            patch.object(
                launch_engine, "runtime_model_dimensions", return_value={"ranks": 1}
            ) as dimensions,
            patch.object(launch_engine.os, "umask"),
            redirect_stdout(output),
        ):
            self.assertEqual(launch_engine.main(["_model-dimensions", "--plan", "plan.json"]), 0)
        dimensions.assert_called_once_with(Path("plan.json"))
        self.assertEqual(output.getvalue(), 'NARWHAL_MODEL_DIMENSIONS={"ranks": 1}\n')

    def test_public_actions_explain_operation_and_backend_or_lifecycle_state(self):
        for command in ("narwhal", "narwhal-engine"):
            parser = command_parser(self.scripts[command])
            for public in public_parsers(parser):
                for action in public._actions:
                    if not isinstance(action, argparse._SubParsersAction):
                        continue
                    descriptions = {row.dest: row.help for row in action._choices_actions}
                    for name, child in action.choices.items():
                        if name.startswith("_"):
                            continue
                        with self.subTest(command=command, action=name):
                            self.assertTrue(descriptions.get(name))
                            self.assertTrue(child.description)
                            if command == "narwhal-engine":
                                self.assertRegex(child.description, r"container|native")
                            elif name in {"init", "up", "verify", "status", "down"}:
                                self.assertRegex(
                                    child.description,
                                    r"initialized|launched|ready|stopped",
                                )

    def test_profile_and_check_help_explain_mode_dependencies(self):
        profile = command_parser(self.scripts["narwhal-profile"])
        output = profile._option_string_actions["--out"].help
        self.assertIn("--refit-samples", output)
        self.assertIn("--merge", output)
        merge = profile._option_string_actions["--merge"].help
        for dependency in ("--out", "--refit-samples", "--only", "--overwrite"):
            self.assertIn(dependency, merge)
        self.assertIn("all five", profile._option_string_actions["--colocated"].help)
        check = command_parser(self.scripts["narwhal-check"])
        evidence = check._option_string_actions["--evidence-out"].help
        for dependency in ("engine_contract", "--ring", "--no-kv", "--verify-evidence"):
            self.assertIn(dependency, evidence)

    def test_readme_inventory_matches_installed_commands(self):
        readme = (ROOT / "README.md").read_text()
        inventory = re.search(r"The wheel installs (.+?)\.", readme).group(1)
        self.assertEqual(set(re.findall(r"`(narwhal[\w-]*)`", inventory)), set(self.scripts))
