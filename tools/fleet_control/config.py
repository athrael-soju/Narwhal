"""Private control-service configuration and the bearer token it names."""

from __future__ import annotations

import ipaddress
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("config/fleet-control.local.json")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8020
DEFAULT_TOKEN_ENV = "NARWHAL_CONTROL_TOKEN"
DEFAULT_RUNS_DIR = Path("runs/fleet-control")
DEFAULT_HOOK_TIMEOUT_S = 900.0
# A token from `openssl rand -hex 32` or `secrets.token_urlsafe(32)` clears this floor.
MIN_TOKEN_LENGTH = 32
RESTORE_HOOK = "restore"
REQUIRED_HOOKS = frozenset({RESTORE_HOOK})
_KEYS = {"host", "port", "token_env", "runs_dir", "fleet", "hooks"}
_HOOK_KEYS = {"argv", "timeout_s"}


class ConfigError(ValueError):
    """Describe an operator-correctable configuration problem."""


@dataclass(frozen=True)
class Hook:
    """A deployment-specific command the service runs for one named operation."""

    name: str
    argv: tuple[str, ...]
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_S


@dataclass(frozen=True)
class ControlConfig:
    """Listener, credential, record location, baseline fleet and hook commands."""

    fleet: Path
    hooks: Mapping[str, Hook]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token_env: str = DEFAULT_TOKEN_ENV
    runs_dir: Path = DEFAULT_RUNS_DIR

    def hook(self, name: str) -> Hook:
        """Return the configured hook, or raise when the deployment did not name one."""
        try:
            return self.hooks[name]
        except KeyError:
            raise ConfigError(f"no {name!r} hook is configured") from None


def is_loopback(host: str) -> bool:
    """Return whether `host` names only the local machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def load_config(path: Path, env: Mapping[str, str]) -> ControlConfig:
    """Read and validate the private control configuration, reporting every problem."""
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the configuration must be a JSON object")
    problems = [f"unknown key {key!r}" for key in sorted(set(raw) - _KEYS)]
    host = raw.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or not is_loopback(host):
        problems.append(f"host must be a loopback address, got {host!r}")
    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append(f"port must be an integer from 1 through 65535, got {port!r}")
    token_env = raw.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not token_env:
        problems.append("token_env must name an environment variable")
    runs_dir = raw.get("runs_dir", str(DEFAULT_RUNS_DIR))
    if not isinstance(runs_dir, str) or not runs_dir:
        problems.append("runs_dir must be a path")
    fleet = raw.get("fleet", env.get("NARWHAL_FLEET"))
    if not isinstance(fleet, str) or not fleet:
        problems.append("fleet must name the baseline fleet configuration (or set NARWHAL_FLEET)")
    hooks = _read_hooks(problems, raw.get("hooks"))
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))
    return ControlConfig(
        fleet=Path(str(fleet)),
        hooks=hooks,
        host=str(host),
        port=int(port),
        token_env=str(token_env),
        runs_dir=Path(str(runs_dir)),
    )


def _read_hooks(problems: list[str], raw: object) -> dict[str, Hook]:
    if not isinstance(raw, dict):
        problems.append("hooks must be an object naming at least the restore command")
        return {}
    hooks = {}
    for name, spec in raw.items():
        label = f"hooks.{name}"
        if not isinstance(spec, dict):
            problems.append(f"{label} must be an object")
            continue
        problems.extend(f"unknown key {label}.{key}" for key in sorted(set(spec) - _HOOK_KEYS))
        argv = spec.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
        ):
            problems.append(f"{label}.argv must be a non-empty list of strings")
            continue
        timeout = spec.get("timeout_s", DEFAULT_HOOK_TIMEOUT_S)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            problems.append(f"{label}.timeout_s must be a positive number of seconds")
            continue
        hooks[name] = Hook(name, tuple(argv), float(timeout))
    problems.extend(f"hooks.{name} is required" for name in sorted(REQUIRED_HOOKS - set(raw)))
    return hooks


def read_token(config: ControlConfig, env: Mapping[str, str] = os.environ) -> str:
    """Return the bearer token from the configured environment variable."""
    token = env.get(config.token_env, "")
    if not token:
        raise ConfigError(f"{config.token_env} must hold the control bearer token")
    if len(token) < MIN_TOKEN_LENGTH or token != token.strip():
        raise ConfigError(
            f"{config.token_env} must hold at least {MIN_TOKEN_LENGTH} characters "
            "without surrounding whitespace"
        )
    return token
