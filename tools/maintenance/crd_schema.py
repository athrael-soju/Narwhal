"""Write the NarwhalFleet CRD and the narwhal chart's generated files from `fleet_resource`.

Run it after changing `src/narwhal/deployment/fleet_resource.py`, or the gate kinds or model
volume name in `kubernetes_objects.py`. With `--check` it changes nothing and exits 1 when a
chart file differs from what it renders; the unit suite runs that check.

`group_template()` renders the `narwhal.group` template and `fleet_template()` the names and
patterns the narwhal chart's admission policy shares with `fleet_resource` and
`kubernetes_objects`. `values_schema()` renders the JSON schema of a chart's `fleet` value,
which the narwhal chart's `values.schema.json` holds.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from narwhal.deployment import fleet_resource, kubernetes_objects
from narwhal.deployment.launch_engine.kubernetes import MODEL_VOLUME_SOURCES

ROOT = Path(__file__).resolve().parents[2]
CRD_CHART = ROOT / "deploy/helm/narwhal-crds"
CHART = ROOT / "deploy/helm/narwhal"
SOURCE = "src/narwhal/deployment/fleet_resource.py"
HEADER = (
    f"{{{{- /* Generated from {SOURCE} by tools/maintenance/crd_schema.py. "
    "Edit the schema, then run the tool. */}}"
)
# The chart's labels; Helm fills them in, so they stay out of `fleet_resource.crd()`.
LABELS = (
    "app.kubernetes.io/name: {{ .Chart.Name }}",
    "app.kubernetes.io/part-of: narwhal",
    "app.kubernetes.io/managed-by: {{ .Release.Service }}",
    "app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}",
    'helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}',
)
_PLAIN_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
# Plain keys YAML 1.1 reads as booleans or null.
_RESERVED = {"y", "n", "yes", "no", "on", "off", "true", "false", "null"}


def _key(key: str) -> str:
    if _PLAIN_KEY.fullmatch(key) and key.lower() not in _RESERVED:
        return key
    return json.dumps(key)


def _scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a YAML double-quoted scalar.
        return json.dumps(value)
    if value == {}:
        return "{}"
    if value == []:
        return "[]"
    raise TypeError(f"no YAML form for {value!r}")


def yaml_lines(value: Any, indent: int = 0) -> list[str]:
    """Block-style YAML for a document of mappings, lists, strings, integers and booleans."""
    pad = " " * indent
    lines: list[str] = []
    if isinstance(value, dict) and value:
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{_key(key)}:")
                lines.extend(yaml_lines(item, indent + 2))
            else:
                lines.append(f"{pad}{_key(key)}: {_scalar(item)}")
    elif isinstance(value, list) and value:
        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = yaml_lines(item, indent + 2)
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(pad + _scalar(value))
    return lines


def crd_template() -> str:
    """The CRD chart's template: `fleet_resource.crd()` with the chart's labels."""
    document = fleet_resource.crd()
    metadata = yaml_lines(document["metadata"], 2)
    lines = [
        HEADER,
        *yaml_lines({key: document[key] for key in ("apiVersion", "kind")}),
        "metadata:",
        metadata[0],
        "  labels:",
        *(f"    {label}" for label in LABELS),
        *metadata[1:],
        "spec:",
        *yaml_lines(document["spec"], 2),
    ]
    return "\n".join(lines) + "\n"


def group_template() -> str:
    """The `narwhal.group` template, which gives a chart `fleet_resource.GROUP`."""
    return (
        f"{HEADER}\n"
        "{{/* The API group of the NarwhalFleet resource, and of every label and annotation "
        "derived from it. */}}\n"
        f'{{{{- define "narwhal.group" -}}}}{fleet_resource.GROUP}{{{{- end -}}}}\n'
    )


def _define(name: str, value: str) -> str:
    return f'{{{{- define "{name}" -}}}}{value}{{{{- end -}}}}'


def fleet_template() -> str:
    """The `narwhal.fleet.*` templates: the fleet's API version, kind and name, the CRD and
    controller Lease names, the gate label, and the model volume rules of
    `fleet_resource.check_model_volume`, for the chart's fleet, RBAC and admission policy.

    Each pattern is a CEL raw string, so it must hold no single quote."""
    patterns = {
        "narwhal.fleet.hostPathRefused": fleet_resource.HOST_PATH_REFUSED,
        "narwhal.fleet.normalisedPath": fleet_resource._NORMALISED,
        "narwhal.fleet.dotSegment": fleet_resource._DOT_SEGMENT,
        "narwhal.fleet.digest": fleet_resource._DIGEST,
    }
    for name, pattern in patterns.items():
        if "'" in pattern or "{{" in pattern or "}}" in pattern:
            raise ValueError(f"{name} cannot be written into a CEL raw string")
    values = {
        "narwhal.fleet.apiVersion": fleet_resource.API_VERSION,
        "narwhal.fleet.kind": fleet_resource.KIND,
        "narwhal.fleet.name": fleet_resource.NAME,
        "narwhal.fleet.plural": fleet_resource.PLURAL,
        "narwhal.fleet.crdName": fleet_resource.CRD_NAME,
        "narwhal.fleet.controllerLease": fleet_resource.CONTROLLER_LEASE,
        "narwhal.fleet.gateLabel": fleet_resource.GATE_LABEL,
        "narwhal.fleet.modelVolume": kubernetes_objects.MODEL_VOLUME,
        # JSON lists, which are CEL list literals too.
        "narwhal.fleet.modelVolumeSources": json.dumps(list(MODEL_VOLUME_SOURCES)),
        "narwhal.fleet.hostNetworkGates": json.dumps(list(kubernetes_objects.HOST_NETWORK_KINDS)),
        **patterns,
    }
    lines = [
        HEADER,
        "{{/* The fleet resource's names, and the rules the admission policy shares with the "
        "controller. */}}",
        *(_define(name, value) for name, value in values.items()),
    ]
    return "\n".join(lines) + "\n"


def _json_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {
        key: _json_schema(value)
        for key, value in node.items()
        if not key.startswith("x-kubernetes-") and key != "format"
    }
    if "properties" in converted:
        converted["properties"] = {
            name: _json_schema(child) for name, child in node["properties"].items()
        }
        converted["additionalProperties"] = False
    return converted


def values_schema() -> dict:
    """The JSON schema of a chart's `fleet` value: a fleet's `spec`, which Helm checks before
    it renders. Helm cannot run CEL, so the API server checks those rules when it stores the
    fleet, and every object refuses fields the schema does not name, as `kubectl --validate`
    does."""
    return dict(_json_schema(copy.deepcopy(fleet_resource.SCHEMA["properties"]["spec"])))


def chart_values_schema() -> str:
    """The narwhal chart's `values.schema.json`, with its `fleet` value from `values_schema()`
    and every other value as the file holds it."""
    document = json.loads((CHART / "values.schema.json").read_text())
    document["properties"]["fleet"] = {
        **values_schema(),
        "description": "The NarwhalFleet the chart installs, as its spec; leave it out to "
        "apply the fleet another way",
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


FILES: dict[Path, Callable[[], str]] = {
    CRD_CHART / "templates" / f"{fleet_resource.PLURAL}.yaml": crd_template,
    CHART / "templates" / "_group.tpl": group_template,
    CHART / "templates" / "_fleet.tpl": fleet_template,
    CHART / "values.schema.json": chart_values_schema,
}


def stale() -> list[Path]:
    """The generated files that differ from what the schema renders."""
    return [
        path for path, render in FILES.items() if not path.is_file() or path.read_text() != render()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when a file differs, and change nothing"
    )
    args = parser.parse_args(argv)
    paths = stale()
    if args.check:
        for path in paths:
            print(f"{path.relative_to(ROOT)} differs from {SOURCE}", file=sys.stderr)
        return 1 if paths else 0
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FILES[path]())
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
