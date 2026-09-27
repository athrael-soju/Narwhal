"""Validate immutable plans and retain their inputs in private durable storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from narwhal import contracts

from .management_access import directory
from .management_records import OperationError, canonical, encode_record, utc_now
from .management_registry import Alias, ManagementRegistry, PlanAction

MAX_PLAN_BYTES = 1_048_576
MAX_BLOB_BYTES = 8 * 1024 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(ge=1, le=86_400_000)]
InputName = Literal[
    "fleet_config",
    "launch_config",
    "host_inventory",
    "model_identity",
    "runtime_identity",
    "network",
    "service_policy",
    "measurement_recipe",
    "load_recipe",
    "credential_refs",
    "resource_ownership",
    "cleanup_selection",
]
ResourceKind = Literal[
    "engine",
    "attestation",
    "router",
    "monitoring",
    "tunnel",
    "measurement_helper",
    "installation",
    "private_artifact",
]


def identifier(value: str) -> str:
    """Accept canonical UUID strings before constructing a storage name."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise OperationError("invalid_input", "Identifier must be a UUID")
    return str(UUID(value))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Document(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)


class Exact(Document):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Cleanup(Exact):
    policy: Literal["temporary_only", "owned_stage_resources"]
    term_grace_ms: Positive
    kill_grace_ms: Positive
    reconcile_ms: Positive


class Stage(Exact):
    stage_id: Alias
    gate: Literal["A", "B", "C", "D", "E", "F", "G"] | None
    operation: Alias
    depends_on: list[Alias]
    subjects: Annotated[list[Alias], Field(min_length=1, max_length=256)]
    input_names: list[InputName]
    timeout_ms: Positive
    cleanup: Cleanup
    retain_on_success: list[ResourceKind]


class Input(Document):
    name: InputName
    sha256: Digest


class AdapterBinding(Document):
    id: Literal["local-dev-v1", "ssh-v1"]
    version: Annotated[str, Field(min_length=1, max_length=128)]
    assets_sha256: Digest


class Source(Document):
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    distribution_version: Annotated[str, Field(min_length=1, max_length=128)]
    wheel_sha256: Digest | None
    bundle_sha256: Digest | None

    @model_validator(mode="after")
    def artifact(self) -> Self:
        """Require at least one immutable source artifact."""
        if self.wheel_sha256 is None and self.bundle_sha256 is None:
            raise ValueError("Source requires an artifact digest")
        return self


class Recipe(Document):
    recipe_id: Alias
    sha256: Digest


class Binding(Document):
    registration_digest: Digest
    adapter: AdapterBinding
    source: Source
    recipe: Recipe | None
    snapshot_id: str
    snapshot_sha256: Digest
    identity_sha256: Digest
    inputs: Annotated[list[Input], Field(max_length=12)]


