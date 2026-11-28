"""Write the Prometheus Operator schemas `make chart-check` checks the chart's monitors against.

`python tools/maintenance/operator_schemas.py <directory>` downloads the CRDs of the pinned
Prometheus Operator release, checks them against the SHA-256 pinned here, and writes each
CRD version's schema as kubeconform reads it, to
`<directory>/<group>/<kind>_<version>.json`. A download already in the directory is kept only
while its SHA-256 equals the pinned one.

The schemas refuse a field the CRD does not name, as the API server prunes or refuses it.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

VERSION = "v0.95.2"
URL = (
    "https://github.com/prometheus-operator/prometheus-operator/releases/download/"
    f"{VERSION}/stripped-down-crds.yaml"
)
SHA256 = "ab6e3f856ea951a34c16911eb4ea42e898046843159164d72cfd3880b358670a"
BUNDLE = f"prometheus-operator-{VERSION}-crds.yaml"


class Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # type: ignore[misc]
    """YAML 1.1 reads a bare `=`, an enum value of the CRDs, as its value tag; it is a string."""


Loader.add_constructor("tag:yaml.org,2002:value", yaml.SafeLoader.construct_yaml_str)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strict(schema: Any) -> Any:
    """`schema` with every object that lists its properties closed to others, except where the
    CRD keeps unknown fields."""
    if isinstance(schema, list):
        return [strict(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    found = {key: strict(value) for key, value in schema.items()}
    if (
        "properties" in found
        and "additionalProperties" not in found
        and not found.get("x-kubernetes-preserve-unknown-fields")
    ):
        found["additionalProperties"] = False
    return found


def schemas(documents: Iterable[Any]) -> dict[str, dict]:
    """Each served version's schema of the CRDs among `documents`, by its path under the
    schema directory."""
    found = {}
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "CustomResourceDefinition":
            continue
        spec = document["spec"]
        kind = spec["names"]["kind"].lower()
        for version in spec["versions"]:
            if not version.get("served", True):
                continue
            schema = copy.deepcopy(version["schema"]["openAPIV3Schema"])
            found[f"{spec['group']}/{kind}_{version['name']}.json"] = strict(schema)
    return found


def bundle(directory: Path) -> bytes:
    """The pinned CRDs, from the directory or downloaded."""
    path = directory / BUNDLE
    if path.is_file() and not path.is_symlink():
        data = path.read_bytes()
        if sha256(data) == SHA256:
            return data
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read()
    observed = sha256(data)
    if observed != SHA256:
        raise SystemExit(f"{URL} has SHA-256 {observed}, not the pinned one")
    write(path, data)
    return data


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        pending.write(data)
    os.replace(pending.name, path)


def install(directory: Path) -> list[Path]:
    """Write the pinned release's schemas under `directory`, and return their paths."""
    documents = yaml.load_all(bundle(directory), Loader=Loader)
    written = []
    for name, schema in sorted(schemas(documents).items()):
        path = directory / name
        write(path, (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode())
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory for the schemas")
    args = parser.parse_args()
    written = install(args.directory)
    print(f"wrote {len(written)} Prometheus Operator {VERSION} schemas in {args.directory}")


if __name__ == "__main__":
    main()
