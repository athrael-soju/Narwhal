"""Write the field tables of docs/deploy/NarwhalFleet.md from the `NarwhalFleet` schema.

Run it after changing `SCHEMA` in `src/narwhal/deployment/fleet_resource.py`. With `--check`
it changes nothing and exits 1 when the page differs from the schema; the unit suite runs that
check.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from narwhal.deployment import fleet_resource

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/deploy/NarwhalFleet.md"
SOURCE = "SCHEMA in src/narwhal/deployment/fleet_resource.py"
BEGIN = (
    f"<!-- Generated from {SOURCE} by tools/maintenance/fleet_reference_doc.py. "
    "Edit the schema, then run the tool. -->"
)
END = "<!-- End of the generated field tables. -->"
# An object whose properties are the stage names is written once, as `<stage>`.
STAGE_KEY = "<stage>"


def _code(value: Any) -> str:
    if isinstance(value, bool):
        return f"`{'true' if value else 'false'}`"
    return f"`{value}`"


def _type(node: Mapping[str, Any]) -> str:
    kind = node.get("type", "object")
    if kind == "array":
        return f"list of {_type(node['items'])}s"
    if kind == "object" and "additionalProperties" in node:
        return f"map of {_type(node['additionalProperties'])}s"
    return str(kind)


def _limits(node: Mapping[str, Any]) -> list[str]:
    """The bounds, choices, default and CEL rules of one field, in words."""
    found = []
    if "enum" in node:
        found.append("One of " + ", ".join(_code(item) for item in node["enum"]) + ".")
    if "minimum" in node and "maximum" in node:
        found.append(f"From {node['minimum']} to {node['maximum']}.")
    elif "minimum" in node:
        found.append(f"At least {node['minimum']}.")
    if "maxLength" in node and "enum" not in node:
        least = node.get("minLength", 0)
        if least:
            found.append(f"{least} to {node['maxLength']} characters.")
        else:
            found.append(f"At most {node['maxLength']} characters.")
    if "minItems" in node:
        found.append(f"{node['minItems']} to {node['maxItems']} items.")
    elif "maxItems" in node:
        found.append(f"At most {node['maxItems']} items.")
    if node.get("x-kubernetes-list-type") == "map":
        keys = ", ".join(_code(key) for key in node["x-kubernetes-list-map-keys"])
        found.append(f"Each item's {keys} is unique.")
    if "maxProperties" in node:
        found.append(f"At most {node['maxProperties']} entries.")
    if "default" in node:
        found.append(f"Default {_code(node['default'])}.")
    found.extend(
        f"API server rule: {rule['message']}." for rule in node.get("x-kubernetes-validations", [])
    )
    return found


def _properties(node: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = node.get("properties") or {}
    if set(properties) == set(fleet_resource.STAGES):
        # Every stage has the same field; one row stands for them all.
        return {STAGE_KEY: properties[fleet_resource.STAGES[0]]}
    return properties


def rows(node: Mapping[str, Any], path: str) -> Iterator[tuple[str, Mapping[str, Any], bool]]:
    """Each field below `node`, depth first, as (path, schema, required)."""
    required = set(node.get("required") or [])
    for name, child in _properties(node).items():
        field = f"{path}.{name}"
        yield field, child, name in required
        if child.get("type") == "array" and child["items"].get("type") == "object":
            yield from rows(child["items"], field + "[]")
        elif child.get("type") == "object":
            yield from rows(child, field)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def table(node: Mapping[str, Any], path: str, *, required_column: bool = True) -> str:
    head = (
        "| Field | Type | Required | Description |"
        if required_column
        else "| Field | Type | Description |"
    )
    lines = [head, "| --- | --- | --- | --- |" if required_column else "| --- | --- | --- |"]
    for field, child, required in rows(node, path):
        description = child.get("description", "").rstrip(".")
        if field.endswith("." + STAGE_KEY):
            description = description.replace(f" {fleet_resource.STAGES[0]} ", " ")
            description = f"{description}, for each stage by name" if description else ""
        text = " ".join([f"{description}." if description else "", *_limits(child)]).strip()
        cells = [f"`{field}`", _type(child)]
        if required_column:
            cells.append("Yes" if required else "")
        cells.append(_cell(text))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _rules(node: Mapping[str, Any]) -> str:
    return "\n".join(f"- {rule['message']}." for rule in node.get("x-kubernetes-validations", []))


def tables() -> str:
    """The generated part of the page: the resource's rules, then the spec and status fields."""
    schema = fleet_resource.SCHEMA
    spec = schema["properties"]["spec"]
    status = schema["properties"]["status"]
    columns = "\n".join(
        f"| {column['name']} | `{column['jsonPath']}` |"
        for column in fleet_resource.PRINTER_COLUMNS
    )
    return "\n\n".join(
        [
            f"`apiVersion: {fleet_resource.API_VERSION}`, `kind: {fleet_resource.KIND}`, "
            f"scope `Namespaced`, plural `{fleet_resource.PLURAL}`, category `narwhal`. "
            "The API server refuses a fleet that breaks these rules:",
            _rules(schema) + "\n" + _rules(spec),
            "### Spec",
            "A field marked Yes is required whenever the field that holds it is set.",
            table(spec, "spec"),
            "### Status",
            "The controller writes the status; nothing else may.",
            table(status, "status", required_column=False),
            "`kubectl get narwhalfleet` prints these columns:",
            "| Column | From |\n| --- | --- |\n" + columns,
        ]
    )


def render(text: str) -> str:
    """`text` with its generated block replaced by the schema's tables."""
    if text.count(BEGIN) != 1:
        raise ValueError(f"{PAGE} needs one block that begins {BEGIN}")
    head, rest = text.split(BEGIN)
    if END not in rest:
        raise ValueError(f"{PAGE}: the generated block has no end marker")
    _, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n\n{tables()}\n\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the page differs, and change nothing"
    )
    args = parser.parse_args(argv)
    text = PAGE.read_text()
    rendered = render(text)
    if args.check:
        if rendered != text:
            print(f"{PAGE.relative_to(ROOT)} differs from {SOURCE}; run {__file__}")
            return 1
        return 0
    if rendered != text:
        PAGE.write_text(rendered)
        print(f"wrote {PAGE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
