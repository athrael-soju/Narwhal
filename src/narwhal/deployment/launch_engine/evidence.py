"""Evidence directories of the engine validation commands, and the failure of a check."""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class CheckFailed(ValueError):
    """An inspected host or a probed engine failed a check that its evidence records."""


def evidence_directory(out: Path, names: Iterable[str]) -> Path:
    """Create `out` at mode 0700 when absent, and refuse it when it holds any of `names`.

    The stage runner passes each attempt its own directory, which already exists, so `out`
    may exist. An earlier attempt's evidence in it is never overwritten.
    """
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in names:
        path = out / name
        if path.exists() or path.is_symlink():
            raise FileExistsError(errno.EEXIST, "evidence already exists", str(path))
    return out.resolve()


def write_bytes(path: Path, data: bytes) -> None:
    """Create `path` at mode 0600 with `data`, refusing an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def write_json(path: Path, document: Any) -> None:
    write_bytes(path, (json.dumps(document, indent=2) + "\n").encode())
