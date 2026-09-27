"""Validate registered SSH deployment inputs and the explicitly selected source assets."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from narwhal import provenance

from .management_access import AccessError, directory, read_input
from .management_records import OperationError
from .management_registry import ManagementTarget

Milliseconds = Annotated[int, Field(ge=1, le=86_400_000)]
Positive = Annotated[int, Field(ge=1, le=1_000_000)]
Fraction = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
ASSETS = (
    "tools/deployment/discover_deployment.py",
    "tools/deployment/checkpoint_manifest.py",
    "tools/deployment/deploy_hosts.py",
    "tools/deployment/host_access.py",
    "tools/deployment/prepare_host_env.py",
    "tools/deployment/engine_launch.py",
    "tools/deployment/launch_engine.py",
    "tools/deployment/cache_capture_hook.py",
    "tools/deployment/attestation_contract.py",
    "tools/deployment/fabric_budget.py",
    "tools/measurement/load_trial.py",
    "tools/measurement/benchmark_runner.py",
    "tools/measurement/benchmark_evidence.py",
    "tools/observability/start.py",
    "tools/observability/artifacts.py",
    "tools/observability/make_targets.py",
    "tools/observability/compose.yml",
    "tools/observability/prometheus.yml",
    "tools/observability/prometheus-alerts.yml",
    "tools/observability/grafana-narwhal.json",
    "tools/observability/grafana/provisioning/dashboards/narwhal.yml",
    "tools/observability/grafana/provisioning/datasources/prometheus.yml",
    "pyproject.toml",
    "constraints-dev.txt",
    "Makefile",
)


class Exact(BaseModel):
    """Keep operator input strict and exclude input values from validation errors."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


def absolute(value: str) -> str:
    """Reject shell expansion and parent traversal in registered paths."""
    if (
        not Path(value).is_absolute()
        or ".." in Path(value).parts
        or any(c in value for c in "\0\r\n")
    ):
        raise ValueError("A literal absolute path is required")
    return value


class StageBudget(Exact):
    timeout_ms: Milliseconds = 300_000
    term_grace_ms: Milliseconds = 10_000
    kill_grace_ms: Milliseconds = 5000
    reconcile_ms: Milliseconds = 30_000


class InstallationBudget(StageBudget):
    timeout_ms: Milliseconds = 3_600_000


class MeasurementBudget(StageBudget):
    timeout_ms: Milliseconds = 3_600_000


class Budgets(Exact):
    discovery: StageBudget = Field(default_factory=StageBudget)
    installation: InstallationBudget = Field(default_factory=InstallationBudget)
    engines: InstallationBudget = Field(default_factory=InstallationBudget)
    fabric: MeasurementBudget = Field(default_factory=MeasurementBudget)
    attestation: StageBudget = Field(default_factory=StageBudget)
    profiling: MeasurementBudget = Field(default_factory=MeasurementBudget)
    preflight: StageBudget = Field(default_factory=StageBudget)
    serving: StageBudget = Field(default_factory=StageBudget)
    workload: MeasurementBudget = Field(default_factory=MeasurementBudget)
    cleanup: StageBudget = Field(default_factory=StageBudget)


class SSHSettings(Exact):
    schema_name: Literal["narwhal.ssh-settings"] = Field(alias="schema")
    schema_version: Literal[1]
    source_root: str
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    hosts_path: str
    launch_path: str
    known_hosts_path: str
    remote_root: str
    supervision: Literal["process"] = "process"
    connect_timeout_s: Annotated[int, Field(ge=1, le=60)] = 10
    command_timeout_s: Annotated[int, Field(ge=1, le=86_400)] = 300
    budgets: Budgets = Field(default_factory=Budgets)

    _paths = field_validator(
        "source_root", "hosts_path", "launch_path", "known_hosts_path", "remote_root"
    )(absolute)


