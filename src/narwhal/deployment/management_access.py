"""Resolve registered inspection inputs and enforce local access boundaries."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from narwhal.contracts import FLEET, validate_document
from narwhal.diagnostics.bundle import Redactor

from .management_registry import ManagementRegistry, ManagementTarget

MAX_INPUT_BYTES = 8 * 1024 * 1024
_RESERVED_ENV = {"PATH", "HOME", "ENV", "BASH_ENV", "NARWHAL_MANAGEMENT_REGISTRY"}


class AccessError(ValueError):
    """Reject an inspection before access, with a client-safe stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def now() -> str:
    """Return a management observation timestamp in UTC."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextlib.contextmanager
def directory(path: Path, *, private: bool = False, create: bool = False) -> Iterator[int]:
    """Open an absolute directory without traversing symlinks or parent references."""
    if not path.is_absolute() or ".." in path.parts:
        raise AccessError(
            "permission_denied", "Registered directory is not a literal absolute path"
        )
    with contextlib.ExitStack() as stack:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        stack.callback(os.close, fd)
        for index, part in enumerate(path.parts[1:]):
            final = index == len(path.parts) - 2
            if create and final:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
            try:
                fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
            except FileNotFoundError:
                raise AccessError("input_missing", "Registered directory is missing") from None
            except OSError:
                raise AccessError(
                    "permission_denied", "Registered directory is unavailable or unsafe"
                ) from None
            stack.callback(os.close, fd)
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise AccessError(
                "permission_denied", "Registered directory ownership or mode is unsafe"
            )
        if private and stat.S_IMODE(metadata.st_mode) != 0o700:
            raise AccessError("permission_denied", "Private directory requires mode 0700")
        yield fd


@contextlib.contextmanager
def open_input(path: Path, *, maximum: int = MAX_INPUT_BYTES) -> Iterator[tuple[int, bytes]]:
    """Read one registered regular input with a byte bound and no symlink traversal."""
    if (
        path.name == ".env"
        or path.name.startswith(".env.")
        or path.suffix.lower() in {".env", ".pem", ".key", ".p12", ".pfx"}
        or path.name
        in {"id_rsa", "id_ed25519", "authorized_keys", "credentials", "credentials.json"}
    ):
        raise AccessError("permission_denied", "Credential files cannot be inspected")
    try:
        with directory(path.parent) as parent:
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
            )
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o022
                ):
                    raise AccessError(
                        "permission_denied", "Registered input ownership or type is unsafe"
                    )
                data = stream.read(maximum + 1)
                if len(data) > maximum:
                    raise AccessError("source_truncated", "Registered input exceeds its byte limit")
                stream.seek(0)
                yield stream.fileno(), data
    except FileNotFoundError:
        raise AccessError("input_missing", "Registered input is missing") from None
    except OSError:
        raise AccessError("permission_denied", "Registered input cannot be read safely") from None


def read_input(path: Path, *, maximum: int = MAX_INPUT_BYTES) -> bytes:
    """Read a bounded registered file through a pinned descriptor."""
    with open_input(path, maximum=maximum) as (_, data):
        return data


def parse_object(data: bytes) -> dict[str, Any]:
    """Parse a JSON object without including source text in errors."""
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError, RecursionError):
        raise AccessError("invalid_input", "Registered input is not a JSON object") from None
    if not isinstance(value, dict):
        raise AccessError("invalid_input", "Registered input is not a JSON object")
    return value


def read_object(path: Path) -> dict[str, Any]:
    """Read a bounded registered JSON object, without echoing its source on failure."""
    return parse_object(read_input(path))


def clean_command(document: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """Apply export policy while preserving command wire metadata and error codes."""
    return {
        **document,
        "data": redactor.value(document.get("data", {})),
        "artifacts": [
            {**row, "path": redactor.text(row["path"])} for row in document.get("artifacts", [])
        ],
        "errors": [
            {
                **row,
                "message": redactor.text(row["message"]),
                **({"context": redactor.value(row["context"])} if "context" in row else {}),
            }
            for row in document.get("errors", [])
        ],
    }


class InspectionAccess:
    """Use one immutable registration snapshot for bounded inspection calls."""

    def __init__(self, registry: ManagementRegistry) -> None:
        self.registry = registry
        self._targets = {target.id: target for target in registry.targets}

    def target(self, target_id: str) -> ManagementTarget:
        """Require an existing target and inspection grant before any access."""
        target = self._targets.get(target_id)
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        if "inspect" not in target.capabilities:
            raise AccessError("permission_denied", "Target does not permit inspection")
        return target

    def fleet(self, target: ManagementTarget) -> tuple[Path, dict[str, Any]]:
        """Resolve only the registered fleet or the dev instance's owned fleet path."""
        path = self.fleet_path(target)
        document = read_object(path)
        validate_document(document, FLEET)
        return path, document

    def fleet_path(self, target: ManagementTarget) -> Path:
        """Return the registered fleet path without opening it."""
        path = target.fleet_file
        if target.kind == "dev":
            if target.instance_dir is None:
                raise AccessError("invalid_input", "Dev target has no instance directory")
            path = target.instance_dir / "fleet.json"
        if path is None:
            raise AccessError("input_missing", "Target has no fleet path")
        return path

    def router(self, target: ManagementTarget) -> str:
        """Resolve the registered router origin at call time, rejecting URL credentials."""
        name = target.endpoints.router_env
        value = os.environ.get(name, "") if name else ""
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise AccessError(
                "source_unavailable", "Registered router endpoint is unset or invalid"
            )
        return value.rstrip("/")

    def redactor(
        self,
        target: ManagementTarget,
        document: dict[str, Any] | None = None,
        *,
        include_request_content: bool = False,
    ) -> Redactor:
        """Apply request-content permission and redact explicitly named credentials."""
        if include_request_content and not target.allow_request_content:
            raise AccessError("permission_denied", "Target does not permit request-content export")
        redactor = Redactor(include_request_content)
        for name in target.credential_env:
            if value := os.environ.get(name):
                redactor.secrets.add(value)
        if document is not None:
            redactor.references(document)
        return redactor

    def environment(self, target: ManagementTarget, document: dict[str, Any]) -> dict[str, str]:
        """Pass registered credentials and fleet endpoint references to the fixed CLI."""
        names = set(target.credential_env)
        engines = document.get("engines")
        for engine in engines if isinstance(engines, list) else []:
            if not isinstance(engine, dict):
                continue
            for field in ("url", "attestation_url"):
                value = engine.get(field)
                if isinstance(value, str) and (
                    match := re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
                ):
                    names.add(match[1])
        for name in names:
            if name in _RESERVED_ENV or name.startswith(("PYTHON", "LD_", "DYLD_")):
                raise AccessError(
                    "permission_denied", "Registered environment name controls process execution"
                )
        return {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            **{name: os.environ[name] for name in names if name in os.environ},
        }

    def audit(self, tool: str, target_id: str | None, outcome: str, codes: list[str]) -> None:
        """Append a private observation receipt without command output or arguments."""
        row = {
            "observed_at": now(),
            "tool": tool,
            "target_id": target_id,
            "outcome": outcome,
            "error_codes": codes,
        }
        self.write_audit("inspection", row)

    def write_audit(
        self, stream_name: Literal["inspection", "execution"], row: dict[str, Any]
    ) -> None:
        """Append one prepared receipt to an owned private file and synchronize it."""
        root = self.registry.state_dir
        with directory(root, private=True, create=True) as fd:
            name = f"{stream_name}-{self.registry.registry_id}.jsonl"
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
                | os.O_NONBLOCK,
                0o600,
                dir_fd=fd,
            )
            with os.fdopen(descriptor, "wb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise AccessError("permission_denied", "Management audit file is unsafe")
                stream.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())

    def check_storage(self, target: ManagementTarget) -> None:
        """Check private output roots before a collector writes or contacts a service."""
        with directory(self.registry.state_dir, private=True, create=True):
            pass
        with directory(target.artifact_root, private=True, create=True):
            pass
        with directory(target.working_directory):
            pass
