"""Copy the router journal rows a session or load job produced into the session directory.

Journal rows carry router-monotonic times, not wall-clock time, so an extract is bounded by
file position instead: the service marks the journal's inode and size when a session or job
starts and copies the whole lines appended after that mark when it ends.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _copy(path: Path, start: Mark, session_dir: Path, name: str, max_bytes: int) -> dict[str, Any]:
    notes: list[str] = []
    with open(path, "rb") as source:
        stat = os.fstat(source.fileno())
        offset = start.offset
        if start.inode is None:
            offset = 0
            notes.append(CREATED)
        elif stat.st_ino != start.inode or stat.st_size < start.offset:
            offset = 0
            notes.append(ROTATED)
        directory = session_dir / JOURNAL_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        relative = f"{JOURNAL_DIR}/{name}"
        source.seek(offset)
        remaining = stat.st_size - offset
        copied = lines = 0
        terminal: Counter[str] = Counter()
        with open(session_dir / relative, "wb", opener=owner_only) as output:
            while remaining > 0:
                line = source.readline(remaining)
                if not line.endswith(b"\n"):
                    notes.append(PARTIAL)
                    break
                if copied + len(line) > max_bytes:
                    notes.append(f"truncated at {max_bytes} bytes")
                    break
                output.write(line)
                remaining -= len(line)
                copied += len(line)
                lines += 1
                value = _terminal(line)
                if value is not None:
                    terminal[value] += 1
    return {
        "extract": relative,
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
