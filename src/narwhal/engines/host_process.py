from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
# One private directory for each user holds the boot record.
BOOT_RECORDS = Path("/tmp")  # noqa: S108 - the record lives in a 0700 directory whose owner is checked
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


def _reported_boot_time() -> int:
    return next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )


def boot_time() -> int:
    """The boot time this host reported when a Narwhal process first read it in this boot.

    /proc/stat derives btime from the wall clock less the uptime, so every clock correction
    moves it, and a WSL2 guest corrects its clock continually. A start time added to the live
    value would differ between two reads of one process. Every reader of one user on one host
    takes the recorded value, which keeps a process start equal across readers and later
    than the starts of earlier processes and boots.
    """
    record = BOOT_RECORDS / f"narwhal-{os.geteuid()}" / f"boot-{BOOT_ID.read_text().strip()}"
    try:
        return int(record.read_text())
    except (OSError, ValueError):
        pass
    reported = _reported_boot_time()
    try:
        record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if record.parent.stat().st_uid != os.geteuid():
            return reported
        for stale in record.parent.glob("boot-*"):
            if stale != record:
                stale.unlink(missing_ok=True)
        # A link fails when another reader recorded the boot first, and that record wins.
        draft = record.with_name(f".{record.name}-{os.getpid()}")
        draft.write_text(str(reported))
        try:
            os.link(draft, record)
        except FileExistsError:
            pass
        finally:
            draft.unlink(missing_ok=True)
        return int(record.read_text())
    except (OSError, ValueError):
        return reported


def native_start(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ValueError(f"process {pid} is not running")
    return boot_time() + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def process_clock(*, container: str | None = None, pid: int | None = None) -> Callable[[], float]:
    if container is not None and pid is None:
        return lambda: container_start(container)
    if pid is not None and container is None:
        return lambda: native_start(pid)
    raise ValueError("a process clock needs exactly one container or process ID")
