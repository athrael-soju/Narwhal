"""Load an operator-owned snapshot of registered management targets."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from narwhal import contracts

MAX_REGISTRY_BYTES = 1_048_576

Alias = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
EnvironmentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
TargetKind = Literal["dev", "fleet"]
Capability = Literal["inspect", "measure", "mutate"]
PlanAction = Literal[
    "dev_init",
    "dev_up",
    "dev_verify",
    "dev_down",
    "fleet_deploy",
    "fleet_profile",
    "fleet_preflight",
    "engine_replace",
    "monitoring_start",
    "deployment_cleanup",
]
Milliseconds = Annotated[int, Field(ge=1, le=86_400_000)]


def _absolute_path(value: Path) -> Path:
    if not value.is_absolute() or "\x00" in str(value):
        raise ValueError("registered paths must be literal absolute paths")
    return value


AbsolutePath = Annotated[Path, AfterValidator(_absolute_path)]


def _unique(values: Iterable[str]) -> None:
    entries = tuple(values)
    if len(entries) != len(set(entries)):
        raise ValueError("registry identifiers and grants must be distinct")


class RegistryModel(BaseModel):
    """Reject extra keys and coercion while keeping startup snapshots immutable."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, validate_default=True
    )


class ManagementAdapter(RegistryModel):
    """Select a supported adapter and its operator-maintained settings."""

    id: Literal["local-dev-v1", "ssh-v1"]
    settings_path: AbsolutePath | None


class ManagementEndpoints(RegistryModel):
    """Store endpoint references without accessing the environment at startup."""

    router_env: EnvironmentName | None = None
    prometheus_env: EnvironmentName | None = None
    grafana_env: EnvironmentName | None = None


class ManagementRecipe(RegistryModel):
    """Locate one registered input recipe without reading it."""

    id: Alias
    kind: TargetKind
    path: AbsolutePath


class ManagementQuery(RegistryModel):
    """Name a fixed PromQL expression and its supported query mode."""

    id: Alias
    expression: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    kind: Literal["instant", "range"]


class ManagementLog(RegistryModel):
    """Name a log file on an adapter-provided host alias."""

    id: Alias
    host_id: Alias
    source: AbsolutePath


class PreparationBudgets(RegistryModel):
    """Bound preparation helpers and their termination and reconciliation phases."""

    timeout_ms: Milliseconds = 300_000
    term_grace_ms: Milliseconds = 10_000
    kill_grace_ms: Milliseconds = 5000
    reconcile_ms: Milliseconds = 30_000


class ManagementTarget(RegistryModel):
    """Describe one target and the capabilities the operator grants it."""

    id: Alias
    kind: TargetKind
    working_directory: AbsolutePath
    artifact_root: AbsolutePath
    fleet_file: AbsolutePath | None
    instance_dir: AbsolutePath | None
    adapter: ManagementAdapter
    endpoints: ManagementEndpoints = Field(default_factory=ManagementEndpoints)
    credential_env: Annotated[tuple[EnvironmentName, ...], Field(max_length=64)] = ()
    capabilities: tuple[Capability, ...] = ("inspect",)
    actions: tuple[PlanAction, ...] = ()
    recipes: Annotated[tuple[ManagementRecipe, ...], Field(max_length=100)] = ()
    queries: Annotated[tuple[ManagementQuery, ...], Field(max_length=100)] = ()
    logs: Annotated[tuple[ManagementLog, ...], Field(max_length=100)] = ()
    freshness_s: Annotated[int, Field(ge=1, le=3600)] = 60
    allow_request_content: bool = False
    preparation: PreparationBudgets = Field(default_factory=PreparationBudgets)

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        """Reject ambiguous aliases and target-kind mismatches before dispatch."""
        _unique(self.credential_env)
        _unique(self.capabilities)
        _unique(self.actions)
        _unique(recipe.id for recipe in self.recipes)
        _unique(query.id for query in self.queries)
        _unique(log.id for log in self.logs)
        if any(recipe.kind != self.kind for recipe in self.recipes):
            raise ValueError("recipe kind must match its target")
        if any(action.startswith("dev_") != (self.kind == "dev") for action in self.actions):
            raise ValueError("registered actions must match the target kind")
        if self.kind == "dev":
            if self.instance_dir is None or self.adapter.id != "local-dev-v1":
                raise ValueError("dev targets require an instance directory and local dev adapter")
            if any(log.host_id != "local" for log in self.logs):
                raise ValueError("dev log sources require the local host alias")
        elif (
            self.fleet_file is None
            or self.instance_dir is not None
            or self.adapter.id != "ssh-v1"
            or self.adapter.settings_path is None
        ):
            raise ValueError("fleet targets require a fleet path, SSH settings and no dev instance")
        return self


class ManagementRegistry(RegistryModel):
    """Hold a fully validated registry snapshot without opening target inputs."""

    schema_name: Literal["narwhal.management-registry"] = Field(alias="schema")
    schema_version: Literal[1]
    registry_id: UUID
    state_dir: AbsolutePath
    targets: Annotated[tuple[ManagementTarget, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def validate_targets(self) -> Self:
        """Require each target alias to resolve to one registry entry."""
        _unique(target.id for target in self.targets)
        return self


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("management registry contains duplicate JSON keys")
        document[key] = value
    return document


def load_registry(path: Path) -> ManagementRegistry:
    """Read a bounded private registry without following its final symlink."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("management registry must be a regular file")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("management registry must be owned by the current user with mode 0600")
        if metadata.st_size > MAX_REGISTRY_BYTES:
            raise ValueError("management registry exceeds the size limit")
        payload = stream.read(MAX_REGISTRY_BYTES + 1)
    if len(payload) > MAX_REGISTRY_BYTES:
        raise ValueError("management registry exceeds the size limit")
    try:
        document = json.loads(payload, object_pairs_hook=_json_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("management registry must contain valid JSON") from None
    try:
        contracts.validate_document(document, contracts.MANAGEMENT_REGISTRY)
    except contracts.ContractVersionError:
        raise ValueError("unsupported management registry contract") from None
    return ManagementRegistry.model_validate_json(payload)