class ProfilingRecipe(Exact):
    prefill_lens: Annotated[list[Positive], Field(min_length=3, max_length=64)] = Field(
        default_factory=lambda: [256, 512, 1024, 2048, 4096, 8192, 12288, 16384]
    )
    decode_concurrency: Annotated[list[Positive], Field(min_length=2, max_length=64)] = Field(
        default_factory=lambda: [1, 4, 16, 48]
    )
    decode_input_lens: Annotated[list[Positive], Field(min_length=2, max_length=64)] = Field(
        default_factory=lambda: [512, 4096, 8192]
    )
    decode_tokens: Annotated[int, Field(ge=3, le=1_000_000)] = 64
    decode_repeats: Annotated[int, Field(ge=1, le=100)] = 1
    prefill_repeats: Annotated[int, Field(ge=3, le=100)] = 3

    @field_validator("prefill_lens", "decode_concurrency", "decode_input_lens")
    @classmethod
    def distinct(cls, value: list[int]) -> list[int]:
        """Reject duplicate measurement points and colliding tunnel ports."""
        if len(value) != len(set(value)):
            raise ValueError("Measurement points must be distinct")
        return value


class FabricRecipe(Exact):
    transport: Literal["ucx_tcp", "ucx_rdma"] = "ucx_tcp"
    duration_s: Annotated[int, Field(ge=1, le=3600)] = 10
    test_port: Annotated[int, Field(ge=1024, le=65535)] = 5201
    repeats: Annotated[int, Field(ge=1, le=100)] = 1
    prompt_tokens: Positive = 8192
    transfer_budget_ms: Milliseconds = 1000
    utilization: Fraction = 0.7
    gid_index: Annotated[int, Field(ge=0, le=255)] = 0


class LoadRecipe(Exact):
    rates: Annotated[
        list[Annotated[float, Field(gt=0, le=100_000, allow_inf_nan=False)]],
        Field(min_length=2, max_length=16),
    ] = Field(default_factory=lambda: [0.5, 1.0])
    requests: Annotated[int, Field(ge=1, le=1_000_000)] = 200
    input_tokens: Positive = 8192
    output_tokens: Positive = 64
    seed: Annotated[int, Field(ge=0, le=2**32 - 1)] = 1729
    ttft_ms: Milliseconds = 2000
    tpot_us: Annotated[int, Field(ge=1, le=86_400_000_000)] = 33_300
    attainment: Fraction = 0.95
    request_timeout_ms: Milliseconds = 120_000
    max_inflight: Annotated[int, Field(ge=1, le=4096)] = 64
    max_lag_ms: Milliseconds = 50
    local_router_port: Annotated[int, Field(ge=1024, le=65535)] = 18000
    local_prometheus_port: Annotated[int, Field(ge=1024, le=65535)] = 19090
    local_grafana_port: Annotated[int, Field(ge=1024, le=65535)] = 13000

    @model_validator(mode="after")
    def distinct(self) -> Self:
        """Reject duplicate measurement points and colliding tunnel ports."""
        if len(self.rates) != len(set(self.rates)):
            raise ValueError("Load rate points must be distinct")
        ports = [self.local_router_port, self.local_prometheus_port, self.local_grafana_port]
        if len(set(ports)) != 3:
            raise ValueError("Local tunnel ports must be distinct")
        return self


class SSHRecipe(Exact):
    schema_name: Literal["narwhal.ssh-recipe"] = Field(alias="schema")
    schema_version: Literal[1]
    environment: Annotated[dict[EnvironmentName, str], Field(max_length=256)] = Field(
        default_factory=dict
    )
    environment_refs: Annotated[dict[EnvironmentName, EnvironmentName], Field(max_length=64)] = (
        Field(default_factory=dict)
    )
    profiling: ProfilingRecipe = Field(default_factory=ProfilingRecipe)
    fabric: FabricRecipe = Field(default_factory=FabricRecipe)
    load: LoadRecipe = Field(default_factory=LoadRecipe)

    @field_validator("environment")
    @classmethod
    def nonsecret(cls, values: dict[str, str]) -> dict[str, str]:
        """Allow only the fixed nonsecret deployment input fields."""
        fields = {
            "ENGINE_IMAGE",
            "ENGINE_MODEL_NAME",
            "MODEL_DIR",
            "RUN_DIR",
            "MODEL_CONFIG_SHA256",
            "FABRIC_INTERFACE",
            "ENGINE_PORT",
            "ATTEST_PORT",
            "NIXL_SIDE_CHANNEL_PORT",
            "UCX_TCP_PORT_RANGE",
            "URL",
            "ATTESTATION_URL",
            "IP",
            "ROUTER_URL",
            "GRAFANA_BIND_ADDRESS",
            "PROMETHEUS_LISTEN_ADDRESS",
        }
        for name, value in values.items():
            field = re.sub(r"^NARWHAL_(?:NODE_[1-9][0-9]*_)?", "", name)
            if (
                not name.startswith("NARWHAL_")
                or field not in fields
                or len(value) > 4096
                or any(c in value for c in "\0\r\n")
            ):
                raise ValueError("Only fixed nonsecret deployment fields are accepted")
        return values

    @model_validator(mode="after")
    def references(self) -> Self:
        """Reject overlapping and process-control environment references."""
        if set(self.environment) & set(self.environment_refs):
            raise ValueError("Environment fields cannot be both literal and referenced")
        reserved = {
            "PATH",
            "HOME",
            "ENV",
            "BASH_ENV",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "SHELLOPTS",
            "BASHOPTS",
            "IFS",
            "CDPATH",
            "GIT_CONFIG",
            "GIT_SSH_COMMAND",
        }
        if any(
            name in reserved
            or name.startswith(
                (
                    "NARWHAL_MANAGEMENT_",
                    "NARWHAL_REMOTE_",
                    "LD_",
                    "DYLD_",
                    "PYTHON",
                    "DOCKER_",
                    "GIT_",
                )
            )
            or "SSH" in name
            for name in {*self.environment_refs, *self.environment_refs.values()}
        ):
            raise ValueError("Reserved execution environment fields cannot be referenced")
        return self


