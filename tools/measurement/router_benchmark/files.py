"""Private output files readable by their owner only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO


def owner_only(path: str | os.PathLike[str], flags: int) -> int:
    return os.open(path, flags, 0o600)


def open_private(path: Path) -> TextIO:
    """Create a private text file that must not exist yet."""
    return open(path, "x", opener=owner_only)


def write_private(path: Path, value: object) -> None:
    """Replace a private JSON file atomically."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", opener=owner_only) as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)
