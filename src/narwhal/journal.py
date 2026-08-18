"""The per-request record: one JSONL row as each request completes.

Written by the router, read by narwhal-report, the oracle and
`narwhal-bench --score-journal`; docs/Api.md carries the row contract.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .provenance import stamp_line

log = logging.getLogger("narwhal.journal")


@dataclass
class RunJournal:
    """One line per request, appended as it completes.

    Every row carries `run`, an id minted when the journal is constructed. The
    file is opened for append, so two arms pointed at one path write into one
    file, and `run` is the boundary between them: `narwhal-bench
    --score-journal` scores whatever the file holds. `narwhal-serve --journal`
    gives each arm its own file instead.
    """

    path: Path
    _fh: Any = None
    started: float = field(default_factory=time.time)
    run: str = ""

    def __post_init__(self) -> None:
        self.run = self.run or f"{int(self.started)}-{uuid.uuid4().hex[:8]}"

    def open(self) -> None:
        """Open for append, and stamp a provenance row."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", buffering=1)
        # One provenance row per open; an appended-to journal keeps a row per
        # process that wrote it. Readers skip rows holding a `meta` key.
        self._fh.write(stamp_line())

    def write(self, row: dict[str, Any]) -> None:
        """One request row, tagged with this journal's run id."""
        if self._fh is not None:
            self._fh.write(json.dumps({"run": self.run, **row}) + "\n")

    def close(self) -> None:
        """Idempotent; flushes the line buffer."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class PayloadLog:
    """Opt-in per-request payload capture, joined to the journal by `rid`.

    The journal deliberately records lengths and timings, never content;
    this sidecar exists for debugging sessions that need the content too.
    Size discipline is two caps: every field truncates at `max_chars`
    (the full lengths are already in the journal row), and the file stops
    growing at `max_mb` - one warning, capture ends, serving continues.
    """

    def __init__(self, path: Path | str, max_chars: int = 2048, max_mb: int = 256) -> None:
        self.path = Path(path)
        self.max_chars = max_chars
        self.max_bytes = max_mb * 1024 * 1024
        self._fh: Any = None
        self._bytes = 0
        self._stopped = False

    def open(self) -> None:
        """Append mode: a restart continues the file and its size count."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a")
        self._bytes = self.path.stat().st_size

    def write(self, rid: str, prompt: str, output: str) -> None:
        """One row, both fields capped; a no-op once the file cap is hit."""
        if self._fh is None or self._stopped:
            return
        if self._bytes >= self.max_bytes:
            self._stopped = True
            log.warning(
                "payload capture stopped: %s reached its %d MB cap; "
                "the journal keeps recording lengths and timings",
                self.path,
                self.max_bytes // (1024 * 1024),
            )
            return
        row = {
            "rid": rid,
            "prompt": prompt[: self.max_chars],
            "prompt_truncated": len(prompt) > self.max_chars,
            "output": output[: self.max_chars],
            "output_truncated": len(output) > self.max_chars,
        }
        line = json.dumps(row, ensure_ascii=False) + "\n"
        self._fh.write(line)
        self._fh.flush()
        self._bytes += len(line.encode())

    def close(self) -> None:
        """Idempotent; the lifespan calls it at shutdown."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None