class SSHHost(Exact):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    ssh_env: EnvironmentName
    password_env: EnvironmentName | None = None
    roles: Annotated[
        tuple[Annotated[str, Field(pattern=r"^(router|engine-[1-9][0-9]*)$")], ...],
        Field(min_length=1, max_length=256),
    ]

    @field_validator("ssh_env", "password_env")
    @classmethod
    def access_reference(cls, value: str | None) -> str | None:
        """Keep management credentials separate from process-control variables."""
        if value is not None and (
            value in {"PATH", "HOME", "ENV", "BASH_ENV", "IFS", "SHELLOPTS", "BASHOPTS", "CDPATH"}
            or value.startswith(
                (
                    "LD_",
                    "DYLD_",
                    "PYTHON",
                    "DOCKER_",
                    "GIT_",
                    "NARWHAL_MANAGEMENT_",
                    "NARWHAL_REMOTE_",
                )
            )
        ):
            raise ValueError("Reserved process-control names cannot supply SSH access")
        return value


class HostInventory(Exact):
    hosts: Annotated[tuple[SSHHost, ...], Field(min_length=1, max_length=100)]


def _document(path: Path, model: type[Exact], schema: str) -> Any:
    try:
        raw = read_input(path)
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("schema") != schema
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("Unsupported schema")
        return model.model_validate_json(raw)
    except AccessError as error:
        raise OperationError(error.code, error.message) from None
    except (ValueError, TypeError, RecursionError):
        raise OperationError(
            "invalid_input", "Registered SSH adapter document is invalid"
        ) from None


def load(target: ManagementTarget) -> SSHSettings:
    """Read one registered fleet adapter settings document."""
    if (
        target.kind != "fleet"
        or target.adapter.id != "ssh-v1"
        or target.adapter.settings_path is None
    ):
        raise OperationError("invalid_input", "SSH settings require a registered fleet target")
    return _document(target.adapter.settings_path, SSHSettings, "narwhal.ssh-settings")


def load_recipe(target: ManagementTarget, recipe_id: str) -> SSHRecipe:
    """Read a registered fixed recipe without accepting client paths or commands."""
    recipe = next((item for item in target.recipes if item.id == recipe_id), None)
    if recipe is None or recipe.kind != "fleet":
        raise OperationError("invalid_input", "Fleet recipe is not registered")
    return _document(recipe.path, SSHRecipe, "narwhal.ssh-recipe")


