"""Run the installed inspection CLI with bounded output and owned-process cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import sysconfig
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypeVar

from narwhal import command_results, contracts

from . import stages

MAX_STDOUT_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
_ALLOWED = {
    ("config", "inspect"),
    ("config", "validate"),
    ("dev", "status"),
    ("diagnostics", "collect"),
}
_REAPER_LOCK = threading.Lock()
_REAPER_USERS = 0
_REAPER_CONTEXT: contextlib.AbstractContextManager[None] | None = None
_T = TypeVar("_T")


class CommandError(Exception):
    """A command boundary failure whose code and message are safe to return."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@contextlib.contextmanager
def _reaper() -> Iterator[None]:
    global _REAPER_USERS, _REAPER_CONTEXT
    with _REAPER_LOCK:
        if not _REAPER_USERS:
            _REAPER_CONTEXT = stages._reaper()
            _REAPER_CONTEXT.__enter__()
        _REAPER_USERS += 1
    try:
        yield
    finally:
        with _REAPER_LOCK:
            _REAPER_USERS -= 1
            if not _REAPER_USERS and _REAPER_CONTEXT is not None:
                _REAPER_CONTEXT.__exit__(None, None, None)
                _REAPER_CONTEXT = None


def _executable() -> Path:
    return Path(sysconfig.get_path("scripts")) / "narwhal"


async def _read(
    stream: asyncio.StreamReader, limit: int, overflow: asyncio.Event, *, retain: bool
) -> bytes:
    saved = bytearray()
    count = 0
    while block := await stream.read(65536):
        count += len(block)
        if count > limit:
            overflow.set()
        elif retain:
            saved.extend(block)
    return bytes(saved)


async def _collect(
    process: asyncio.subprocess.Process, overflow: asyncio.Event
) -> tuple[bytes, int]:
    assert process.stdout is not None and process.stderr is not None
    stdout, _, returncode = await asyncio.gather(
        _read(process.stdout, MAX_STDOUT_BYTES, overflow, retain=True),
        _read(process.stderr, MAX_STDERR_BYTES, overflow, retain=False),
        process.wait(),
    )
    return stdout, returncode


async def _settle(task: asyncio.Task[_T]) -> tuple[_T, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def _group_members(leader: int) -> dict[int, tuple[str, int, int, int]]:
    return {pid: row for pid, row in stages._processes().items() if row[2] == leader}


def _reap_group(leader: int) -> bool:
    members = _group_members(leader)
    for pid, row in members.items():
        if pid != leader and row[0] in {"Z", "X"}:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
    return bool(_group_members(leader))


async def _cleanup(process: asyncio.subprocess.Process, budget: float) -> None:
    deadline = time.monotonic() + budget
    escalate_at = time.monotonic() + budget / 2
    if process.returncode is not None and not _reap_group(process.pid):
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    escalated = False
    while True:
        present = _reap_group(process.pid)
        if process.returncode is not None and not present:
            await process.wait()
            return
        now = time.monotonic()
        if now >= escalate_at and not escalated:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            escalated = True
        if now >= deadline:
            raise CommandError(
                "recovery_required", "Command processes exceeded the cleanup deadline"
            )
        await asyncio.sleep(min(0.01, deadline - now))


async def _finish(
    process: asyncio.subprocess.Process,
    collection: asyncio.Task[tuple[bytes, int]] | None,
    budget: float,
) -> None:
    try:
        await _cleanup(process, budget)
    finally:
        if collection is not None:
            if not collection.done():
                collection.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await collection


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("nonfinite JSON value")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON value")
    return result


def _validate_output(payload: bytes, returncode: int) -> dict[str, Any]:
    try:
        document = json.loads(
            payload, object_pairs_hook=_object, parse_constant=_nonfinite, parse_float=_finite_float
        )
    except (ValueError, UnicodeError, RecursionError):
        raise CommandError("invalid_input", "Command returned invalid JSON output") from None
    if not isinstance(document, dict):
        raise CommandError("invalid_input", "Command returned an invalid result")
    try:
        contracts.validate_document(document, contracts.COMMAND_RESULT)
    except contracts.ContractVersionError:
        raise contracts.ContractVersionError("Command result contract is unsupported") from None
    status = document.get("status")
    exit_code = document.get("exit_code")
    if (
        not isinstance(status, str)
        or status not in command_results.EXIT_CODES
        or type(exit_code) is not int
        or exit_code != command_results.EXIT_CODES[status]
        or exit_code != returncode
        or document.get("command") != "narwhal"
        or not isinstance(document.get("operation"), str)
        or not isinstance(document.get("data"), dict)
        or not isinstance(document.get("errors"), list)
        or not isinstance(document.get("artifacts"), list)
    ):
        raise CommandError("invalid_input", "Command returned an inconsistent result")
    for error in document["errors"]:
        if not isinstance(error, dict) or not all(
            isinstance(error.get(key), str) for key in ("code", "message", "command")
        ):
            raise CommandError("invalid_input", "Command returned an invalid error entry")
        if "context" in error and not isinstance(error["context"], dict):
            raise CommandError("invalid_input", "Command returned an invalid error context")
    for artifact in document["artifacts"]:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("kind"), str)
            or not isinstance(artifact.get("path"), str)
            or not Path(artifact["path"]).is_absolute()
            or artifact.get("state") not in {"created", "updated", "existing", "missing"}
        ):
            raise CommandError("invalid_input", "Command returned an invalid artifact entry")
    return document


