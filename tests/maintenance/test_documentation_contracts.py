"""Compare reference tables with the configuration, parsers and schema registry."""

import argparse
import ast
import importlib
import json
import re
import tomllib
import unittest
from dataclasses import asdict
from unittest.mock import patch

from narwhal.config import SLO, EngineContract, EngineSpec, FleetConfig
from narwhal.config.serialization import document
from narwhal.contracts import CONTRACTS
from narwhal.dev.template import default_template, reference
from narwhal.serving.outcomes import ERROR_RESPONSES
from tests.fixtures import ROOT


class ParserCaptured(Exception):
    """Stop command execution before configuration or external I/O."""


def parser_actions(parser):
    """Include options belonging to fleet subcommands."""
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from parser_actions(child)


def error_table_rows(text):
    """Yield the HTTP and error `type` cells of each row in tables that carry both columns."""
    columns = None
    for line in text.splitlines():
        if not line.startswith("|"):
            columns = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if columns is None:
            if "HTTP" in cells and "Error `type`" in cells:
                columns = cells.index("HTTP"), cells.index("Error `type`")
            continue
        if set(line) <= set("|-: "):
            continue
        yield cells[columns[0]], cells[columns[1]]


class DocumentationContractTests(unittest.TestCase):
    def test_configuration_literal_defaults(self):
        """Backticked JSON defaults agree with their owning configuration fields.

        The settings guide states the same defaults, so it is checked with the reference.
        """
        engine = asdict(EngineSpec("e0", "http://stub"))
        contract = EngineContract().fields()
        cfg = FleetConfig(model="stub", engines=[EngineSpec("e0", "http://stub")], slo=SLO(1, 1))
        fleet = document(cfg)
        section = ""
        checked = 0
        pages = [
            *sorted((ROOT / "docs/configuration").glob("*.md")),
            ROOT / "docs/operate/07-Admission-Queue-and-Retry-Settings.md",
        ]
        for page in pages:
            for line in page.read_text().splitlines():
                if line.startswith("## "):
                    section = line
                match = re.match(r"\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", line)
                if match is None:
                    continue
                field, raw = match.groups()
                try:
                    expected = json.loads(raw)
                except ValueError:
                    continue
                source = fleet
                if section == "## 2. Minimal fleet definition" and field in engine:
                    source = engine
                elif (
                    section == "## 3. Engine shape and compatibility contract" and field in contract
                ):
                    source = contract
                with self.subTest(page=page.name, section=section, field=field):
                    actual = source
                    for part in field.split("."):
                        actual = actual[part]
                    actual = getattr(actual, "value", actual)
                    self.assertEqual(actual, expected)
                checked += 1
        self.assertGreaterEqual(checked, 85)

    def test_cli_tables_name_registered_options_and_literal_defaults(self):
        """Each command table maps to its shipped parser, including subcommands."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        sections = {}
        for page in (ROOT / "docs/cli").glob("*.md"):
            content = page.read_text()
            # A page may title a script's subcommand, such as `narwhal dev`.
            heading = re.search(r"^# `(narwhal(?:-[^` ]+)?)(?: [a-z]+)?`\n", content, re.M)
            self.assertIsNotNone(heading, page)
            sections[heading.group(1)] = content
        self.assertEqual(set(sections), set(project["scripts"]))
        for name, entry in project["scripts"].items():
            captured = []

            def capture(parser, *args, captured=captured, **kwargs):
                captured.append(parser)
                raise ParserCaptured

            module, function = entry.split(":")
            command = getattr(importlib.import_module(module), function)
            with (
                patch.object(argparse.ArgumentParser, "parse_args", capture),
                self.assertRaises(ParserCaptured),
            ):
                command([])
            options = {
                option: action
                for action in parser_actions(captured[0])
                for option in action.option_strings
            }
            rows = re.findall(
                r"^\| `(--[^ `]+)[^`]*` \| ([^|]+)\|",
                sections[name],
                re.M,
            )
            self.assertTrue(rows, name)
            for option, default in rows:
                with self.subTest(command=name, option=option):
                    self.assertIn(option, options)
                    try:
                        expected = json.loads(default.strip().strip("`"))
                    except ValueError:
                        continue
                    actual = options[option].default
                    # None defers resolution to FleetConfig or the runtime constructor.
                    if actual is not None:
                        self.assertEqual(actual, expected)

    def test_cuda_runtime_install_pins_each_dev_template(self):
        """The documented CUDA install provides the runtime each dev template requires."""
        text = (ROOT / "docs/dev/CUDA-Runtime.md").read_text()
        packages = dict(re.findall(r"'([a-z0-9-]+)==([^']+)'", text))
        packages["vllm-gguf-plugin"] = re.search(r"/vllm_gguf_plugin-([^-]+)-cp310", text)[1]
        revision = re.search(r"checkout ([0-9a-f]{40})", text)[1]
        commands = re.sub(r" \\\n\s*", " ", text)
        for template in (reference(), default_template()):
            model = template["model"]
            with self.subTest(template=template["name"]):
                self.assertEqual(packages, template["runtime"]["expected_packages"])
                self.assertEqual(revision, template["runtime"]["gguf_plugin_source_revision"])
                self.assertIn(
                    f"hf download {model['repository']} --revision {model['revision']} "
                    f"{model['filename']} ",
                    commands,
                )
                self.assertIn(
                    f"hf download {model['tokenizer_repository']} "
                    f"--revision {model['tokenizer_revision']} ",
                    commands,
                )

    def test_reference_versions_cover_the_contract_registry(self):
        """Every versioned interface appears with its current schema version."""
        text = (ROOT / "docs/telemetry/05-Compatibility.md").read_text()
        rows = re.findall(r"^\|[^|]+\|\s*`(narwhal\.[^`]+)`\s*\|\s*(\d+)\s*\|", text, re.M)
        self.assertEqual(
            {schema: int(version) for schema, version in rows},
            {contract.schema: contract.current for contract in CONTRACTS.values()},
        )

    def test_http_error_tables_list_every_status_and_error_type(self):
        """The HTTP API error tables list exactly the registered status and error type pairs."""
        listed = set()
        for page in sorted((ROOT / "docs/http-api").glob("*.md")):
            for status, error_type in error_table_rows(page.read_text()):
                with self.subTest(page=page.name, status=status, error_type=error_type):
                    # One row names one status and one error type.
                    status_match = re.fullmatch(r"`(\d{3})`", status)
                    type_match = re.fullmatch(r"`([a-z_]+)`", error_type)
                    self.assertIsNotNone(status_match)
                    self.assertIsNotNone(type_match)
                    listed.add((int(status_match[1]), type_match[1]))
        self.assertEqual(listed, ERROR_RESPONSES)

    def test_serving_error_bodies_use_the_registered_builder(self):
        """Serving code builds every JSON error body through `error_response`."""
        literal = []
        for path in sorted((ROOT / "src/narwhal/serving").rglob("*.py")):
            if path.name == "outcomes.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "JSONResponse":
                    literal += [
                        f"{path.name}:{node.lineno}"
                        for inner in ast.walk(node)
                        if isinstance(inner, ast.Dict)
                        and any(
                            isinstance(key, ast.Constant) and key.value == "error"
                            for key in inner.keys
                        )
                    ]
        self.assertEqual(literal, [])