def load_hosts(settings: SSHSettings, environment: dict[str, str]) -> tuple[SSHHost, ...]:
    """Resolve existing host aliases while retaining only credential references."""
    try:
        hosts = HostInventory.model_validate_json(read_input(Path(settings.hosts_path))).hosts
        ids: set[str] = set()
        roles: set[str] = set()
        destinations: set[str] = set()
        for host in hosts:
            destination = environment.get(host.ssh_env, "")
            if (
                not destination
                or destination.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in destination)
            ):
                raise ValueError("Invalid SSH destination")
            if (
                host.id in ids
                or destination in destinations
                or roles & set(host.roles)
                or len(host.roles) != len(set(host.roles))
            ):
                raise ValueError("Host roles or destinations overlap")
            if host.password_env and not environment.get(host.password_env):
                raise ValueError("Missing SSH credential")
            ids.add(host.id)
            destinations.add(destination)
            roles.update(host.roles)
        if "router" not in roles or not any(role.startswith("engine-") for role in roles):
            raise ValueError("Missing router or engine role")
        return hosts
    except AccessError as error:
        raise OperationError(error.code, error.message) from None
    except (ValidationError, ValueError, TypeError):
        raise OperationError("invalid_input", "Registered SSH host inventory is invalid") from None


def verify_source(settings: SSHSettings, *, deadline: float | None = None) -> dict[str, Any]:
    """Compare the declared helpers to one Git archive within one overall deadline."""
    from .ssh_worker import WorkerError, _command

    deadline = time.monotonic() + 30 if deadline is None else deadline
    try:
        source = provenance.verified_source()
        if source["commit"] != settings.source_commit:
            raise ValueError("Source revision mismatch")
        root = Path(settings.source_root)
        with directory(root):
            pass
        executable = shutil.which("git", path=os.defpath)
        if executable is None:
            raise ValueError("Git unavailable")
        command = [executable, "-c", "core.hooksPath=/dev/null", "-C", str(root)]
        if (
            _command([*command, "rev-parse", "HEAD"], deadline).decode().strip()
            != settings.source_commit
        ):
            raise ValueError("Checkout revision mismatch")
        data = _command(
            [
                *command,
                "archive",
                "--format=tar",
                "HEAD",
                "src/narwhal",
                "tools",
                "config/fleet.example.json",
                "pyproject.toml",
                "constraints-dev.txt",
                "Makefile",
                "_narwhal_build.py",
                "MANIFEST.in",
            ],
            deadline,
            maximum=64 * 1024 * 1024,
        )
        entries = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for entry in archive:
                if entry.isdir():
                    continue
                if not entry.isfile() and not entry.issym():
                    raise ValueError("Source asset type is unsupported")
                if entry.issym():
                    entries[entry.name] = (entry.linkname.encode(), True)
                else:
                    stream = archive.extractfile(entry)
                    if stream is None:
                        raise ValueError("Source asset is missing")
                    entries[entry.name] = (stream.read(), False)
        required = (
            set(ASSETS)
            | {name for name in entries if name.startswith(("src/narwhal/", "tools/"))}
            | {"_narwhal_build.py", "MANIFEST.in"}
        )
        hashes = {}
        for name in sorted(required):
            if time.monotonic() >= deadline:
                raise WorkerError("stage_timeout", "Source verification deadline expired")
            if name not in entries:
                raise ValueError("Required source asset is absent")
            expected, symlink = entries[name]
            path = root / name
            if symlink:
                if not path.is_symlink() or os.readlink(path).encode() != expected:
                    raise ValueError("Source asset symlink differs")
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(root).as_posix()
                if relative not in entries or entries[relative][1]:
                    raise ValueError("Source asset link has an unapproved target")
                content = read_input(resolved, maximum=16 * 1024 * 1024)
                if content != entries[relative][0]:
                    raise ValueError("Source asset target differs")
            else:
                content = read_input(path, maximum=16 * 1024 * 1024)
                if content != expected:
                    raise ValueError("Source asset differs")
            hashes[name] = hashlib.sha256(content).hexdigest()
        for prefix in ("src/narwhal", "tools"):
            for path in (root / prefix).rglob("*"):
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                relative = path.relative_to(root).as_posix()
                if path.is_dir() and not path.is_symlink():
                    continue
                if relative not in entries and path.name not in {
                    "_build_provenance.json",
                    "_source_bundle.tar.gz",
                }:
                    raise ValueError("Source tree contains an unapproved asset")
        payload = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
        return {
            "source": source,
            "assets": hashes,
            "assets_sha256": hashlib.sha256(payload).hexdigest(),
        }
    except WorkerError as error:
        raise OperationError(
            error.code, "Registered source verification did not complete"
        ) from None
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError):
        raise OperationError(
            "prerequisite_failed",
            "Registered source assets do not match the installed verified revision",
        ) from None
