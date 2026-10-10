from __future__ import annotations

import hashlib
import os
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
