"""Check a value against a JSON schema, as helm and the API server do.

The tests check many fleets against the CRD's schema, too many to send each to a server. This
checks the keywords the schema uses and refuses one it does not know, so a schema change cannot
pass here unchecked.
"""

from __future__ import annotations

import re
from typing import Any

_ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples"}
_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class SchemaError(ValueError):
    pass


def errors(value: Any, node: Any, path: str = "") -> list[str]:
    """Each place where `value` breaks `node`, as `path: reason`."""
    if node is True:
        return []
    if node is False:
        return [f"{path or '/'}: not allowed"]
    found: list[str] = []
    where = path or "/"
    for keyword, rule in node.items():
        if keyword in _ANNOTATIONS or keyword in ("then", "else"):
            continue
        if keyword == "type":
            kinds = rule if isinstance(rule, list) else [rule]
            if not any(_is(value, kind) for kind in kinds):
                found.append(f"{where}: not of type {rule}")
        elif keyword == "enum":
            if value not in rule:
                found.append(f"{where}: {value!r} is not one of {rule}")
        elif keyword == "const":
            if value != rule:
                found.append(f"{where}: {value!r} is not {rule!r}")
        elif keyword == "pattern":
            if isinstance(value, str) and not re.search(rule, value):
                found.append(f"{where}: {value!r} does not match {rule}")
        elif keyword == "minimum":
            if _is(value, "number") and value < rule:
                found.append(f"{where}: {value} is below {rule}")
        elif keyword == "maximum":
            if _is(value, "number") and value > rule:
                found.append(f"{where}: {value} is above {rule}")
        elif keyword in (
            "minItems",
            "maxItems",
            "minProperties",
            "maxProperties",
            "minLength",
            "maxLength",
        ):
            if isinstance(value, (list, dict, str)) and _is(value, _counted(keyword)):
                size = len(value)
                if (keyword.startswith("min") and size < rule) or (
                    keyword.startswith("max") and size > rule
                ):
                    found.append(f"{where}: {keyword} {rule}, has {size}")
        elif keyword == "items":
            if isinstance(value, list):
                for index, item in enumerate(value):
                    found += errors(item, rule, f"{path}/{index}")
        elif keyword == "required":
            if isinstance(value, dict):
                found += [f"{where}: {name} is required" for name in rule if name not in value]
        elif keyword == "properties":
            if isinstance(value, dict):
                for name, child in rule.items():
                    if name in value:
                        found += errors(value[name], child, f"{path}/{name}")
        elif keyword == "additionalProperties":
            if isinstance(value, dict):
                known = set(node.get("properties", {}))
                for name in value:
                    if name not in known:
                        found += errors(value[name], rule, f"{path}/{name}")
        elif keyword == "propertyNames":
            if isinstance(value, dict):
                for name in value:
                    found += errors(name, rule, f"{path}/{name}(name)")
        elif keyword == "allOf":
            for part in rule:
                found += errors(value, part, path)
        elif keyword == "anyOf":
            if all(errors(value, part, path) for part in rule):
                found.append(f"{where}: matches none of anyOf")
        elif keyword == "oneOf":
            if sum(not errors(value, part, path) for part in rule) != 1:
                found.append(f"{where}: does not match exactly one of oneOf")
        elif keyword == "not":
            if not errors(value, rule, path):
                found.append(f"{where}: matches a schema it must not")
        elif keyword == "if":
            branch = node.get("else" if errors(value, rule, path) else "then")
            if branch is not None:
                found += errors(value, branch, path)
        else:
            raise SchemaError(f"{where}: the schema uses {keyword}, which this check does not know")
    return found


def _is(value: Any, kind: str) -> bool:
    if kind in ("number", "integer"):
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        return numeric and (kind == "number" or float(value).is_integer())
    return isinstance(value, _TYPES[kind]) and not (kind != "boolean" and isinstance(value, bool))


def _counted(keyword: str) -> str:
    return {"Items": "array", "Properties": "object", "Length": "string"}[
        keyword.removeprefix("min").removeprefix("max")
    ]
