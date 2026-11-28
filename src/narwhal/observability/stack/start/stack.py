"""Run Docker Compose against the pinned observability project."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from narwhal.observability.stack.artifacts import MOUNTS, STATE

from .services import (
    COMMAND_TIMEOUT_S,
    Container,
    Listener,
    StartupError,
    configured_listener,
    configured_services,
)

BASE = Path(__file__).resolve().parents[1]
COMPOSE_FILE = BASE / "compose.yml"
RENDERER_PORT = 8081
RENDERER_TOKEN = MOUNTS.parent / "renderer-token"
# Grafana's admin password, private to the operator beside the renderer token, and the mount
# that hands a copy to the Grafana container, whose user cannot read the private file. The
# mounts directory itself is private to the operator.
ADMIN_PASSWORD = "grafana-admin-password"
ADMIN_PASSWORD_MOUNT = Path("mounts") / "grafana-secrets"
# Prometheus reads these staged files only at start.
PROMETHEUS_CONFIG = (
    MOUNTS / "prometheus" / "prometheus.yml",
    MOUNTS / "prometheus" / "prometheus-alerts.yml",
)
COMPOSE_UP_TIMEOUT_S = 300.0


class Stack(Protocol):
    """Compose operations consumed by the startup checks."""

    def up(self) -> None:
        """Create or update the project services."""

    def container(self, service: str) -> Container | None:
        """Return the project's container for one service."""

    def protect_grafana(self) -> None:
        """Set Grafana's admin password to the stack's private one, once Grafana is ready."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _local(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)


def compose_problem(runner: Callable[[Sequence[str]], Any] = _local) -> str | None:
    """Why this host cannot run the stack's Docker Compose project, or None when it can."""
    result = runner(["docker", "compose", "version"])
    if result.returncode == 0:
        return None
    lines = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
    answer = lines[0].strip() if lines else f"exit {result.returncode}"
    return (
        f"docker compose version failed ({answer}); the monitoring stack needs Docker Compose "
        "v2: install the Docker Compose plugin, the docker-compose-plugin package"
    )


class ComposeStack:
    """Run Docker Compose against the repository's pinned project file."""

    def __init__(
        self,
        runner: Runner = subprocess.run,
        env: Mapping[str, str] | None = None,
        token_path: Path = RENDERER_TOKEN,
        prometheus_config: Sequence[Path] = PROMETHEUS_CONFIG,
    ) -> None:
        self._runner = runner
        self._prometheus_config = prometheus_config
        self._compose_env = dict(os.environ if env is None else env)
        prometheus, grafana = configured_services(self._compose_env)
        self._compose_env["NARWHAL_PROMETHEUS_URL"] = prometheus.listener.url
        self._compose_env["NARWHAL_GRAFANA_URL"] = grafana.listener.url
        self._compose_env["NARWHAL_RENDERER_ADDRESS"] = Listener(
            grafana.listener.host, RENDERER_PORT
        ).authority
        self._compose_env["NARWHAL_RENDERER_TOKEN"] = renderer_token(token_path)
        self._password_path = token_path.parent / ADMIN_PASSWORD
        self._password_mount = token_path.parent / ADMIN_PASSWORD_MOUNT
        self._prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(STATE.resolve()),
            "-f",
            str(COMPOSE_FILE),
        ]

    def _run(
        self,
        *args: str,
        timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            [*self._prefix, *args],
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            env=self._compose_env,
        )

    def up(self) -> None:
        """Create or update Prometheus, Grafana and the image renderer."""
        stage_admin_password(self._password_path, self._password_mount)
        self._compose_env["NARWHAL_PROMETHEUS_CONFIG_DIGEST"] = config_digest(
            self._prometheus_config
        )
        result = self._run("up", "-d", timeout_s=COMPOSE_UP_TIMEOUT_S)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

    def protect_grafana(self) -> None:
        """Reset Grafana's admin password to the private one.

        Grafana reads `GF_SECURITY_ADMIN_PASSWORD__FILE` only when it creates its database, so a
        database from an earlier start keeps the password it had, such as the default `admin`.
        The password goes to the reset on standard input, never on a command line.
        """
        self._runner(
            [
                *self._prefix,
                "exec",
                "-T",
                "grafana",
                "grafana",
                "cli",
                "admin",
                "reset-admin-password",
                "--password-from-stdin",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
            env=self._compose_env,
            input=admin_password(self._password_path) + "\n",
        )

    def container(self, service: str) -> Container | None:
        """Inspect a service container selected through this Compose project."""
        result = self._run("ps", "--all", "--quiet", service)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return None
        if len(ids) != 1:
            raise StartupError(f"Compose returned {len(ids)} containers for {service}")
        cid = ids[0]
        inspected = self._runner(
            ["docker", "inspect", cid],
            check=True,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
        )
        documents = json.loads(inspected.stdout)
        if len(documents) != 1:
            raise StartupError(f"Docker returned an invalid inspection for {service}")
        document = documents[0]
        state = document.get("State", {})
        config = document.get("Config", {})
        return Container(
            cid=cid,
            image=str(config.get("Image", "")),
            status=str(state.get("Status", "unknown")),
            restarting=bool(state.get("Restarting", False)),
            listener=configured_listener(config, service),
        )


def config_digest(paths: Sequence[Path]) -> str:
    """Return the SHA-256 of the staged files, or an empty string before staging."""
    if not any(path.exists() for path in paths):
        return ""
    digest = hashlib.sha256()
    for path in paths:
        data = path.read_bytes() if path.exists() else b""
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def renderer_token(path: Path) -> str:
    """Return the Grafana-to-renderer token, creating a private one on first use."""
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_hex(32))
    return path.read_text().strip()


def admin_password(path: Path) -> str:
    """Return Grafana's admin password, creating a private random one on first use."""
    return renderer_token(path)


def stage_admin_password(path: Path, mount: Path) -> None:
    """Copy the admin password into the directory the Grafana container mounts.

    Grafana runs as its own user, which cannot read the operator's private file, so the copy is
    readable; the mounts directory above it is private to the operator.
    """
    password = admin_password(path)
    mount.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if mount.is_symlink():
        raise ValueError(f"Monitoring mount directory must be a real directory: {mount}")
    mount.mkdir(mode=0o755, exist_ok=True)
    mount.chmod(0o755)
    target = mount / ADMIN_PASSWORD
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o644)
        output.write(password)
    os.replace(temporary, target)
