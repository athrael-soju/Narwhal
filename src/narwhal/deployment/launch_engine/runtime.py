from __future__ import annotations

import ast
import contextlib
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

SALT_BYTES = 32
# The prefix of a keyed digest: an HMAC-SHA256 under a private salt.
KEYED = "hmac-sha256:"


def digest(path: Path) -> str:
    # Streams the file, so a multi-gigabyte model file stays out of memory.
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular_file_digest(path: Path) -> str:
    """Hash a regular file without following a symbolic link in its last path component."""
    # O_NONBLOCK keeps a FIFO from blocking the open; regular files ignore it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path} is not a regular file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def salt_file(path: Path, *, create: bool = True) -> bytes | None:
    """The 32 random bytes in `path`, at mode 0600, made once when `create` allows.

    Without `create`, a missing file returns None. A file of another size, owner or mode is
    refused.
    """
    if create and not path.exists() and not path.is_symlink():
        temporary = path.with_name(f".{path.name}-{secrets.token_hex(16)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secrets.token_bytes(SALT_BYTES))
                stream.flush()
                os.fsync(stream.fileno())
            # A link never replaces a salt another process made first.
            with contextlib.suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            temporary.unlink()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if create:
            raise
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        value = stream.read(SALT_BYTES + 1)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or len(value) != SALT_BYTES
    ):
        raise ValueError(
            f"{path} must be a regular file of {SALT_BYTES} bytes that this user owns at mode 0600"
        )
    return value


def keyed_digest(secret: bytes, digest: str) -> str:
    """The HMAC-SHA256 of `digest` under `secret`.

    A record keeps this in place of a digest that can cover a credential, so a guessed
    credential cannot be checked against the record without the salt.
    """
    return KEYED + hmac.new(secret, digest.encode(), hashlib.sha256).hexdigest()


def code_digest(path: Path) -> str:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(getattr(first.value, "value", None), str):
                node.body = node.body[1:] or [ast.Pass()]
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)


def write_once(path: Path, text: str, mismatch: str) -> None:
    try:
        write_private(path, text)
    except FileExistsError:
        if path.read_text() != text:
            raise ValueError(mismatch) from None
