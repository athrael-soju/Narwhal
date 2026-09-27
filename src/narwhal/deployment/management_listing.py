"""List permitted target summaries through retained, registry-bound cursors."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from typing import Any
from uuid import UUID, uuid4

from .management_access import AccessError, directory
from .management_registry import ManagementRegistry, ManagementTarget

MAX_PAGE_BYTES = 220_000
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_CURSOR_BYTES = 1024
_STORE = "target-listing"


def _uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _invalid_cursor() -> AccessError:
    return AccessError("invalid_cursor", "Target listing cursor is invalid; restart listing")


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _summary(target: ManagementTarget) -> dict[str, Any]:
    # Listing does not open SSH settings. Host aliases come only from registered
    # log sources, plus the local host for dev targets, until discovery is added.
    hosts = {entry.host_id for entry in target.logs}
    if target.kind == "dev":
        hosts.add("local")
    return {
        "id": target.id,
        "kind": target.kind,
        "capabilities": list(target.capabilities),
        "actions": list(target.actions),
        "adapter_id": target.adapter.id,
        "recipes": [{"id": entry.id, "kind": entry.kind} for entry in target.recipes],
        "query_ids": [entry.id for entry in target.queries],
        "log_ids": [entry.id for entry in target.logs],
        "host_ids": sorted(hosts),
    }


def _pages(targets: list[dict[str, Any]], limit: int) -> list[tuple[int, int]]:
    overhead = len(json.dumps({"targets": [], "next_cursor": str(UUID(int=0))}).encode())
    pages = []
    start = 0
    size = overhead
    for index, target in enumerate(targets):
        item_size = len(json.dumps(target, ensure_ascii=False, allow_nan=False).encode())
        if item_size + overhead > MAX_PAGE_BYTES:
            raise AccessError("source_truncated", "Target summary exceeds the listing byte limit")
        separator = 2 if index > start else 0
        if index > start and (
            index - start >= limit or size + separator + item_size > MAX_PAGE_BYTES
        ):
            pages.append((start, index))
            start = index
            size = overhead
            separator = 0
        size += separator + item_size
    pages.append((start, len(targets)))
    return pages


def _private_directory(parent: int) -> int:
    with contextlib.suppress(FileExistsError):
        os.mkdir(_STORE, mode=0o700, dir_fd=parent)
    descriptor = os.open(
        _STORE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise AccessError(
            "permission_denied", "Target listing directory requires private ownership"
        )
    return descriptor


def _write(store: int, name: str, value: object) -> bytes:
    payload = _encode(value)
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=store,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return payload


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate key")
        value[name] = item
    return value


def _read(store: int, name: str, maximum: int) -> tuple[dict[str, Any], bytes]:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=store
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > maximum
            ):
                raise _invalid_cursor()
            payload = stream.read(maximum + 1)
            if len(payload) > maximum:
                raise _invalid_cursor()
        document = json.loads(payload, object_pairs_hook=_object)
        if not isinstance(document, dict):
            raise _invalid_cursor()
        return document, payload
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise _invalid_cursor() from None


def _resume(
    store: int,
    cursor: str,
    registry_id: str,
    digest: str,
    limit: int,
    targets: list[dict[str, Any]],
    pages: list[tuple[int, int]],
) -> dict[str, Any]:
    record, _ = _read(store, f"cursor-{cursor}.json", MAX_CURSOR_BYTES)
    keys = {
        "version",
        "registry_id",
        "registry_digest",
        "snapshot_id",
        "snapshot_sha256",
        "limit",
        "offset",
        "next_cursor",
    }
    if (
        set(record) != keys
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["registry_id"] != registry_id
        or record["registry_digest"] != digest
        or type(record["limit"]) is not int
        or record["limit"] != limit
        or type(record["offset"]) is not int
        or not _uuid(record["snapshot_id"])
        or (record["next_cursor"] is not None and not _uuid(record["next_cursor"]))
    ):
        raise _invalid_cursor()
    page = next((part for part in pages[1:] if part[0] == record["offset"]), None)
    if page is None or ((page[1] == len(targets)) != (record["next_cursor"] is None)):
        raise _invalid_cursor()
    snapshot, payload = _read(store, f"snapshot-{record['snapshot_id']}.json", MAX_SNAPSHOT_BYTES)
    expected = {
        "version": 1,
        "registry_id": registry_id,
        "registry_digest": digest,
        "targets": targets,
    }
    if (
        hashlib.sha256(payload).hexdigest() != record["snapshot_sha256"]
        or type(snapshot.get("version")) is not int
        or snapshot != expected
    ):
        raise _invalid_cursor()
    return {"targets": snapshot["targets"][page[0] : page[1]], "next_cursor": record["next_cursor"]}


def list_targets(
    registry: ManagementRegistry, *, limit: int = 20, cursor: str | None = None
) -> dict[str, Any]:
    """Return registered inspection targets without reading target-owned inputs.

    Cursors bind the complete registry snapshot and requested page limit. Dev
    summaries include host ``local``; fleet host aliases come only from logs
    declared in the registry, without opening adapter settings or discovering hosts.
    """
    if cursor is not None and not _uuid(cursor):
        raise _invalid_cursor()
    if type(limit) is not int or not 1 <= limit <= 100:
        if cursor is not None:
            raise _invalid_cursor()
        raise AccessError("invalid_input", "Target listing limit must be an integer from 1 to 100")
    registry_id = str(registry.registry_id)
    digest = hashlib.sha256(_encode(registry.model_dump(mode="json", by_alias=True))).hexdigest()
    targets = [
        _summary(target)
        for target in sorted(registry.targets, key=lambda value: value.id)
        if "inspect" in target.capabilities
    ]
    pages = _pages(targets, limit)
    try:
        with directory(registry.state_dir, private=True, create=True) as parent:
            store = _private_directory(parent)
            try:
                if cursor is not None:
                    return _resume(store, cursor, registry_id, digest, limit, targets, pages)
                snapshot_id = str(uuid4())
                snapshot = {
                    "version": 1,
                    "registry_id": registry_id,
                    "registry_digest": digest,
                    "targets": targets,
                }
                if len(_encode(snapshot)) > MAX_SNAPSHOT_BYTES:
                    raise AccessError("source_truncated", "Target snapshot exceeds its byte limit")
                payload = _write(store, f"snapshot-{snapshot_id}.json", snapshot)
                snapshot_hash = hashlib.sha256(payload).hexdigest()
                cursors = [str(uuid4()) for _ in pages[1:]]
                for index, ((offset, _), token) in enumerate(zip(pages[1:], cursors, strict=True)):
                    _write(
                        store,
                        f"cursor-{token}.json",
                        {
                            "version": 1,
                            "registry_id": registry_id,
                            "registry_digest": digest,
                            "snapshot_id": snapshot_id,
                            "snapshot_sha256": snapshot_hash,
                            "limit": limit,
                            "offset": offset,
                            "next_cursor": cursors[index + 1] if index + 1 < len(cursors) else None,
                        },
                    )
                os.fsync(store)
                return {
                    "targets": targets[: pages[0][1]],
                    "next_cursor": cursors[0] if cursors else None,
                }
            finally:
                os.close(store)
    except AccessError:
        raise
    except OSError:
        raise AccessError(
            "permission_denied", "Target listing storage is unavailable or unsafe"
        ) from None
