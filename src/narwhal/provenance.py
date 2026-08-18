"""Which code wrote a journal, recorded in the journal itself.

Journals and bench sample files open with one `{"meta": ...}` row carrying the
package version and, when the code runs from its own checkout, `git describe`.
A result file can then always answer "which build produced you". Readers skip
the row.
"""

from __future__ import annotations

import functools
import json
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any


def stamp() -> dict[str, Any]:
    """The meta row: package version, plus git describe when run from a checkout."""
    return {"meta": {"package": "narwhal-inference", "version": _version(), "git": _describe()}}


def stamp_line() -> str:
    """The stamp as one JSONL line."""
    return json.dumps(stamp()) + "\n"


@functools.cache
def _version() -> str:
    try:
        return metadata.version("narwhal-inference")
    except metadata.PackageNotFoundError:
        return "unknown"


@functools.cache
def _describe() -> str | None:
    """The build this module came from: `git describe`, or the deploy stamp.

    An installed wheel runs from site-packages, and whatever repository happens
    to enclose that directory is not this code's history. The toplevel must
    hold the package source before its describe is trusted. A fleet deploy
    ships no `.git` at all - `narwhal-fleet deploy` drops the source commit
    into DEPLOYED_COMMIT instead, and that file is the fallback, so journals
    written on a node still name their build.
    """
    here = Path(__file__).resolve().parent
    stamp = here / "DEPLOYED_COMMIT"
    try:
        top = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],  # noqa: S607 - PATH lookup deliberate; absent git returns None
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
        if not top or not (Path(top) / "src" / "narwhal").is_dir():
            return _read_stamp(stamp)
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(here), "describe", "--always", "--dirty"],  # noqa: S607 - PATH lookup deliberate; absent git returns None
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
        return out or _read_stamp(stamp)
    except (OSError, subprocess.SubprocessError):
        return _read_stamp(stamp)


def _read_stamp(stamp: Path) -> str | None:
    """The deploy's DEPLOYED_COMMIT, when this tree arrived by tarball."""
    try:
        text = stamp.read_text().strip()
    except OSError:
        return None
    return text or None
