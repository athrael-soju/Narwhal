"""Version persisted documents and machine-readable operator interfaces."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLEET = "fleet"
PROFILES = "profiles"
HANDOFF = "handoff"
LEASE = "lease"
LIFECYCLE = "lifecycle"
JOURNAL = "journal"
STATE = "state"
METRICS = "metrics"
ATTESTATION = "attestation"
CANARY_CASES = "canary_cases"
CANARY = "canary"
CLI = "cli"


@dataclass(frozen=True)
class Contract:
    """One interface's written and accepted versions."""

    schema: str
    current: int = 1


CONTRACTS: dict[str, Contract] = {
    FLEET: Contract("narwhal.fleet"),
    PROFILES: Contract("narwhal.profiles"),
    HANDOFF: Contract("narwhal.handoff"),
    LEASE: Contract("narwhal.router-lease"),
    LIFECYCLE: Contract("narwhal.lifecycle"),
    JOURNAL: Contract("narwhal.journal"),
    STATE: Contract("narwhal.state"),
    METRICS: Contract("narwhal.metrics"),
    ATTESTATION: Contract("narwhal.attestation"),
    CANARY_CASES: Contract("narwhal.canary-cases"),
    CANARY: Contract("narwhal.canary"),
    CLI: Contract("narwhal.contract-manifest"),
}


class ContractVersionError(ValueError):
    """A document violates the declared interface contract."""


def current(name: str) -> int:
    """Return the version new writers must emit for `name`."""
    return _contract(name).current


def versioned(name: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Prefix a document or row with its schema identity and current version."""
    overlap = {"schema", "schema_version"} & set(body)
    if overlap:
        fields = ", ".join(sorted(overlap))
        raise ValueError(f"{name} body cannot replace {fields}")
    spec = _contract(name)
    return {"schema": spec.schema, "schema_version": spec.current, **body}


def validate_document(document: Any, name: str) -> int:
    """Validate one document and return the version its reader observed."""
    if not isinstance(document, Mapping):
        raise ContractVersionError(f"{name} document must be an object")
    spec = _contract(name)
    schema = document.get("schema")
    if schema != spec.schema:
        raise ContractVersionError(f"{name} schema is {schema!r}, expected {spec.schema!r}")

    if "schema_version" not in document:
        raise ContractVersionError(f"{name} document has no schema_version")
    version = document["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise ContractVersionError(f"{name} schema_version must be a nonnegative integer")
    if version != spec.current:
        relation = "newer than" if version > spec.current else "not readable by"
        raise ContractVersionError(
            f"{name} schema version {version} is {relation} this build "
            f"(writes and reads {spec.current})"
        )
    return version


def validate_any_document(document: Any, names: tuple[str, ...]) -> tuple[str, int]:
    """Validate a row accepted under one of several related contracts."""
    if not names:
        raise ValueError("at least one contract name is required")
    if not isinstance(document, Mapping):
        raise ContractVersionError("document must be an object")
    schema = document.get("schema")
    for name in names:
        if _contract(name).schema == schema:
            return name, validate_document(document, name)
    expected = ", ".join(_contract(name).schema for name in names)
    raise ContractVersionError(f"schema is {schema!r}, expected one of {expected}")


def validate_jsonl_row(row: Any, *names: str) -> tuple[str, int]:
    """Validate a JSONL metadata or data row under the allowed contracts."""
    if not isinstance(row, Mapping):
        raise ContractVersionError("JSONL row must be an object")
    document = row.get("meta") if isinstance(row.get("meta"), Mapping) else row
    return validate_any_document(document, tuple(names))


def read_jsonl(path: Path, *names: str) -> list[dict[str, Any]]:
    """Read JSONL and reject malformed or incompatible rows with a line number."""
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            validate_jsonl_row(row, *names)
        except (json.JSONDecodeError, ContractVersionError) as exc:
            raise ContractVersionError(f"{path}:{line_number}: {exc}") from exc
        rows.append(dict(row))
    return rows


def read_json(path: Path, name: str) -> dict[str, Any]:
    """Read one JSON object and enforce its interface version."""
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ContractVersionError(f"{path}: malformed JSON: {exc}") from exc
    validate_document(document, name)
    return dict(document)


def manifest() -> dict[str, Any]:
    """Return the machine-readable contract registry exposed by the CLI."""
    rows = {
        name: {
            "schema": spec.schema,
            "write": spec.current,
            "read": [spec.current],
        }
        for name, spec in sorted(CONTRACTS.items())
    }
    return versioned(CLI, {"contracts": rows})


def _contract(name: str) -> Contract:
    try:
        return CONTRACTS[name]
    except KeyError as exc:
        raise ValueError(f"unknown interface contract {name!r}") from exc
