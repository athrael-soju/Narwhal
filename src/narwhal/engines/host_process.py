from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger("narwhal.host")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
# Holds one directory for each user, mode 0700, checked for its owner before every use.
BOOT_RECORDS = Path("/tmp")  # noqa: S108 - see the comment above
DRAFT_SECONDS = 60.0
_warned = False
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


def _records() -> int:
    """Open this user's record directory, creating it; raise when others can reach it."""
    path = BOOT_RECORDS / f"narwhal-{os.geteuid()}"
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(directory)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(directory)
        raise ValueError(f"{path} must be a directory private to this user")
    return directory


def _recorded(directory: int, name: str) -> int | None:
    try:
        record = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(record) as stream:
        text = stream.read(32).strip()
    return int(text) if text.isdigit() else None


def _record(directory: int, name: str, reported: int) -> None:
    now = time.time()
    for entry in os.listdir(directory):
        earlier_boot = entry.startswith("boot-") and entry != name
        # A draft this old belongs to a reader that stopped before it finished.
        abandoned = entry.startswith(".boot-") and (
            now - os.stat(entry, dir_fd=directory, follow_symlinks=False).st_mtime > DRAFT_SECONDS
        )
        if earlier_boot or abandoned:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(entry, dir_fd=directory)
    draft = f".{name}-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(draft, flags, 0o600, dir_fd=directory), "w") as stream:
        stream.write(str(reported))
    try:
        try:
            # A link fails when another reader recorded the boot first, and that record wins.
            os.link(draft, name, src_dir_fd=directory, dst_dir_fd=directory)
        except FileExistsError:
            if _recorded(directory, name) is None:
                os.replace(draft, name, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            # A file system with no hard links takes the record by rename.
            os.replace(draft, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(draft, dir_fd=directory)


def boot_time() -> int:
    """The boot time the host reported when one of this user's processes first read it in
    this boot, kept in a private directory under `BOOT_RECORDS`.

    /proc/stat derives btime from the wall clock less the uptime, so each clock correction
    moves it. Readers that take the record agree on one value for the whole boot. A reader
    that cannot use the record logs a warning and returns the reported value.
    """
    reported = _reported_boot_time()
    try:
        name = f"boot-{BOOT_ID.read_text().strip()}"
        directory = _records()
    except (OSError, ValueError) as error:
        _warn(error)
        return reported
    try:
        recorded = _recorded(directory, name)
        if recorded is None:
            _record(directory, name, reported)
            recorded = _recorded(directory, name)
        if recorded is None:
            raise ValueError("the boot record holds no time")
        return recorded
    except (OSError, ValueError) as error:
        _warn(error)
        return reported
    finally:
        os.close(directory)


def _warn(error: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning(
            "boot time record unavailable (%s); a native engine's start time follows the "
            "system clock",
            error,
        )


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
