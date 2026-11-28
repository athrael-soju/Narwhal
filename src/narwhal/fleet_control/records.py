"""Action log and per-session run records, written owner-only under the runs directory."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from narwhal.contracts import canonical_digest

RUN_SCHEMA = "narwhal.fleet-control-run"
RUN_SCHEMA_VERSION = 1
ACTION_LOG = "actions.jsonl"
RUN_RECORD = "run.json"
BASELINE = "baseline.json"
SESSIONS = "sessions"

Outcome = Literal["ok", "refused", "failed"]


def owner_only(path: str | os.PathLike[str], flags: int) -> int:
    return os.open(path, flags, 0o600)


def write_private(path: Path, value: object) -> None:
    """Replace a private JSON file atomically."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", opener=owner_only) as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


def timestamp(moment: datetime) -> str:
    return moment.isoformat()


@dataclass
class Action:
    """One operator action with its parameters, outcome and effect.

    `seq` numbers the actions of one session from 1; actions refused outside a session have none.
    """

    seq: int | None
    session: str | None
    name: str
    params: Mapping[str, Any]
    started_at: str
    finished_at: str
    outcome: Outcome
    result: Mapping[str, Any] | None = None
    error: str | None = None

    def document(self) -> dict[str, Any]:
        """Return the action as one log row and run-record entry."""
        return {
            "seq": self.seq,
            "session": self.session,
            "action": self.name,
            "params": dict(self.params),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "result": None if self.result is None else dict(self.result),
            "error": self.error,
        }


@dataclass
class Session:
    """One operator session: its baseline, applied configurations and actions."""

    id: str
    directory: Path
    started_at: str
    baseline_source: str
    baseline: Mapping[str, Any]
    configurations: list[dict[str, Any]] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    ended_at: str | None = None

    @property
    def baseline_path(self) -> Path:
        """Return the session's copy of the baseline fleet configuration."""
        return self.directory / BASELINE

    @property
    def fleet_path(self) -> Path:
        """Return the session's file holding the fleet configuration that governs the fleet."""
        return self.directory / self.configurations[-1]["fleet"]

    def apply_configuration(
        self, at: str, source: str, document: Mapping[str, Any], *, fleet: str = BASELINE
    ) -> None:
        """Record a fleet configuration that now governs the deployment.

        `fleet` names the session file that holds `document`, relative to the session directory.
        """
        self.configurations.append(
            {
                "applied_at": at,
                "source": source,
                "fleet": fleet,
                "digest": canonical_digest(document),
                "document": document,
            }
        )

    def document(self) -> dict[str, Any]:
        """Return the run record."""
        return {
            "schema": RUN_SCHEMA,
            "schema_version": RUN_SCHEMA_VERSION,
            "session": self.id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "baseline": {"source": self.baseline_source, "copy": BASELINE},
            "configuration": self.configurations[-1] if self.configurations else None,
            "configurations": self.configurations,
            "actions": [action.document() for action in self.actions],
        }


class RunStore:
    """Append actions to the shared log and keep each session's run record current."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def action_log(self) -> Path:
        """Return the append-only log of every authenticated action."""
        return self.root / ACTION_LOG

    def open_session(
        self, moment: datetime, baseline_source: str, baseline: Mapping[str, Any]
    ) -> Session:
        """Create a private session directory holding the baseline copy and run record."""
        sessions = self.root / SESSIONS
        sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
        session_id = f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        directory = sessions / session_id
        directory.mkdir(mode=0o700)
        session = Session(session_id, directory, timestamp(moment), baseline_source, baseline)
        write_private(session.baseline_path, baseline)
        session.apply_configuration(session.started_at, "baseline", baseline)
        self.save(session)
        return session

    def record(
        self,
        session: Session | None,
        name: str,
        params: Mapping[str, Any],
        started: str,
        finished: str,
        outcome: Outcome,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> Action:
        """Append one action to the log and, inside a session, to its run record."""
        action = Action(
            None if session is None else len(session.actions) + 1,
            None if session is None else session.id,
            name,
            params,
            started,
            finished,
            outcome,
            result,
            error,
        )
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps(action.document(), allow_nan=False)
        with open(self.action_log, "a", opener=owner_only) as stream:
            stream.write(line + "\n")
        if session is not None:
            session.actions.append(action)
            self.save(session)
        return action

    def save(self, session: Session) -> None:
        """Rewrite the session's run record."""
        write_private(session.directory / RUN_RECORD, session.document())
