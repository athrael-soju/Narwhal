"""Compare reference tables with the configuration, parsers and schema registry."""

import argparse
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
from tools.tests.fixtures import ROOT


class ParserCaptured(Exception):
    """Stop command execution before configuration or external I/O."""


def parser_actions(parser):
    """Include options belonging to fleet subcommands."""
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from parser_actions(child)


class DocumentationContractTests(unittest.TestCase):
    def test_configuration_literal_defaults(self):
        """Backticked JSON defaults agree with their owning configuration fields."""
        engine = EngineSpec("e0", "http://stub")
        cfg = FleetConfig(model="stub", engines=[engine], slo=SLO(1, 1))
        fleet = document(cfg)
        section = ""
        checked = 0
        for line in (ROOT / "docs/Configuration.md").read_text().splitlines():
            if line.startswith("## "):
                section = line
            match = re.match(r"\| `([^`]+)` \| `([^`]+)` \|", line)
            if match is None:
                continue
            field, raw = match.groups()
            try:
                expected = json.loads(raw)
            except ValueError:
                continue
            source = fleet
            if section == "## Engine contract":
                source = EngineContract().fields()
            elif section == "## Required fields" and field in asdict(engine):
                source = asdict(engine)
            with self.subTest(section=section, field=field):
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
        sections = dict(
            re.findall(
                r"## `(narwhal-[^`]+)`\n([\s\S]*?)(?=\n## |\Z)",
                (ROOT / "docs/CLI-Reference.md").read_text(),
            )
        )
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

    def test_reference_versions_cover_the_contract_registry(self):
        """Every persisted interface appears with its current schema version."""
        text = (ROOT / "docs/API-and-Data-Reference.md").read_text()
        rows = re.findall(r"^\|[^|]+\|\s*`(narwhal\.[^`]+)`\s*\|\s*(\d+)\s*\|", text, re.M)
        self.assertEqual(
            {schema: int(version) for schema, version in rows},
            {contract.schema: contract.current for contract in CONTRACTS.values()},
        )

    def test_reference_names_demand_history_and_decode_floor_metrics(self):
        """The metrics inventory includes every demand-history gauge and the floor target."""
        text = (ROOT / "docs/API-and-Data-Reference.md").read_text()

        for metric in (
            "narwhal_decode_floor",
            "narwhal_demand_history_cells",
            "narwhal_demand_history_cell_limit",
            "narwhal_demand_history_observations",
            "narwhal_demand_history_overflow_observations",
        ):
            with self.subTest(metric=metric):
                self.assertIn(f"`{metric}`", text)
