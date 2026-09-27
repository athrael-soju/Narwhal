"""Persist and read immutable text exports within one registered target."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from narwhal.deployment.management_registry import ManagementTarget

MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_READ_BYTES = 65536
_MAX_RECORD_BYTES = 65536
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_BINDING_FIELDS = (
    "id",
    "kind",
    "working_directory",
    "artifact_root",
    "fleet_file",
    "instance_dir",
    "adapter",
    "endpoints",
    "credential_env",
)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ArtifactError(ValueError):
    """Report a stable export failure without disclosing private paths or bytes."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Annotated[int, Field(ge=1, le=1)]
    registry_id: str
    target_id: str
    target_digest: Digest
    allow_request_content: bool
    artifact_id: str
    kind: str
    sha256: Digest
    size_bytes: Annotated[int, Field(ge=0, le=MAX_EXPORT_BYTES)]
    observed_at: str
    complete: bool


def _uuid(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ArtifactError("invalid_input", "Artifact and registry identifiers must be UUIDs")
    return str(UUID(value))


def _text(data: bytes) -> str:
    try:
        decoded = data.decode("utf-8")
    except UnicodeError:
        raise ArtifactError(
            "unsupported_media_type", "Artifact content must be UTF-8 text"
        ) from None
    if any(ord(character) < 32 and character not in "\t\n\r" for character in decoded):
        raise ArtifactError("unsupported_media_type", "Artifact content contains binary controls")
    return decoded


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise ArtifactError(
                "artifact_missing", "Artifact storage or export is missing"
            ) from None
        if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise ArtifactError(
                "permission_denied", "Artifact storage cannot be accessed safely"
            ) from None
        raise ArtifactError("artifact_changed", "Artifact storage could not be verified") from None


def _private(metadata: os.stat_result, *, directory: bool) -> None:
    correct_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not correct_type(metadata.st_mode):
        raise ArtifactError("permission_denied", "Artifact storage has an unsupported file type")
    required_mode = 0o700 if directory else 0o600
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != required_mode:
        raise ArtifactError(
            "permission_denied", "Artifact storage must be private and owned by this user"
        )
    if not directory and metadata.st_nlink != 1:
        raise ArtifactError(
            "permission_denied", "Artifact exports must not have additional hard links"
        )


@contextmanager
def _root(path: Path, *, create: bool) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise ArtifactError(
            "permission_denied", "Artifact roots must use absolute paths without traversal"
        )
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for index, component in enumerate(path.parts[1:], start=1):
            if create and index == len(path.parts) - 1:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _private(os.fstat(descriptor), directory=True)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _directory(parent: int, name: str, *, create: bool) -> Iterator[int]:
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=parent)
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _private(os.fstat(descriptor), directory=True)
        yield descriptor
    finally:
        os.close(descriptor)


