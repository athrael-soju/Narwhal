"""Every NarwhalFleet the documentation shows holds only fields the CRD declares.

A field the schema drops lingers in an example until someone applies it, and the API server
then refuses it. This reads each YAML block of the documentation that holds a NarwhalFleet, a
narwhal chart values file or a fragment of `spec.engines`, and checks it against the schema the
chart and the API server check: every key declared, every required field present, every value
of its type. A value that holds a `<placeholder>` is the reader's to fill, so only its key is
checked.
"""

from __future__ import annotations

import json
import re
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from narwhal.deployment import fleet_resource
from tests.deployment import chart_schema
from tools.maintenance import crd_schema

ROOT = Path(__file__).resolve().parents[2]
BLOCK = re.compile(r"^(?P<indent>[ \t]*)```ya?ml[ \t]*\n(?P<body>.*?)^(?P=indent)```", re.M | re.S)
PLACEHOLDER = re.compile(r"<[^<>\n]+>")
MANIFEST_KEYS = {"apiVersion", "kind", "metadata", "spec"}
# What the documented install commands add to a values file with --set-file.
SET_FILE = {"monitoring": {"grafana": {"dashboard": "{}"}}}


def pages() -> list[Path]:
    found = sorted((ROOT / "docs").rglob("*.md"))
    found += [ROOT / "README.md", *sorted((ROOT / "deploy" / "helm").glob("*/README.md"))]
    return found


def blocks(page: Path) -> Iterator[tuple[int, Any]]:
    """Each YAML document of each YAML block of `page`, with the block's line."""
    text = page.read_text()
    for match in BLOCK.finditer(text):
        indent = len(match["indent"])
        body = "".join(line[indent:] + "\n" for line in match["body"].splitlines())
        line = text.count("\n", 0, match.start()) + 1
        try:
            documents = list(yaml.safe_load_all(body))
        except yaml.YAMLError:
            continue
        for document in documents:
            yield line, document


def _at(document: Any, path: str) -> Any:
    value = document
    for part in [p for p in path.split("/") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def problems(document: Any, schema: dict) -> list[str]:
    """Each way `document` breaks `schema`, leaving out the values the reader fills in."""
    found = []
    for error in chart_schema.errors(document, schema):
        path, _, reason = error.partition(": ")
        value = _at(document, path)
        # An undeclared or missing field is wrong whatever the reader fills in.
        declared = reason != "not allowed" and not reason.endswith(" is required")
        if declared and isinstance(value, str) and PLACEHOLDER.search(value):
            continue
        found.append(error)
    return found


def merged(defaults: Any, values: Any) -> Any:
    """`values` over the chart's `defaults`, as Helm merges a values file."""
    if isinstance(defaults, dict) and isinstance(values, dict):
        return {
            key: merged(defaults[key], values[key])
            if key in defaults and key in values
            else values.get(key, defaults.get(key))
            for key in {**defaults, **values}
        }
    return values


class DocFleetExampleTests(unittest.TestCase):
    """The documentation's fleets, chart values and engine fragments against the schema."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = crd_schema.values_schema()
        cls.values = json.loads(crd_schema.chart_values_schema())
        cls.defaults = yaml.safe_load((ROOT / "deploy/helm/narwhal/values.yaml").read_text())
        engines = cls.spec["properties"]["engines"]
        # A fragment of spec.engines names some of its fields, not all.
        cls.engines = {key: value for key, value in engines.items() if key != "required"}
        cls.found: list[tuple[str, str, Any, dict]] = []
        for page in pages():
            where = page.relative_to(ROOT).as_posix()
            for line, document in blocks(page):
                if not isinstance(document, dict):
                    continue
                name = f"{where}:{line}"
                if document.get("kind") == fleet_resource.KIND:
                    cls.found.append((name, "manifest", document, cls.spec))
                elif "fleet" in document and set(document) <= set(cls.values["properties"]):
                    cls.found.append((name, "values", document, cls.values))
                elif document and set(document) <= set(engines["properties"]):
                    cls.found.append((name, "engines", document, cls.engines))

    def test_the_documentation_shows_a_fleet_manifest_and_a_values_file(self):
        kinds = {kind for _, kind, _, _ in self.found}
        self.assertLessEqual({"manifest", "values", "engines"}, kinds)

    def test_every_fleet_example_holds_only_declared_fields_of_their_types(self):
        for name, kind, document, schema in self.found:
            with self.subTest(example=name):
                if kind == "manifest":
                    self.assertLessEqual(set(document), MANIFEST_KEYS, "top-level keys")
                    self.assertEqual(
                        document.get("apiVersion"),
                        f"{fleet_resource.GROUP}/{fleet_resource.VERSION}",
                    )
                    self.assertEqual(problems(document.get("spec"), schema), [])
                elif kind == "values":
                    self.assertEqual(
                        problems(merged(merged(self.defaults, document), SET_FILE), schema), []
                    )
                else:
                    self.assertEqual(problems(document, schema), [])


if __name__ == "__main__":
    unittest.main()
