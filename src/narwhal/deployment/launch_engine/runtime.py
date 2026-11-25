from __future__ import annotations

import ast
import hashlib
import os
import stat
from pathlib import Path


def digest(path: Path) -> str:
    # Streams the file, so a multi-gigabyte model file stays out of memory.
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular_file_digest(path: Path) -> str:
    """Hash a regular file without following a symbolic link in its last path component."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path} is not a regular file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
