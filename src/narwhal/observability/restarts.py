"""Restart counts of supervised processes, as the supervisor reports them.

On SSH hosts `narwhal deploy run` starts each sidecar and router as a systemd user unit and
names the unit in `NARWHAL_SUPERVISOR_UNIT`, so the process reads its unit's `NRestarts`. In
a Kubernetes pod the kubelet's `restartCount` of each container comes from the pod status.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

UNIT_ENV = "NARWHAL_SUPERVISOR_UNIT"
# Seconds a read restart count stays current, so a scrape every few seconds costs one read.
CACHE_S = 15.0


def systemd_restarts(unit: str) -> int | None:
    """The unit's `NRestarts`, or None when systemd does not report it."""
    try:
        output = subprocess.run(  # noqa: S603 - fixed argv, the unit name is an argument
            ["systemctl", "--user", "show", "--property=NRestarts", "--value", unit],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return int(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def pod_restarts(pod: dict) -> dict[str, int]:
    """Each container's `restartCount` from a pod object."""
    return {
        str(status["name"]): int(status.get("restartCount", 0))
        for status in pod.get("status", {}).get("containerStatuses") or []
        if "name" in status
    }


class Cached:
    """A reading that refreshes at most once every `CACHE_S` seconds."""

    def __init__(
        self,
        read: Callable[[], dict[str, int]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._clock = clock
        self._value: dict[str, int] = {}
        self._at: float | None = None

    def __call__(self) -> dict[str, int]:
        now = self._clock()
        if self._at is None or now - self._at >= CACHE_S:
            try:
                self._value = self._read()
            except (OSError, ValueError, RuntimeError):
                self._value = {}
            self._at = now
        return self._value


def supervisor(process: str) -> Cached | None:
    """The restart count of this process's systemd unit, when a unit names it."""
    unit = os.environ.get(UNIT_ENV)
    if not unit:
        return None

    def read() -> dict[str, int]:
        count = systemd_restarts(unit)
        return {} if count is None else {process: count}

    return Cached(read)


def render_restarts(supervised: dict[str, int], kubelet: dict[str, int] | None = None) -> list[str]:
    """`narwhal_supervisor_restarts` by process and `narwhal_kubelet_restarts` by container."""
    from .metrics.exposition import metric_lines

    out = metric_lines(
        "narwhal_supervisor_restarts",
        "Times the systemd unit of this process restarted it since the unit started",
        "gauge",
        [({"process": name}, count) for name, count in sorted(supervised.items())],
    )
    if kubelet is not None:
        out += metric_lines(
            "narwhal_kubelet_restarts",
            "The kubelet's restart count of each container of this engine pod",
            "gauge",
            [({"container": name}, count) for name, count in sorted(kubelet.items())],
        )
    return out