async def run_command(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    pass_fds: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Invoke an allowed finite action and preserve its validated command result.

    Callers must construct arguments and the environment from validated local
    registration. This function never accepts a client-selected executable.
    """
    if not all(isinstance(value, str) and "\x00" not in value for value in arguments):
        raise CommandError("invalid_input", "Command invocation failed validation")
    selected, json_mode = command_results.output_arguments(arguments)
    if (
        tuple(selected[:2]) not in _ALLOWED
        or not json_mode
        or isinstance(timeout_s, bool)
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
        or not cwd.is_absolute()
    ):
        raise CommandError("invalid_input", "Command invocation failed validation")
    reserve = min(1.0, max(0.1, timeout_s * 0.2))
    work_budget = max(timeout_s - reserve, timeout_s / 2)
    work_deadline = time.monotonic() + work_budget
    collection: asyncio.Task[tuple[bytes, int]] | None = None
    overflow_wait: asyncio.Task[bool] | None = None
    try:
        with _reaper():
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    str(_executable()),
                    *arguments,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=pass_fds,
                )
            )
            process, cancelled = await _settle(spawn)
            try:
                overflow = asyncio.Event()
                collection = asyncio.create_task(_collect(process, overflow))
                if cancelled:
                    raise asyncio.CancelledError
                overflow_wait = asyncio.create_task(overflow.wait())
                completed, _ = await asyncio.wait(
                    {collection, overflow_wait},
                    timeout=max(0.0, work_deadline - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if overflow.is_set():
                    raise CommandError("source_truncated", "Command output exceeded its byte limit")
                if collection not in completed:
                    raise CommandError("stage_timeout", "Command exceeded its execution deadline")
                payload, returncode = collection.result()
                return _validate_output(payload, returncode)
            finally:
                if overflow_wait is not None:
                    overflow_wait.cancel()
                finish = asyncio.create_task(_finish(process, collection, reserve))
                _, cancelled = await _settle(finish)
                if overflow_wait is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await overflow_wait
                if cancelled:
                    raise asyncio.CancelledError
    except (CommandError, contracts.ContractVersionError, asyncio.CancelledError):
        raise
    except OSError:
        raise CommandError(
            "source_unavailable", "Installed command could not be executed"
        ) from None
    except Exception:
        raise CommandError("operation_failed", "Command execution failed") from None
