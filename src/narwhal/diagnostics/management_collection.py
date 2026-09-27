"""Collect registered diagnostic sources and retain redacted immutable exports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from narwhal.contracts import DIAGNOSTIC_BUNDLE, ContractVersionError, validate_document
from narwhal.deployment.management_access import (
    AccessError,
    InspectionAccess,
    clean_command,
    directory,
    open_input,
    parse_object,
)
from narwhal.deployment.management_commands import run_command
from narwhal.deployment.management_registry import ManagementTarget

from .bundle import Redactor, _selected
from .management_artifacts import (
    MAX_EXPORT_BYTES,
    ArtifactError,
    ArtifactStore,
    _directory,
    _read_file,
    _write_file,
)

MAX_SOURCES = 128
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_SOURCE_FILE = re.compile(
    r"[0-9]{4}-(?:artifact|health|ready|state|lifecycle|metrics)\.(?:json|txt)"
)


@dataclass(frozen=True)
class DiagnosticCollection:
    """Return a CLI outcome and the separate immutable exports produced from it."""

    command_result: dict[str, Any]
    data: dict[str, Any]
    artifacts: list[dict[str, Any]]
    errors: list[dict[str, Any]]


@contextmanager
def _scratch(target: ManagementTarget) -> Iterator[tuple[int, Path]]:
    with directory(target.artifact_root, private=True, create=True) as root:
        name = f".mcp-collection-{uuid4()}"
        os.mkdir(name, mode=0o700, dir_fd=root)
        try:
            with _directory(root, name, create=False) as descriptor:
                yield descriptor, target.artifact_root / name
        finally:
            shutil.rmtree(name, dir_fd=root)


def _fleet_snapshot(path: Path) -> tuple[bytes | None, dict[str, Any]]:
    try:
        with open_input(path) as (_, contents):
            try:
                document = parse_object(contents)
            except AccessError:
                document = {}
            return contents, document
    except AccessError as error:
        if error.code not in {"input_missing", "source_truncated"}:
            raise
        # Collection reports missing or truncated inputs beside usable router evidence.
        return None, {}


def _manifest(descriptor: int) -> dict[str, Any]:
    payload = _read_file(descriptor, "manifest.json", MAX_MANIFEST_BYTES)
    try:
        document = parse_object(payload)
        validate_document(document, DIAGNOSTIC_BUNDLE)
    except ContractVersionError:
        raise
    except ValueError:
        raise AccessError("invalid_input", "Diagnostic manifest failed validation") from None
    rows = document.get("sources")
    if (
        document.get("status") not in {"success", "partial"}
        or not isinstance(rows, list)
        or len(rows) > MAX_SOURCES
        or any(not isinstance(row, dict) or not isinstance(row.get("status"), str) for row in rows)
    ):
        raise AccessError("invalid_input", "Diagnostic manifest has invalid source records")
    return document


def _check_dev_inputs(target: ManagementTarget, include_request_content: bool) -> None:
    """Reject unsafe instance paths before the collector contacts router endpoints."""
    if target.kind != "dev" or target.instance_dir is None:
        return
    instance = target.instance_dir
    try:
        with directory(instance):
            pass
    except AccessError as error:
        if error.code == "input_missing":
            return
        raise
    lifecycle: dict[str, Any] = {}
    for name in ("instance.json", "lifecycle.json", "fleet.json"):
        try:
            with open_input(
                instance / name, maximum=MAX_SOURCE_BYTES if name == "lifecycle.json" else 0
            ) as (_, data):
                if name == "lifecycle.json":
                    with suppress(AccessError):
                        lifecycle = parse_object(data)
        except AccessError as error:
            if error.code not in {"input_missing", "source_truncated"}:
                raise
    selected = lifecycle.get("run")
    if not selected:
        return
    if not isinstance(selected, str):
        raise AccessError("invalid_input", "Instance run path is invalid")
    run = Path(selected)
    if not run.is_absolute():
        run = target.working_directory / run
    if ".." in run.parts or not run.is_relative_to(instance):
        raise AccessError("permission_denied", "Instance run lies outside its registered directory")
    try:
        with directory(run):
            pass
    except AccessError as error:
        if error.code == "input_missing":
            return
        raise
    paths = [
        run / name for name in ("fleet.json", "profiles.json", "teardown.json", "router-state.json")
    ]
    paths.extend(_selected(run, include_request_content, MAX_SOURCES))
    for path in dict.fromkeys(paths):
        try:
            # The one-byte limit still checks the descriptor's type, owner and mode.
            with open_input(path, maximum=0):
                pass
        except AccessError as error:
            if error.code not in {"input_missing", "source_truncated"}:
                raise


def _retain_source(
    descriptor: int,
    source: dict[str, Any],
    store: ArtifactStore,
    redactor: Redactor,
) -> dict[str, Any] | None:
    filename = source.get("file")
    if filename is None:
        return None
    if not isinstance(filename, str) or not _SOURCE_FILE.fullmatch(filename):
        raise ArtifactError("permission_denied", "Diagnostic export filename is invalid")
    contents = _read_file(descriptor, filename, MAX_EXPORT_BYTES)
    if type(source.get("bytes")) is not int or source["bytes"] != len(contents):
        raise ArtifactError("artifact_changed", "Diagnostic source size changed before export")
    if source.get("sha256") != hashlib.sha256(contents).hexdigest():
        raise ArtifactError("artifact_changed", "Diagnostic source hash changed before export")
    exported = store.export(
        redactor.body(contents), kind="diagnostic_source", complete=source["status"] == "ok"
    )
    source.update(
        artifact_id=exported["artifact_id"], sha256=exported["sha256"], bytes=exported["size_bytes"]
    )
    return exported


async def collect_diagnostics(
    access: InspectionAccess,
    target_id: str,
    *,
    timeout_s: float = 30,
    include_request_content: bool = False,
) -> DiagnosticCollection:
    """Run bounded installed collection using only a registered target and its grants."""
    target = access.target(target_id)
    if type(include_request_content) is not bool:
        raise AccessError("invalid_input", "Request-content selection must be a boolean")
    redactor = access.redactor(target, include_request_content=include_request_content)
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
        raise AccessError(
            "invalid_input", "Collection timeout must be positive and at most 30 seconds"
        )
    deadline = monotonic() + timeout_s
    _check_dev_inputs(target, include_request_content)
    fleet_path = access.fleet_path(target)
    contents, document = _fleet_snapshot(fleet_path)
    redactor.references(document)
    router = access.router(target)
    environment = access.environment(target, document)
    for index, secret in enumerate(sorted(redactor.secrets)):
        environment[f"NARWHAL_MCP_SECRET_{index}"] = secret
    access.check_storage(target)
    store = ArtifactStore(str(access.registry.registry_id), target)
    with _scratch(target) as (scratch, scratch_path):
        selected_fleet = fleet_path
        if contents is not None:
            _write_file(scratch, "fleet.json", redactor.body(contents))
            selected_fleet = scratch_path / "fleet.json"
        remaining = deadline - monotonic()
        command_budget = remaining - min(2.0, remaining * 0.2)
        collection_budget = command_budget - min(1.0, max(0.1, command_budget * 0.2)) - 0.1
        if collection_budget <= 0:
            raise AccessError("stage_timeout", "Collection deadline expired before command start")
        arguments = [
            "diagnostics",
            "collect",
            "--format",
            "json",
            "--router",
            router,
            "--out",
            f"/proc/self/fd/{scratch}/bundle",
            "--fleet",
            str(selected_fleet),
            "--timeout",
            str(collection_budget),
            "--source-timeout",
            str(min(5.0, collection_budget)),
            "--max-source-bytes",
            str(MAX_SOURCE_BYTES),
            "--max-sources",
            str(MAX_SOURCES),
        ]
        if target.kind == "dev" and target.instance_dir is not None:
            arguments.extend(("--instance", str(target.instance_dir)))
        if include_request_content:
            arguments.append("--include-request-content")
        command = await run_command(
            arguments,
            cwd=target.working_directory,
            env=environment,
            timeout_s=command_budget,
            pass_fds=(scratch,),
        )
        command = clean_command(command, redactor)
        if command["status"] not in {"success", "degraded"}:
            return DiagnosticCollection(command, {}, [], [])
        artifacts = []
        errors = []
        with _directory(scratch, "bundle", create=False) as bundle:
            manifest = _manifest(bundle)
            # Recheck both metadata and bodies with the complete parent-side secret set.
            manifest = redactor.value(manifest)
            for row in manifest["sources"]:
                if row.get("source") == str(selected_fleet):
                    row["source"] = redactor.text(str(fleet_path))
                    if contents is not None:
                        row["capture"] = "redacted_registered_fleet_snapshot"
                        row["source_sha256"] = hashlib.sha256(contents).hexdigest()
                        row["source_bytes"] = len(contents)
                        row.pop("source_mtime_ns", None)
                if "file" not in row:
                    continue
                try:
                    if monotonic() >= deadline:
                        raise ArtifactError(
                            "source_unavailable", "Diagnostic export deadline expired"
                        )
                    reference = _retain_source(bundle, row, store, redactor)
                    if reference is not None:
                        artifacts.append(reference)
                except (ArtifactError, OSError) as failure:
                    code = (
                        failure.code if isinstance(failure, ArtifactError) else "artifact_missing"
                    )
                    message = (
                        failure.message
                        if isinstance(failure, ArtifactError)
                        else "Captured source is unavailable"
                    )
                    row["export_error"] = {"code": code, "message": message}
                    manifest["status"] = "partial"
                    errors.append(
                        {"code": code, "message": message, "context": {"source": row.get("source")}}
                    )
                await asyncio.sleep(0)
        manifest_reference = store.export(
            (json.dumps(manifest, ensure_ascii=False) + "\n").encode(),
            "diagnostic_manifest",
            complete=manifest["status"] == "success",
        )
        artifacts.append(manifest_reference)
        bundle_index = {
            "manifest_artifact_id": manifest_reference["artifact_id"],
            "collection_status": manifest["status"],
            "sources": [
                {
                    "source": row.get("source"),
                    "status": row["status"],
                    "artifact_id": row.get("artifact_id"),
                    "export_error": row.get("export_error"),
                }
                for row in manifest["sources"]
            ],
        }
        bundle_reference = store.export(
            (json.dumps(bundle_index, ensure_ascii=False) + "\n").encode(),
            "diagnostic_bundle",
            complete=manifest["status"] == "success",
        )
        artifacts.append(bundle_reference)
        return DiagnosticCollection(
            command,
            {
                "bundle_artifact_id": bundle_reference["artifact_id"],
                "manifest_artifact_id": manifest_reference["artifact_id"],
                "collection_status": manifest["status"],
                "source_count": len(manifest["sources"]),
            },
            artifacts,
            errors,
        )
