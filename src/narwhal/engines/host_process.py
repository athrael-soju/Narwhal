from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

_STARTED_AT = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z")


def container_start(container: str) -> float:
    result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
        ["docker", "inspect", "--format", "{{.State.Running}} {{.State.StartedAt}}", container],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    running, _, started = result.stdout.strip().partition(" ")
    match = _STARTED_AT.fullmatch(started)
    if running != "true" or match is None:
        raise ValueError(f"container {container} is not running")
    seconds = datetime.fromisoformat(match.group(1) + "+00:00").timestamp()
    # Microseconds keep the value exact across reads of one container start.
    return seconds + int((match.group(2) or "0")[:6].ljust(6, "0")) / 1e6


def native_start(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ValueError(f"process {pid} is not running")
    boot = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def process_clock(*, container: str | None = None, pid: int | None = None) -> Callable[[], float]:
    if container is not None and pid is None:
        return lambda: container_start(container)
    if pid is not None and container is None:
        return lambda: native_start(pid)
    raise ValueError("a process clock needs exactly one container or process ID")
