"""Versioned outcomes for finite operator commands."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import httpx

from .contracts import COMMAND_RESULT, CONTRACTS, versioned

EXIT_CODES = {
    "success": 0,
    "failed_gate": 1,
    "invalid_input": 2,
    "degraded": 3,
    "error": 4,
    "interrupted": 130,
}


@dataclass
class _Result:
    command: str
    operation: str
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[Path, tuple[str, tuple[int, int] | None]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    secrets: set[str] = field(default_factory=set)


_active: ContextVar[_Result | None] = ContextVar("command_result", default=None)


def json_mode() -> bool:
    """Return whether the current invocation requested the result contract."""
    return _active.get() is not None


def add_format(parser: argparse.ArgumentParser) -> None:
    """Advertise the shared output selector in command help."""
    parser.add_argument("--format", choices=("text", "json"), default="text", help="output format")


def protect_environment(name: str) -> None:
    """Redact a configured credential whose variable may have an arbitrary name."""
    if (result := _active.get()) is not None and name and (value := os.environ.get(name)):
        result.secrets.add(value)


def set_operation(operation: str) -> None:
    """Name the selected finite operation."""
    if (result := _active.get()) is not None:
        result.operation = operation


def set_data(data: Mapping[str, Any]) -> None:
    """Attach structured operation data independently of diagnostic prose."""
    if (result := _active.get()) is not None:
        result.data.update(data)


def set_status(status: str) -> None:
    """Select a documented outcome and its process exit code."""
    if status not in EXIT_CODES:
        raise ValueError(f"unknown command status {status!r}")
    if (result := _active.get()) is not None:
        result.status = status


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def add_artifact(kind: str, path: str | Path) -> None:
    """Track an artifact before work so failures retain its actual final state."""
    if (result := _active.get()) is not None:
        resolved = Path(path).expanduser().absolute()
        result.artifacts.setdefault(resolved, (kind, _signature(resolved)))


def record_error(
    code: str,
    message: str,
    *,
    stage: str | None = None,
    engine: str | None = None,
    field: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> None:
    """Append a stable error code and the applicable operation context."""
    if (result := _active.get()) is not None:
        row: dict[str, Any] = {"code": code, "message": message, "command": result.command}
        row.update(
            {
                key: value
                for key, value in (("stage", stage), ("engine", engine), ("field", field))
                if value is not None
            }
        )
        if context:
            row["context"] = dict(context)
        result.errors.append(row)


class _RedactedStream:
    def __init__(self, stream: TextIO, redact: Callable[[str], str]) -> None:
        self.stream = stream
        self.redact = redact
        self.pending = ""
        self.argument_error = ""

    def write(self, value: str) -> int:
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            sanitized = self.redact(line)
            if ": error: " in sanitized:
                self.argument_error = sanitized.split(": error: ", 1)[1][:8192]
            self.stream.write(sanitized + "\n")
        return len(value)

    def flush(self) -> None:
        if self.pending:
            self.stream.write(self.redact(self.pending))
            self.pending = ""
        self.stream.flush()


@contextmanager
def _child_stdout(diagnostics: _RedactedStream) -> Iterator[None]:
    # Python stream replacement leaves inherited descriptors unchanged. Spool child
    # diagnostics to disk, keeping Python progress on a duplicate of the original stderr.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as capture:
        saved_stdout, saved_stderr = os.dup(1), os.dup(2)
        original_stream = diagnostics.stream
        progress = None
        try:
            try:
                if original_stream.fileno() == 2:
                    progress = os.fdopen(os.dup(saved_stderr), "w", encoding="utf-8")
                    diagnostics.stream = progress
            except (AttributeError, io.UnsupportedOperation):
                pass
            os.dup2(capture.fileno(), 1)
            os.dup2(capture.fileno(), 2)
            yield
        finally:
            diagnostics.flush()
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
            diagnostics.stream = original_stream
            if progress is not None:
                progress.close()
            capture.seek(0)
            for line in capture:
                diagnostics.write(line)
            diagnostics.flush()


def _redactor() -> Callable[[str], str]:
    secrets = sorted(
        {
            value
            for name, value in os.environ.items()
            if value and re.search(r"KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL", name, re.I)
        },
        key=len,
        reverse=True,
    )

    def redact(value: str) -> str:
        configured = _active.get()
        private = set(secrets) | (configured.secrets if configured is not None else set())
        for secret in sorted(private, key=len, reverse=True):
            if len(secret) < 4:
                value = re.sub(
                    r"(?<![\w-])" + re.escape(secret) + r"(?![\w-])", "[REDACTED]", value
                )
            else:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
        return re.sub(
            r"(?i)([?&](?:api[_-]?key|token|password|secret)=)[^&\s\"']+",
            r"\1[REDACTED]",
            value,
        )

    return redact


def output_arguments(argv: list[str]) -> tuple[list[str], bool]:
    """Separate output selection before dispatch or any command side effects."""
    stripped: list[str] = []
    requested = False
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--":
            stripped.extend(argv[index:])
            break
        if value in {"--format=json", "--format=text"}:
            requested = value.endswith("json")
        elif value == "--format" and index + 1 < len(argv) and argv[index + 1] in {"text", "json"}:
            index += 1
            requested = argv[index] == "json"
        else:
            stripped.append(value)
        index += 1
    return stripped, requested


def _exception(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted", "stage_cancelled" if hasattr(exc, "stage") else "interrupted"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or (
        hasattr(exc, "stage") and "timeout" in type(exc).__name__.lower()
    ):
        return "error", "stage_timeout"
    if isinstance(exc, FileNotFoundError):
        return "invalid_input", "input_missing"
    if isinstance(exc, FileExistsError):
        return "invalid_input", "output_exists"
    if isinstance(exc, PermissionError):
        return "error", "permission_denied"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_input", "invalid_input"
    if isinstance(exc, httpx.HTTPError):
        return "error", "engine_http_error"
    return "error", "operation_failed"


def invoke(
    command: str,
    argv: list[str] | None,
    callback: Callable[[list[str]], int],
    *,
    operation: str = "run",
    capture_child_stdout: bool = True,
) -> int:
    """Run one finite command, emitting exactly one requested JSON result."""
    arguments, requested = output_arguments(list(sys.argv[1:] if argv is None else argv))
    if not requested:
        return callback(arguments)
    result = _Result(command, operation)
    token = _active.set(result)
    stdout = sys.stdout
    redact = _redactor()
    diagnostics = _RedactedStream(sys.stderr, redact)
    try:
        children = _child_stdout(diagnostics) if capture_child_stdout else nullcontext()
        with children, redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            try:
                code = callback(arguments)
                if result.status is None:
                    result.status = {0: "success", 1: "failed_gate", 2: "invalid_input"}.get(
                        code, "error"
                    )
                if code and not result.errors:
                    record_error(result.status, "Operation completed with " + result.status)
            except SystemExit as exc:
                result.status = "success" if exc.code in (None, 0) else "invalid_input"
                if result.status != "success":
                    message = diagnostics.argument_error or "Command arguments failed validation"
                    option = re.search(r"--[a-zA-Z][a-zA-Z0-9-]*", message)
                    record_error(
                        "invalid_arguments",
                        message,
                        stage="arguments",
                        field=option.group() if option is not None else None,
                    )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                RuntimeError,
                httpx.HTTPError,
                subprocess.SubprocessError,
                KeyboardInterrupt,
            ) as exc:
                result.status, error = _exception(exc)
                record_error(
                    error,
                    str(exc) or result.status,
                    stage=getattr(exc, "stage", result.operation),
                    context=getattr(exc, "context", None),
                )
                print(str(exc) or result.status, file=sys.stderr)
        diagnostics.flush()
        artifacts = []
        for path, (kind, before) in result.artifacts.items():
            after = _signature(path)
            state = (
                "missing"
                if after is None
                else "created"
                if before is None
                else "existing"
                if before == after
                else "updated"
            )
            artifacts.append({"kind": kind, "path": str(path), "state": state})
        exit_code = EXIT_CODES[result.status or "error"]
        payload = versioned(
            COMMAND_RESULT,
            {
                "command": command,
                "operation": result.operation,
                "status": result.status,
                "exit_code": exit_code,
                "data": result.data,
                "artifacts": artifacts,
                "errors": result.errors,
            },
        )

        # Schema identity, status codes and hashes are wire metadata. Apply credential
        # redaction to operator data and diagnostic text before JSON escaping.
        schemas = {contract.schema for contract in CONTRACTS.values()}

        def clean(value: Any, key: str = "") -> Any:
            if isinstance(value, str):
                if key == "schema" and value in schemas:
                    return value
                if key.endswith(("sha256", "digest", "fingerprint")) and re.fullmatch(
                    r"(?:sha256:)?[0-9a-fA-F]{40,128}", value
                ):
                    return value
                return redact(value)
            if isinstance(value, dict):
                return {name: clean(item, name) for name, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [clean(item) for item in value]
            return value

        payload["data"] = clean(result.data)
        payload["artifacts"] = [{**row, "path": redact(row["path"])} for row in artifacts]
        payload["errors"] = [
            {
                **row,
                "message": redact(row["message"]),
                **({"context": clean(row["context"])} if "context" in row else {}),
            }
            for row in result.errors
        ]
        print(json.dumps(payload, sort_keys=True, allow_nan=False), file=stdout)
        return exit_code
    finally:
        _active.reset(token)