def _read_file(parent: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        _private(before, directory=False)
        if before.st_size > limit:
            raise ArtifactError("artifact_changed", "Artifact exceeds its recorded storage limit")
        remaining = limit + 1
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, MAX_READ_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after or len(data) != before.st_size or len(data) > limit:
        raise ArtifactError("artifact_changed", "Artifact changed while being read")
    return data


def _write_file(parent: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(name, flags, mode=0o600, dir_fd=parent)
    with os.fdopen(descriptor, "wb") as stream:
        _private(os.fstat(stream.fileno()), directory=False)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class ArtifactStore:
    """Scope immutable exports to a registry and the target's resource binding."""

    def __init__(self, registry_id: str, target: ManagementTarget) -> None:
        self._registry_id = _uuid(registry_id)
        self._target = target
        document = target.model_dump(mode="json")
        binding = {field: document[field] for field in _BINDING_FIELDS}
        encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self._target_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @contextmanager
    def _scope(self, *, create: bool) -> Iterator[int]:
        with (
            _root(self._target.artifact_root, create=create) as root,
            _directory(root, ".management-artifacts", create=create) as store,
            _directory(store, self._registry_id, create=create) as registry,
            _directory(registry, self._target.id, create=create) as target,
        ):
            yield target

    def export(self, data: bytes, kind: str, complete: bool = True) -> dict[str, Any]:
        """Store already-redacted UTF-8 bytes and return their immutable reference."""
        if type(data) is not bytes or not isinstance(kind, str) or type(complete) is not bool:
            raise ArtifactError(
                "invalid_input", "Artifact bytes, kind or completeness has an invalid type"
            )
        if len(data) > MAX_EXPORT_BYTES:
            raise ArtifactError("artifact_too_large", "Artifact exceeds the export size limit")
        _text(data)
        record = _ArtifactRecord(
            version=1,
            registry_id=self._registry_id,
            target_id=self._target.id,
            target_digest=self._target_digest,
            allow_request_content=self._target.allow_request_content,
            artifact_id=str(uuid4()),
            kind=kind,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            observed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            complete=complete,
        )
        metadata = record.model_dump_json().encode("utf-8")
        if len(metadata) > _MAX_RECORD_BYTES:
            raise ArtifactError("artifact_too_large", "Artifact metadata exceeds the storage limit")
        with _storage_errors(), self._scope(create=True) as scope:
            os.mkdir(record.artifact_id, mode=0o700, dir_fd=scope)
            with _directory(scope, record.artifact_id, create=False) as artifact:
                _write_file(artifact, "content", data)
                _write_file(artifact, "record.json", metadata)
                os.fsync(artifact)
            os.fsync(scope)
        return {
            "artifact_id": record.artifact_id,
            "kind": kind,
            "state": "created",
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "observed_at": record.observed_at,
            "complete": complete,
        }

    def read(
        self, artifact_id: str, offset: int = 0, max_bytes: int = MAX_READ_BYTES
    ) -> dict[str, Any]:
        """Verify a retained export and read complete UTF-8 characters within its byte limit."""
        artifact_id = _uuid(artifact_id)
        if (
            type(offset) is not int
            or offset < 0
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_READ_BYTES
        ):
            raise ArtifactError("invalid_input", "Artifact offset or byte limit is invalid")
        with (
            _storage_errors(),
            self._scope(create=False) as scope,
            _directory(scope, artifact_id, create=False) as artifact,
        ):
            metadata = _read_file(artifact, "record.json", _MAX_RECORD_BYTES)
            try:
                record = _ArtifactRecord.model_validate_json(metadata)
            except ValueError:
                raise ArtifactError(
                    "artifact_changed", "Artifact metadata failed validation"
                ) from None
            if (
                record.registry_id != self._registry_id
                or record.target_id != self._target.id
                or record.target_digest != self._target_digest
            ):
                raise ArtifactError(
                    "permission_denied", "Artifact belongs to a different target binding"
                )
            if record.allow_request_content and not self._target.allow_request_content:
                raise ArtifactError(
                    "permission_denied", "Artifact request-content permission was revoked"
                )
            if record.artifact_id != artifact_id:
                raise ArtifactError(
                    "artifact_changed", "Artifact identity does not match its reference"
                )
            data = _read_file(artifact, "content", MAX_EXPORT_BYTES)
        if len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.sha256:
            raise ArtifactError(
                "artifact_changed", "Artifact bytes no longer match their recorded identity"
            )
        _text(data)
        if offset > len(data) or (offset < len(data) and data[offset] & 0xC0 == 0x80):
            raise ArtifactError(
                "invalid_input", "Artifact offset must be on a UTF-8 character boundary"
            )
        end = min(offset + max_bytes, len(data))
        while end > offset and end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        if end == offset and offset < len(data):
            raise ArtifactError(
                "invalid_input", "Artifact byte limit cannot fit the next UTF-8 character"
            )
        return {
            "artifact_id": artifact_id,
            "sha256": record.sha256,
            "text": data[offset:end].decode("utf-8"),
            "offset": offset,
            "next_offset": end if end < len(data) else None,
            "complete": end == len(data),
        }
