"""Bound helper processes and retain their output and Linux ownership evidence."""

from __future__ import annotations

import contextlib
import ctypes
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path


def write_evidence(path: Path, record: dict) -> None:
    """Replace a private stage record while retaining its previous complete state."""
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            output.write(json.dumps(record, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class StageTimeout(ValueError):
    """A helper exhausted its execution budget; context records cleanup separately."""

    def __init__(self, stage: str, context: dict):
        self.stage = stage
        self.context = context
        super().__init__(
            f"{stage} exhausted {context['budget_seconds']:g}s; "
            f"inspect {context['evidence']}; {context['recovery']}"
        )


class StageCancelled(KeyboardInterrupt):
    """Cancellation with retained stage and owned-process cleanup evidence."""

    def __init__(self, stage: str, context: dict):
        self.stage = stage
        self.context = context
        super().__init__(f"{stage} cancelled; inspect {context['evidence']}")


def seconds(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


@contextlib.contextmanager
def cancellation() -> Iterator[None]:
    """Turn SIGTERM into an exception while the owning command can run its cleanup."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def _reaper() -> Iterator[None]:
    # Adopt grandchildren on Linux so termination also consumes their wait status.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "could not enable helper child reaping")
    try:
        yield
    finally:
        libc.prctl(36, previous.value, 0, 0, 0)


def _processes() -> dict[int, tuple[str, int, int, int]]:
    result = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                result[int(path.name)] = (
                    fields[0],
                    int(fields[1]),
                    int(fields[2]),
                    int(fields[19]),
                )
            except (FileNotFoundError, ProcessLookupError):
                pass
    return result


def _discover(owned: dict[int, int], leader: int) -> dict[int, tuple[str, int, int, int]]:
    processes = _processes()
    leader_matches = leader not in owned or (
        leader in processes and processes[leader][3] == owned[leader]
    )
    group_anchored = leader_matches or any(
        processes.get(pid, (None, None, None, None))[3] == ticks and processes[pid][2] == leader
        for pid, ticks in owned.items()
    )
    while True:
        added = {
            pid: values[3]
            for pid, values in processes.items()
            if pid not in owned
            and (
                (values[2] == leader and group_anchored)
                or (
                    values[1] in owned
                    and values[1] in processes
                    and processes[values[1]][3] == owned[values[1]]
                )
            )
        }
        if not added:
            break
        owned.update(added)
    return {pid: row for pid, row in processes.items() if owned.get(pid) == row[3]}


def _signal(owned: dict[int, int], sig: signal.Signals) -> None:
    for pid, ticks in owned.items():
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[19]) == ticks:
                signal.pidfd_send_signal(descriptor, sig)
        except (FileNotFoundError, ProcessLookupError):
            pass
        finally:
            os.close(descriptor)


def _cleanup(child: subprocess.Popen, owned: dict[int, int], grace: float, kill: float) -> dict:
    started = time.monotonic()
    escalated = False
    deadline = started + grace
    signalled: set[int] = set()
    while True:
        members = _discover(owned, child.pid)
        child.poll()
        for pid, row in members.items():
            if pid != child.pid and row[0] in {"Z", "X"}:
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
        live = {pid: row[3] for pid, row in members.items() if row[0] not in {"Z", "X"}}
        if not live:
            break
        now = time.monotonic()
        if now >= deadline:
            if escalated:
                break
            escalated = True
            deadline = now + kill
            signalled.clear()
        _signal(
            {pid: ticks for pid, ticks in live.items() if pid not in signalled},
            signal.SIGKILL if escalated else signal.SIGTERM,
        )
        signalled.update(live)
        time.sleep(0.02)
    child.poll()
    survivors = {
        pid: row[3] for pid, row in _discover(owned, child.pid).items() if row[0] not in {"Z", "X"}
    }
    return {
        "elapsed_seconds": time.monotonic() - started,
        "escalated": escalated,
        "surviving_processes": survivors,
    }


def run(
    command: list[str],
    *,
    stage: str,
    log: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    retain_descendants: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute one stage; execution and cleanup have independent finite budgets."""
    default = seconds("NARWHAL_STAGE_TIMEOUT_SECONDS", 300)
    override = "NARWHAL_STAGE_" + re.sub(r"[^A-Z0-9]", "_", stage.upper()) + "_TIMEOUT_SECONDS"
    budget = timeout if timeout is not None else seconds(override, default)
    grace = seconds("NARWHAL_STAGE_CLEANUP_GRACE_SECONDS", 10)
    kill = seconds("NARWHAL_STAGE_KILL_GRACE_SECONDS", 5)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("stage timeout must be finite and positive")
    token = uuid.uuid4().hex[:12]
    evidence = log.with_name(f"{log.name}.{token}.stage.json")
    stdout_path = log.with_name(f"{log.name}.{token}.stdout")
    stderr_path = log.with_name(f"{log.name}.{token}.stderr")
    context: dict = {
        "stage": stage,
        "started_at": time.time(),
        "budget_seconds": budget,
        "cleanup_grace_seconds": grace,
        "kill_grace_seconds": kill,
        "argv": command,
        "evidence": str(evidence),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "recovery": "inspect stage evidence and use dev status/down for an owned instance",
    }
    started = time.monotonic()
    failure: type[StageTimeout] | type[StageCancelled] | None = None
    child = None
    owned: dict[int, int] = {}
    with _reaper(), cancellation():
        with contextlib.ExitStack() as stack:
            outputs = [
                stack.enter_context(
                    os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
                )
                for path in (stdout_path, stderr_path)
            ]
            try:
                child = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=outputs[0],
                    stderr=outputs[1],
                    start_new_session=True,
                )
                _discover(owned, child.pid)
                context.update(pid=child.pid, processes=owned, status="running")
                write_evidence(evidence, context)
                while child.poll() is None:
                    _discover(owned, child.pid)
                    if time.monotonic() - started >= budget:
                        failure = StageTimeout
                        break
                    time.sleep(min(0.02, budget))
            except KeyboardInterrupt:
                failure = StageCancelled
            finally:
                if child is not None and (failure or not retain_descendants or child.poll() != 0):
                    saved = {}
                    if threading.current_thread() is threading.main_thread():
                        for sig in (signal.SIGINT, signal.SIGTERM):
                            saved[sig] = signal.signal(sig, signal.SIG_IGN)
                    try:
                        context["cleanup"] = _cleanup(child, owned, grace, kill)
                    except OSError as error:
                        context["cleanup"] = {"error": str(error), "surviving_processes": owned}
                    finally:
                        for sig, handler in saved.items():
                            signal.signal(sig, handler)
                context.update(
                    elapsed_seconds=time.monotonic() - started,
                    returncode=child.returncode if child else None,
                    status="cancelled"
                    if failure is StageCancelled
                    else "timeout"
                    if failure
                    else "completed",
                )
                write_evidence(evidence, context)
        stdout = stdout_path.read_text(errors="replace")
        stderr = stderr_path.read_text(errors="replace")
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as output:
            output.write(json.dumps(context) + "\n" + stdout + stderr)
    if failure:
        raise failure(stage, context)
    assert child is not None
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
