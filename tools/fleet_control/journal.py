"""Copy the router journal rows a session or load job produced into the session directory.

Journal rows carry router-monotonic times, not wall-clock time, so an extract is bounded by
file position instead: the service marks the journal's inode and size when a session or job
starts and copies the whole lines appended after that mark when it ends. A router pair writes
one journal per router; the service marks and copies each of them.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .records import owner_only

JOURNAL_DIR = "journal"
SESSION_EXTRACT = "session.jsonl"
NOT_CONFIGURED = "router.journal is not configured"
MISSING = "the router journal does not exist"
CREATED = "the router journal did not exist at the start mark; copied from its start"
ROTATED = (
    "the router journal was replaced or truncated after the start mark; "
    "copied from the start of the current file"
)
PARTIAL = "an incomplete last line was left out"


@dataclass(frozen=True)
class Mark:
    """The journal's inode and size at the start of a session or job, or None when absent."""

    inode: int | None
    offset: int


def mark(path: Path | None) -> Mark | None:
    """Return the journal's current position, or None when no journal is configured."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except FileNotFoundError:
        return Mark(None, 0)
    return Mark(stat.st_ino, stat.st_size)


def extract(
    path: Path | None, start: Mark | None, session_dir: Path, name: str, max_bytes: int
) -> dict[str, Any]:
    """Copy the whole journal lines appended since `start` to `journal/<name>` in the session.

    Return the run-record entry: the extract path relative to the session directory, the
    copied line and byte counts, terminal rows by `terminal` value, the file positions copied
    and any notes. The copy stops at the journal's size when the extract starts, at an
    incomplete last line, and at `max_bytes`.
    """
    if path is None or start is None:
        return {"extract": None, "notes": [NOT_CONFIGURED]}
    try:
        return _copy(path, start, session_dir, name, max_bytes)
    except FileNotFoundError:
        return {"extract": None, "notes": [MISSING]}
    except OSError as exc:
        return {"extract": None, "notes": [f"the journal extract failed: {exc}"]}


# Each configured journal's mark, or None when no journal is configured.
Marks = tuple[Mark | None, ...] | None


def marks(paths: Sequence[Path]) -> Marks:
    """Return each journal's current position, or None when no journal is configured."""
    return tuple(mark(path) for path in paths) if paths else None


def extract_all(
    paths: Sequence[Path],
    starts: Sequence[Mark | None] | None,
    session_dir: Path,
    name: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Copy the lines each journal gained since its start mark to `journal/<name>`.

    One journal gives the entry `extract` returns. Several journals, one per router of a pair,
    are copied one after another in `paths` order into the one extract, within `max_bytes` in
    all. Their entry adds `journals`, each journal's path, offsets, counts and notes.
    """
    if not paths or starts is None:
        return {"extract": None, "notes": [NOT_CONFIGURED]}
    if len(paths) == 1:
        return extract(paths[0], starts[0], session_dir, name, max_bytes)
    try:
        return _copy_all(paths, starts, session_dir, name, max_bytes)
    except OSError as exc:
        return {"extract": None, "notes": [f"the journal extract failed: {exc}"]}


def _copy_all(
    paths: Sequence[Path],
    starts: Sequence[Mark | None],
    session_dir: Path,
    name: str,
    max_bytes: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with contextlib.ExitStack() as stack:
        sources: list[tuple[Path, Mark | None, BinaryIO | None]] = []
        for path, start in zip(paths, starts, strict=True):
            try:
                source: BinaryIO | None = stack.enter_context(open(path, "rb"))
            except FileNotFoundError:
                source = None
            sources.append((path, start, source))
        if all(source is None for _, _, source in sources):
            missing = [{"journal": str(path), "notes": [MISSING]} for path in paths]
            return {"extract": None, "notes": [MISSING], "journals": missing}
        relative = _output(session_dir, name)
        with open(session_dir / relative, "wb", opener=owner_only) as output:
            budget = max_bytes
            for path, start, source in sources:
                if source is None:
                    rows.append({"journal": str(path), "notes": [MISSING]})
                    continue
                row = _append(source, start or Mark(None, 0), output, budget, cap=max_bytes)
                budget -= row["bytes"]
                rows.append({"journal": str(path), **row})
    terminal: Counter[str] = Counter()
    for row in rows:
        terminal.update(row.pop("terminal", {}))
    return {
        "extract": relative,
        "lines": sum(row.get("lines", 0) for row in rows),
        "bytes": sum(row.get("bytes", 0) for row in rows),
        "terminal": dict(sorted(terminal.items())),
        "notes": [],
        "journals": rows,
    }


def _output(session_dir: Path, name: str) -> str:
    directory = session_dir / JOURNAL_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    return f"{JOURNAL_DIR}/{name}"


def _copy(path: Path, start: Mark, session_dir: Path, name: str, max_bytes: int) -> dict[str, Any]:
    with open(path, "rb") as source:
        relative = _output(session_dir, name)
        with open(session_dir / relative, "wb", opener=owner_only) as output:
            row = _append(source, start, output, max_bytes)
    return {"extract": relative, **row}


def _append(
    source: BinaryIO, start: Mark, output: BinaryIO, max_bytes: int, *, cap: int | None = None
) -> dict[str, Any]:
    """Append the whole lines `source` gained since `start` to `output`, up to `max_bytes`.

    `cap` is the size limit of the whole extract that a truncation note names.
    """
    notes: list[str] = []
    stat = os.fstat(source.fileno())
    offset = start.offset
    if start.inode is None:
        offset = 0
        notes.append(CREATED)
    elif stat.st_ino != start.inode or stat.st_size < start.offset:
        offset = 0
        notes.append(ROTATED)
    source.seek(offset)
    remaining = stat.st_size - offset
    copied = lines = 0
    terminal: Counter[str] = Counter()
    while remaining > 0:
        line = source.readline(remaining)
        if not line.endswith(b"\n"):
            notes.append(PARTIAL)
            break
        if copied + len(line) > max_bytes:
            notes.append(f"truncated at {cap or max_bytes} bytes")
            break
        output.write(line)
        remaining -= len(line)
        copied += len(line)
        lines += 1
        value = _terminal(line)
        if value is not None:
            terminal[value] += 1
    return {
        "start_offset": offset,
        "end_offset": offset + copied,
        "lines": lines,
        "bytes": copied,
        "terminal": dict(sorted(terminal.items())),
        "notes": notes,
    }


def _terminal(line: bytes) -> str | None:
    """Return a terminal request row's `terminal` value."""
    if b'"terminal"' not in line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    value = row.get("terminal") if isinstance(row, dict) else None
    return value if isinstance(value, str) else None