class Payload(Exact):
    action: PlanAction
    target_id: Alias
    binding: Binding
    parameters: dict[str, Any]
    stages: Annotated[list[Stage], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def dependencies(self) -> Self:
        """Require ordered stages, available inputs and every deployment gate."""
        names = [entry.name for entry in self.binding.inputs]
        if names != sorted(set(names)):
            raise ValueError("Input names must be unique and sorted")
        earlier: set[str] = set()
        for stage in self.stages:
            if stage.stage_id in earlier or not set(stage.depends_on) <= earlier:
                raise ValueError("Stage dependencies must name earlier stages")
            if not set(stage.input_names) <= set(names):
                raise ValueError("Stage input is missing")
            for values in (
                stage.depends_on,
                stage.subjects,
                stage.input_names,
                stage.retain_on_success,
            ):
                if len(values) != len(set(values)):
                    raise ValueError("Stage selectors must be distinct")
            earlier.add(stage.stage_id)
        if self.action == "fleet_deploy":
            gates = [stage.gate for stage in self.stages]
            if set(gates) != set("ABCDEFG") or gates != sorted(gate for gate in gates if gate):
                raise ValueError("Fleet deployment requires ordered Gates A through G")
        elif any(stage.gate is not None for stage in self.stages):
            raise ValueError("Standalone stages have no deployment gate")
        return self


class Plan(Document):
    schema_name: Literal["narwhal.deployment-plan"] = Field(alias="schema")
    schema_version: Literal[1]
    plan_id: str
    plan_digest: Digest
    created_at: str
    payload: Payload


class Snapshot(Document):
    """Require the identities and observations retained by preparation."""

    schema_name: Literal["narwhal.management-snapshot"] = Field(alias="schema")
    schema_version: Literal[1]
    snapshot_id: str
    observed_at: str
    identity: dict[str, Any]
    observations: dict[str, Any]


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Check a plan's version, required fields and canonical payload digest."""
    contracts.validate_document(plan, contracts.DEPLOYMENT_PLAN)
    try:
        Plan.model_validate(plan)
        identifier(plan["plan_id"])
        identifier(plan["payload"]["binding"]["snapshot_id"])
        if (
            not plan["created_at"].endswith("Z")
            or datetime.fromisoformat(plan["created_at"]).tzinfo != UTC
        ):
            raise ValueError("Plan timestamp must be UTC")
        encoded = canonical(plan)
        if len(encoded) > MAX_PLAN_BYTES:
            raise ValueError("Plan exceeds storage limit")
        if digest(canonical(plan["payload"])) != plan["plan_digest"]:
            raise ValueError("Plan payload digest differs")
    except (ValidationError, ValueError, TypeError, KeyError):
        raise OperationError("invalid_input", "Plan fields or digest are invalid") from None
    return plan


def _private(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise OperationError("permission_denied", "Plan storage must be private regular files")


def _read(parent: int, name: str, maximum: int) -> bytes:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
        )
    except FileNotFoundError:
        raise OperationError("plan_evidence_missing", "Retained plan evidence is missing") from None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        _private(before)
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (
            len(data) > maximum
            or len(data) != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise OperationError("plan_evidence_changed", "Plan evidence changed or is oversized")
        return data


def _write(parent: int, name: str, data: bytes) -> None:
    """Publish a complete fsynced file without overwriting an existing identity."""
    temporary = f".pending-{uuid4()}"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError:
            if _read(parent, name, MAX_BLOB_BYTES) != data:
                raise OperationError(
                    "plan_evidence_changed", "Stored plan identity differs"
                ) from None
    finally:
        os.unlink(temporary, dir_fd=parent)
    os.fsync(parent)


class PlanStore:
    """Keep plans, snapshots and content-addressed inputs under one target."""

    def __init__(self, registry: ManagementRegistry, target_id: str) -> None:
        if not any(target.id == target_id for target in registry.targets):
            raise OperationError("target_not_found", "Target is not registered")
        self.root = registry.state_dir
        self.target_id = target_id
        self.parts = (".management-plans", str(registry.registry_id), target_id)

    @contextmanager
    def _scope(self, create: bool) -> Iterator[int]:
        with directory(self.root, private=True, create=create) as root:
            descriptors = [root]
            try:
                for name in self.parts:
                    if create:
                        with suppress(FileExistsError):
                            os.mkdir(name, mode=0o700, dir_fd=descriptors[-1])
                            os.fsync(descriptors[-1])
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=descriptors[-1],
                    )
                    descriptors.append(fd)
                    meta = os.fstat(fd)
                    if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
                        raise OperationError(
                            "permission_denied", "Plan directories require mode 0700"
                        )
                yield descriptors[-1]
            except FileNotFoundError:
                raise OperationError("plan_not_found", "Plan storage is missing") from None
            finally:
                for fd in reversed(descriptors[1:]):
                    os.close(fd)

    def put_blob(self, data: bytes) -> str:
        """Commit bounded input bytes once under their content digest."""
        if not isinstance(data, bytes) or len(data) > MAX_BLOB_BYTES:
            raise OperationError("invalid_input", "Plan input exceeds its byte limit")
        identity = digest(data)
        with self._scope(True) as fd:
            _write(fd, identity + ".blob", data)
        return identity

    def blob(self, identity: str) -> bytes:
        """Read input bytes and verify their content digest."""
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise OperationError("invalid_input", "Input digest is invalid")
        with self._scope(False) as fd:
            data = _read(fd, identity + ".blob", MAX_BLOB_BYTES)
        if digest(data) != identity:
            raise OperationError(
                "plan_evidence_changed", "Input digest differs from retained bytes"
            )
        return data

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Verify evidence before publishing the immutable plan."""
        plan = contracts.versioned(
            contracts.DEPLOYMENT_PLAN,
            {
                "plan_id": str(uuid4()),
                "plan_digest": digest(canonical(payload)),
                "created_at": utc_now(),
                "payload": payload,
            },
        )
        validate_plan(plan)
        if plan["payload"]["target_id"] != self.target_id:
            raise OperationError("invalid_input", "Plan belongs to another target")
        self.verify_evidence(plan)
        with self._scope(True) as fd:
            _write(fd, plan["plan_id"] + ".json", canonical(plan))
        return plan

    def read(self, plan_id: str) -> dict[str, Any]:
        """Read and verify one target-scoped plan and every retained input."""
        name = identifier(plan_id) + ".json"
        with self._scope(False) as fd:
            data = _read(fd, name, MAX_PLAN_BYTES)
        try:
            document = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise OperationError("invalid_input", "Stored plan is malformed") from None
        validate_plan(document)
        if (
            document["plan_id"] != identifier(plan_id)
            or document["payload"]["target_id"] != self.target_id
        ):
            raise OperationError("invalid_input", "Stored plan identifier differs")
        self.verify_evidence(document)
        return document

    def verify_evidence(self, plan: dict[str, Any]) -> None:
        """Require matching snapshot identity and input bytes."""
        binding = plan["payload"]["binding"]
        raw = self.blob(binding["snapshot_sha256"])
        try:
            snapshot = json.loads(raw)
            contracts.validate_document(snapshot, contracts.MANAGEMENT_SNAPSHOT)
            Snapshot.model_validate(snapshot)
            encode_record(snapshot)
            if (
                not {"snapshot_id", "observed_at", "identity", "observations"} <= set(snapshot)
                or snapshot["snapshot_id"] != binding["snapshot_id"]
                or not isinstance(snapshot["identity"], dict)
                or not isinstance(snapshot["observations"], dict)
                or not snapshot["observed_at"].endswith("Z")
                or datetime.fromisoformat(snapshot["observed_at"]).tzinfo != UTC
                or digest(canonical(snapshot["identity"])) != binding["identity_sha256"]
            ):
                raise ValueError("Snapshot identity differs")
        except (ValueError, KeyError, TypeError):
            raise OperationError(
                "plan_evidence_changed", "Snapshot identity cannot be verified"
            ) from None
        for entry in binding["inputs"]:
            self.blob(entry["sha256"])
