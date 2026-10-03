"""Run Docker Compose against the pinned observability project."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from tools.observability.artifacts import MOUNTS

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
COMPOSE_UP_TIMEOUT_S = 300.0


class Stack(Protocol):
    """Compose operations consumed by the startup checks."""

    def up(self) -> None:
        """Create or update the project services."""

    def container(self, service: str) -> Container | None:
        """Return the project's container for one service."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ComposeStack:
    """Run Docker Compose against the repository's pinned project file."""

    def __init__(
        self,
        runner: Runner = subprocess.run,
        env: Mapping[str, str] | None = None,
        token_path: Path = RENDERER_TOKEN,
    ) -> None:
        self._runner = runner
        self._compose_env = dict(os.environ if env is None else env)
        prometheus, grafana = configured_services(self._compose_env)
        self._compose_env["NARWHAL_PROMETHEUS_URL"] = prometheus.listener.url
        self._compose_env["NARWHAL_GRAFANA_URL"] = grafana.listener.url
        self._compose_env["NARWHAL_RENDERER_ADDRESS"] = Listener(
            grafana.listener.host, RENDERER_PORT
        ).authority
        self._compose_env["NARWHAL_RENDERER_TOKEN"] = renderer_token(token_path)
        self._prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(BASE),
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
        result = self._run("up", "-d", timeout_s=COMPOSE_UP_TIMEOUT_S)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

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


def renderer_token(path: Path) -> str:
    """Return the Grafana-to-renderer token, creating a private one on first use."""
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_hex(32))
    return path.read_text().strip()
