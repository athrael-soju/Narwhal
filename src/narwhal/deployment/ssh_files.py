"""Transfer bounded chunks under one operation's private remote directory."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CHUNK_BYTES = 262_144
MAX_FILE_BYTES = 128 * 1024 * 1024


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextlib.contextmanager
def _parent(root: str, name: str, *, create: bool) -> Iterator[tuple[int, str]]:
    relative = Path(name)
    base = Path(root)
    if (
        not base.is_absolute()
        or ".." in base.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) < 3
        or relative.parts[0] != "operations"
        or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", relative.parts[1])
        or any("\0" in part for part in (*base.parts, *relative.parts))
    ):
        raise ValueError("Invalid operation file path")
    with contextlib.ExitStack() as stack:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        stack.callback(os.close, descriptor)
        parts = (*base.parts[1:], *relative.parts[:-1])
        for index, part in enumerate(parts):
            private = index >= len(base.parts) - 2
            if create and private:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
            )
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError("Operation directory requires private ownership")
        yield descriptor, relative.name


def _regular(fd: int, *, writable: bool = False, links: int = 1) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != links
        or (writable and info.st_size > MAX_FILE_BYTES)
    ):
        raise ValueError("Unsafe operation file")
    return info


def _recover_publication(
    parent: int, name: str, partial: str, descriptor: int, total: int, expected: str
) -> None:
    """Remove only the verified partial link left by interrupted publication."""
    before = _regular(descriptor, writable=True, links=2)
    if before.st_size != total:
        raise ValueError("Interrupted upload size differs from declared identity")
    try:
        partial_fd = os.open(
            partial, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
        )
    except FileNotFoundError:
        raise ValueError("Unsafe upload publication links") from None
    try:
        linked = _regular(partial_fd, links=2)
        if _identity(linked) != _identity(before):
            raise ValueError("Upload publication names do not identify the same file")
        value = hashlib.sha256()
        for offset in range(0, total, CHUNK_BYTES):
            maximum = min(CHUNK_BYTES, total - offset)
            chunk = os.pread(descriptor, maximum, offset)
            if len(chunk) != maximum:
                raise ValueError("Interrupted upload changed during verification")
            value.update(chunk)
        if value.hexdigest() != expected:
            raise ValueError("Interrupted upload digest differs from declared identity")
        if _identity(_regular(descriptor, links=2)) != _identity(before):
            raise ValueError("Interrupted upload changed during verification")
        for selected in (name, partial):
            current = os.stat(selected, dir_fd=parent, follow_symlinks=False)
            if _identity(current) != _identity(before):
                raise ValueError("Upload publication path changed during verification")
        os.unlink(partial, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(partial_fd)


def file_dispatch(request: dict[str, Any]) -> dict[str, Any]:
    """Handle fixed upload and artifact-read requests from the SSH transport."""
    operation = request["op"]
    if operation not in {"put", "read_file"}:
        raise ValueError("Unknown file operation")
    offset = request.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= MAX_FILE_BYTES:
        raise ValueError("Invalid file offset")
    with _parent(request["root"], request["path"], create=operation == "put") as (parent, name):
        if operation == "read_file":
            maximum = request["max_bytes"]
            if type(maximum) is not int or not 1 <= maximum <= CHUNK_BYTES:
                raise ValueError("Invalid read size")
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
            )
            try:
                before = _regular(descriptor)
                content = os.pread(descriptor, maximum, offset)
                after = os.fstat(descriptor)
                if _identity(before) != _identity(after):
                    raise ValueError("Operation artifact changed during read")
                return {
                    "data": base64.b64encode(content).decode(),
                    "offset": offset,
                    "size": before.st_size,
                    "identity": _identity(before),
                    "complete": offset + len(content) >= before.st_size,
                }
            finally:
                os.close(descriptor)
        total = request["total_size"]
        expected = request["sha256"]
        if (
            type(total) is not int
            or not 0 <= total <= MAX_FILE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            raise ValueError("Invalid upload identity")
        content = base64.b64decode(request["data"], validate=True)
        if len(content) > CHUNK_BYTES or offset + len(content) > total:
            raise ValueError("Upload chunk exceeds its bound")
        partial = name + ".upload-" + expected
        final = False
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
            )
            final = True
        except FileNotFoundError:
            descriptor = os.open(
                partial,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
        try:
            if final and os.fstat(descriptor).st_nlink == 2:
                _recover_publication(parent, name, partial, descriptor, total, expected)
            info = _regular(descriptor, writable=True)
            if info.st_size < offset or info.st_size > total:
                raise ValueError("Upload offset differs from retained bytes")
            present = os.pread(descriptor, len(content), offset)
            if present:
                if present != content:
                    raise ValueError("Upload chunk differs from retained bytes")
            elif content:
                if final:
                    raise ValueError("Existing artifact differs from upload")
                if os.pwrite(descriptor, content, offset) != len(content):
                    raise OSError("Short upload write")
                os.fsync(descriptor)
            size = os.fstat(descriptor).st_size
            complete = size == total
            if complete:
                value = hashlib.sha256()
                position = 0
                while chunk := os.pread(descriptor, CHUNK_BYTES, position):
                    value.update(chunk)
                    position += len(chunk)
                if value.hexdigest() != expected:
                    raise ValueError("Upload digest differs from declared identity")
                if not final:
                    os.link(
                        partial, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                    )
                    os.unlink(partial, dir_fd=parent)
                    os.fsync(parent)
            return {"size": size, "complete": complete, "sha256": expected}
        finally:
            os.close(descriptor)


class SSHFiles:
    """Keep transfer dispatch bounded by the operation's existing context."""

    def __init__(self, transport: Any, context: Any, root: str) -> None:
        self.transport, self.context, self.root = transport, context, root

    def put(self, host_id: str, path: str, content: bytes) -> dict[str, Any]:
        """Upload immutable bytes in chunks, checking cancellation before each chunk."""
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("Operation input exceeds upload limit")
        expected = hashlib.sha256(content).hexdigest()
        result: dict[str, Any] = {}
        for offset in range(0, max(1, len(content)), CHUNK_BYTES):
            self.context.assert_current()
            result = self.transport.rpc(
                host_id,
                {
                    "op": "put",
                    "root": self.root,
                    "path": path,
                    "offset": offset,
                    "data": base64.b64encode(content[offset : offset + CHUNK_BYTES]).decode(),
                    "sha256": expected,
                    "total_size": len(content),
                },
            )
        if not result.get("complete") or result.get("sha256") != expected:
            raise ValueError("Remote upload did not commit the declared input")
        return result

    def read(self, host_id: str, path: str, *, max_bytes: int) -> bytes:
        """Read one finite artifact and reject truncation or size changes."""
        content = bytearray()
        size = None
        identity = None
        while len(content) < max_bytes:
            self.context.assert_current()
            result = self.transport.rpc(
                host_id,
                {
                    "op": "read_file",
                    "root": self.root,
                    "path": path,
                    "offset": len(content),
                    "max_bytes": min(CHUNK_BYTES, max_bytes - len(content)),
                },
            )
            if size is None:
                size = result["size"]
                identity = result["identity"]
            if (
                result["size"] != size
                or size > max_bytes
                or result["identity"] != identity
                or result["offset"] != len(content)
            ):
                raise ValueError("Remote artifact changed or exceeds its byte limit")
            block = base64.b64decode(result["data"], validate=True)
            if len(block) > min(CHUNK_BYTES, max_bytes - len(content)):
                raise ValueError("Remote artifact chunk exceeds its byte limit")
            content.extend(block)
            if result["complete"]:
                if len(content) != size:
                    raise ValueError("Remote artifact ended before its declared size")
                return bytes(content)
            if not block:
                raise ValueError("Remote artifact read made no progress")
        raise ValueError("Remote artifact exceeds its byte limit")
