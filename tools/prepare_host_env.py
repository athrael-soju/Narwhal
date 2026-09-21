"""Write a private router or engine environment from the workstation's loaded values."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

ENGINE_FIELDS = (
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
)
ENGINE_OPTIONAL = ("ATTEST_DOCUMENT_SOURCE", "ATTEST_DOCUMENT_SHA256")
ROUTER_OPTIONAL = ("ROUTER_URL", "GRAFANA_BIND_ADDRESS", "PROMETHEUS_LISTEN_ADDRESS")


def select_values(
    role: str, node: int | None, fleet: dict, env: Mapping[str, str]
) -> dict[str, str]:
    """Select role inputs, resolving node overrides before shared engine defaults."""
    values = {}

    def include(name: str, source: str | None = None, *, required: bool = True) -> None:
        source = source or name
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or "SSH" in name:
            raise ValueError("fleet environment references must use deployment variable names")
        value = env.get(source, "")
        if value:
            values[name] = value
        elif required:
            raise ValueError(f"{source} is unset or empty")

    include("NARWHAL_DEPLOYMENT_REVISION")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", values["NARWHAL_DEPLOYMENT_REVISION"]):
        raise ValueError("NARWHAL_DEPLOYMENT_REVISION must contain the full commit SHA")

    key_name = fleet.get("engine", {}).get("engine_api_key_env")
    if key_name:
        include(key_name)

    if role == "router":
        values["NARWHAL_FLEET"] = "config/fleet.local.json"
        for field in ROUTER_OPTIONAL:
            include(f"NARWHAL_{field}", required=False)
        for engine in fleet["engines"]:
            for field in ("url", "attestation_url"):
                reference = re.fullmatch(r"\$\{([^}]+)\}", engine.get(field, ""))
                if reference:
                    include(reference[1])
    elif role == "engine":
        if node is None or node < 1:
            raise ValueError("engine export requires a positive --node number")
        values["NARWHAL_ENGINE_LAUNCH_CONFIG"] = f"config/engine-launch.engine-{node}.json"
        for field in (*ENGINE_FIELDS, *ENGINE_OPTIONAL):
            name = f"NARWHAL_{field}"
            override = f"NARWHAL_NODE_{node}_{field}"
            include(name, override if override in env else name, required=field in ENGINE_FIELDS)
        for name in env:
            if re.fullmatch(r"NARWHAL_NODE_[1-9][0-9]*_IP", name):
                include(name)
        for field in ("URL", "ATTESTATION_URL"):
            include(f"NARWHAL_NODE_{node}_{field}", required=False)
    else:
        raise ValueError("role must be router or engine")
    return values


def write_environment(path: Path, values: Mapping[str, str]) -> None:
    """Create a mode-0600 shell file, preserving any existing destination."""
    content = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in sorted(values.items())
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def main(argv: list[str] | None = None) -> int:
    """Export loaded values locally; report field names and status only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("router", "engine"), required=True)
    parser.add_argument("--node", type=int)
    parser.add_argument("--fleet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        fleet = json.loads(args.fleet.read_text())
        values = select_values(args.role, args.node, fleet, os.environ)
        write_environment(args.out, values)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, FileExistsError):
            parser.exit(1, "Output already exists; select a fresh output path.\n")
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            parser.exit(1, f"{error}\n")
        parser.exit(1, "Check the supplied fleet structure, input access and output directory.\n")
    print(f"Prepared {args.role} environment ({len(values)} variables, mode 0600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
