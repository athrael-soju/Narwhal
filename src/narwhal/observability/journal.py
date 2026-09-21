"""JSONL request journal."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import JOURNAL, versioned
from ..provenance import stamp_line


@dataclass
class RunJournal:
    """Append completed requests to a JSONL journal.

    Each process gets a run ID so readers can separate appended sessions.
    `extra` stamps run metadata into the provenance row; set it before open.
    """

    path: Path
    _fh: Any = None
    started: float = field(default_factory=time.time)
    run: str = ""
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.run = self.run or f"{int(self.started)}-{uuid.uuid4().hex[:8]}"

    def open(self) -> None:
        """Open the journal and append a provenance row."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", buffering=1)
        # Appended journals retain one provenance row per writer.
        self._fh.write(stamp_line(self.extra, contract=JOURNAL))

    def write(self, row: dict[str, Any]) -> None:
        """Append a request row tagged with this process's run ID."""
        if self._fh is not None:
            self._fh.write(json.dumps(versioned(JOURNAL, {"run": self.run, **row})) + "\n")

    def close(self) -> None:
        """Close the journal if open."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None
