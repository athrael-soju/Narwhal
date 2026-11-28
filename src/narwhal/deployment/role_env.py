"""Select each router and engine role's private environment from the workstation's values."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .launch_engine.backend import EngineLauncher, cache_capture, launcher, transfer_measurement
from .launch_engine.settings import KV_SIDE_CHANNEL_PORT_ENV

ENGINE_FIELDS = (
    "ENGINE_IMAGE",
    "ENGINE_MODEL_NAME",
    "MODEL_DIR",
    "RUN_DIR",
    "MODEL_CONFIG_SHA256",
    "MODEL_TREE_SHA256",
    "FABRIC_INTERFACE",
    "ENGINE_PORT",
    "ATTEST_PORT",
    "UCX_TCP_PORT_RANGE",
    KV_SIDE_CHANNEL_PORT_ENV.removeprefix("NARWHAL_"),
)
# A deployment that serves a model file names it and pins its SHA-256.
MODEL_FILE_FIELDS = ("MODEL_PATH", "MODEL_SHA256")
ROUTER_OPTIONAL = (
    "ROUTER_URL",
    "TARGETS_URL",
    "FLEET_NAME",
    "ALERTMANAGER_URL",
    "GRAFANA_BIND_ADDRESS",
    "PROMETHEUS_LISTEN_ADDRESS",
)
# Engine hosts run helper snapshots from this directory of the deployment directory.
HELPER_DIRECTORY = "runs/deployment-tools"


@dataclass(frozen=True)
class Helper:
    """A standard-library script that engine hosts run as a copy checked by its SHA-256."""

    name: str
    source: Path
    path_env: str
    sha256_env: str


def helpers(engine: EngineLauncher) -> list[Helper]:
    """Return the helper snapshots that the engine hosts of a fleet on `engine` receive."""
    selected: list[Helper] = []
    if (capture := cache_capture(engine)) is not None:
        selected.append(
            Helper(
                "cache_capture_hook.py",
                capture.cache_hook,
                "NARWHAL_CACHE_CAPTURE_HOOK",
                "NARWHAL_CACHE_CAPTURE_HOOK_SHA256",
            )
        )
    if (measurement := transfer_measurement(engine)) is not None:
        selected.append(
            Helper(
                "transfer_measure.py",
                measurement.transfer_tool,
                "NARWHAL_TRANSFER_MEASURE_TOOL",
                "NARWHAL_TRANSFER_MEASURE_SHA256",
            )
        )
    return selected


def select_values(
    role: str, node: int | None, fleet: dict, env: Mapping[str, str], *, by_reference: bool = False
) -> dict[str, str]:
    """Select role inputs, resolving node overrides before shared engine defaults.

    The engine API key is the value of the variable the fleet's `engine_api_key_env` names. With
    `by_reference`, as on Kubernetes, the key stays in a Secret that each pod or Job calling an
    engine reads itself: the role file holds neither the key nor `NARWHAL_ENGINE_API_KEY`, and
    an environment that holds the key is refused.
    """
    values: dict[str, str] = {}

    def include(name: str, source: str | None = None, *, required: bool = True) -> None:
        source = source or name
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or "SSH" in name:
            raise ValueError("fleet environment references must use deployment variable names")
        value = env.get(source, "")
        if value:
            values[name] = value
        elif required:
            raise ValueError(f"{source} is unset or empty")

    key_name = fleet.get("engine", {}).get("engine_api_key_env")
    if by_reference:
        if held := sorted(n for n in (key_name, "NARWHAL_ENGINE_API_KEY") if n and env.get(n)):
            raise ValueError(
                f"{', '.join(held)} holds the engine API key; the engines read it from their "
                "Secret, so unset it"
            )
    elif key_name:
        include(key_name)
        if role == "engine":
            values["NARWHAL_ENGINE_API_KEY"] = values[key_name]

    if role == "router":
        values["NARWHAL_FLEET"] = "runs/deployment/fleet.json"
        for field in ROUTER_OPTIONAL:
            include(f"NARWHAL_{field}", required=False)
        for engine in fleet["engines"]:
            for field in ("url", "attestation_url"):
                reference = re.fullmatch(r"\$\{([^}]+)\}", engine.get(field, ""))
                if reference:
                    include(reference[1])
    elif role == "engine":
        if node is None or node < 1:
            raise ValueError("engine export requires a positive node number")
        for helper in helpers(launcher(fleet.get("engine", {}).get("backend"))):
            values[helper.path_env] = f"{HELPER_DIRECTORY}/{helper.name}"
        values["NARWHAL_ENGINE_LAUNCH_CONFIG"] = f"config/engine-launch.engine-{node}.json"
        for field in ENGINE_FIELDS:
            name = f"NARWHAL_{field}"
            override = f"NARWHAL_NODE_{node}_{field}"
            include(name, override if override in env else name)
        for field in MODEL_FILE_FIELDS:
            name = f"NARWHAL_{field}"
            override = f"NARWHAL_NODE_{node}_{field}"
            include(name, override if override in env else name, required=False)
        if sum(f"NARWHAL_{field}" in values for field in MODEL_FILE_FIELDS) == 1:
            raise ValueError("set NARWHAL_MODEL_PATH and NARWHAL_MODEL_SHA256 together")
        for name in env:
            if re.fullmatch(r"NARWHAL_NODE_[1-9][0-9]*_IP", name):
                include(name)
        for field in ("URL", "ATTESTATION_URL"):
            include(f"NARWHAL_NODE_{node}_{field}", required=False)
    else:
        raise ValueError("role must be router or engine")
    return values


def read_environment(path: Path) -> dict[str, str]:
    """Return the values of a role file that `write_environment` wrote."""
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        words = shlex.split(line)
        if len(words) != 2 or words[0] != "export" or "=" not in words[1]:
            raise ValueError(f"{path} line {number}: expected export NAME=value")
        name, value = words[1].split("=", 1)
        values[name] = value
    return values


def write_environment(path: Path, values: Mapping[str, str]) -> None:
    """Create a mode-0600 shell file, preserving any existing destination."""
    content = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in sorted(values.items())
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
