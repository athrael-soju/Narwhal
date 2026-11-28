"""Run deployment-specific hook commands and capture their outcome."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Hook
from .records import owner_only

TAIL_BYTES = 4096
KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class HookResult:
    """Exit status, duration and the output log of one hook run."""

    hook: str
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    log: Path
    tail: str

    @property
    def ok(self) -> bool:
        """Return whether the command exited 0 within its timeout."""
        return self.exit_code == 0 and not self.timed_out

    def document(self, base: Path) -> dict[str, Any]:
        """Return the run-record entry, naming the log relative to the session directory."""
        return {
            "hook": self.hook,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "log": str(self.log.relative_to(base)),
            "tail": self.tail,
        }


async def run_hook(hook: Hook, env: Mapping[str, str], log: Path) -> HookResult:
    """Run `hook` in its own process group, writing stdout and stderr to `log`.

    A command that outlives its timeout, or whose caller is cancelled, receives SIGTERM
    and then SIGKILL across its whole process group.
    """
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with open(log, "x", opener=owner_only) as output:
        process = await asyncio.create_subprocess_exec(
            *hook.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
            env=dict(env),
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), hook.timeout_s)
        except TimeoutError:
            timed_out = True
            await terminate(process)
        except asyncio.CancelledError:
            await asyncio.shield(terminate(process))
            raise
    return HookResult(
        hook.name,
        hook.argv,
        None if timed_out else process.returncode,
        timed_out,
        time.monotonic() - started,
        log,
        tail(log),
    )


async def terminate(process: asyncio.subprocess.Process) -> None:
    """Send SIGTERM, then SIGKILL after a grace period, to the process's whole group."""
    for sig, grace in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, None)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            await asyncio.wait_for(process.wait(), grace)
            return
        except TimeoutError:
            continue


def tail(path: Path) -> str:
    """Return the last TAIL_BYTES of a log file as text."""
    with open(path, "rb") as stream:
        stream.seek(max(0, path.stat().st_size - TAIL_BYTES))
        return stream.read().decode(errors="replace")
