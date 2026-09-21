"""Build metadata embedded in journal and benchmark files."""

from __future__ import annotations

import functools
import hashlib
import json
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any

from .contracts import versioned


def stamp(extra: dict[str, Any] | None = None, *, contract: str | None = None) -> dict[str, Any]:
    """Return package and source version metadata with optional run fields."""
    meta = {
        "package": "narwhal-inference",
        "version": _version(),
        "git": _describe(),
        "source": source_digest(),
    }
    if extra:
        overlap = set(meta) & set(extra)
        if overlap:
            raise ValueError(f"run metadata cannot replace {', '.join(sorted(overlap))}")
        meta.update(extra)
    if contract is not None:
        meta = versioned(contract, meta)
    return {"meta": meta}


@functools.cache
def source_digest() -> str:
    """SHA-256 of package Python files ordered by relative path.

    Identical source has the same digest in a checkout, deployment or wheel.
    """
    root = Path(__file__).resolve().parent
    read = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts):
        read.update(path.relative_to(root).as_posix().encode())
        read.update(b"\0")
        read.update(path.read_bytes())
        read.update(b"\0")
    return "sha256:" + read.hexdigest()


def stamp_line(extra: dict[str, Any] | None = None, *, contract: str | None = None) -> str:
    """Serialize the build stamp and optional run fields as one JSONL row."""
    return json.dumps(stamp(extra, contract=contract)) + "\n"


@functools.cache
def _version() -> str:
    try:
        return metadata.version("narwhal-inference")
    except metadata.PackageNotFoundError:
        return "unknown"


@functools.cache
def _describe() -> str | None:
    """Return the package source's Git description when it runs from a checkout."""
    here = Path(__file__).resolve().parent
    try:
        top = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],  # noqa: S607 - PATH lookup deliberate; absent git returns None
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
        if not top or not (Path(top) / "src" / "narwhal").is_dir():
            return None
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(here), "describe", "--always", "--dirty"],  # noqa: S607 - PATH lookup deliberate; absent git returns None
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None
