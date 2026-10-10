"""Operator sessions, recorded actions, the session's outstanding changes and the load-job slot."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from narwhal.config.loading import load as load_fleet
from narwhal.contracts import canonical_digest

from .config import ControlConfig, Hook
from .hooks import HookResult, run_hook
from .jobs import Job, JobRunner, JobSlot
from .journal import SESSION_EXTRACT, Mark, extract, mark
from .records import Action, Outcome, RunStore, Session, timestamp

Operation = Callable[[Session | None], Awaitable[Mapping[str, Any] | None]]

# Engine change each action leaves, by axis: process or router lifecycle.
ENGINE_CHANGES: dict[str, tuple[str, str | None]] = {
    "pause": ("process", "paused"),
    "stop": ("process", "stopped"),
    "resume": ("process", None),
    "start": ("process", None),
    "drain": ("lifecycle", "drained"),
    "readmit": ("lifecycle", None),
}
UNDO = {"paused": "resume", "stopped": "start", "drained": "readmit"}
# Actions that clear the engine changes.
CLEARING_ACTIONS = frozenset({"config.cold_restart"})
RESTORE_ACTION = "config.restore"


def session_changes(session: Session) -> dict[str, Any]:
    """Return the configuration and engine changes the session leaves in place, in undo order."""
    axes: dict[str, dict[str, str]] = {}
    for action in session.actions:
        if action.name == RESTORE_ACTION:
            # A restore step with no seq found the engine already in service.
            for step in (action.result or {}).get("steps", []):
                if step.get("seq") is None:
                    axes.get(str(step.get("engine")), {}).pop("lifecycle", None)
            continue
        if action.outcome != "ok":
            continue
        if action.name in CLEARING_ACTIONS:
            axes.clear()
            continue
        verb = action.name.removeprefix("engine.")
        if verb == action.name or verb not in ENGINE_CHANGES:
            continue
        iid = str(action.params.get("engine"))
        axis, change = ENGINE_CHANGES[verb]
        if change is None:
            axes.get(iid, {}).pop(axis, None)
        else:
            axes.setdefault(iid, {})[axis] = change
    engines = {
        iid: [changes[axis] for axis in ("process", "lifecycle") if axis in changes]
        for iid, changes in axes.items()
        if changes
    }
    baseline = session.configurations[0]["digest"]
    return {
        "configuration": session.configurations[-1]["digest"] != baseline,
        "engines": engines,
    }


class ActionError(Exception):
    """An action that was refused or failed, carrying its HTTP status and recorded entry."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        outcome: Outcome = "failed",
        result: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.outcome = outcome
        self.result = result
        self.action: Action | None = None


def refused(message: str, status: int = 409) -> ActionError:
    """Return the error for an action the service declined to attempt."""
    return ActionError(message, status=status, outcome="refused")


class ControlService:
    """Run operator actions against the fleet and record each one."""

    def __init__(
        self,
        config: ControlConfig,
        *,
        runner: JobRunner | None = None,
        env: Mapping[str, str] = os.environ,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.store = RunStore(config.runs_dir)
        self.session: Session | None = None
        self._env = env
        self._now = now
        self._exclusive: str | None = None
        self._exclusive_since: str | None = None
        self._job_count = 0
        # Router journal positions where the session and each load job began.
        self._session_mark: Mark | None = None
        self._job_marks: dict[str, Mark | None] = {}
        self.jobs = None if runner is None else JobSlot(runner, self._job_finished, self.stamp)

    def stamp(self) -> str:
        """Return the current time as recorded in actions."""
        return timestamp(self._now())

    async def act(
        self,
        name: str,
        params: Mapping[str, Any],
        operation: Operation,
        *,
        needs_session: bool = True,
        exclusive: bool = False,
    ) -> Action:
        """Run and record one action, raising ActionError when it does not succeed."""
        if self._exclusive is not None:
            error = refused(f"{self._exclusive} is in progress")
            error.action = self.store.record(
                self.session, name, params, self.stamp(), self.stamp(), "refused", error=str(error)
            )
            raise error
        if exclusive:
            self._exclusive = name
            self._exclusive_since = self.stamp()
        try:
            return await self._perform(name, params, operation, needs_session)
        finally:
            if exclusive:
                self._exclusive = None
                self._exclusive_since = None

    async def _perform(
        self, name: str, params: Mapping[str, Any], operation: Operation, needs_session: bool
    ) -> Action:
        started = self.stamp()
        try:
            if needs_session and self.session is None:
                raise refused("no session is active; start one with POST /api/session")
            result = await operation(self.session)
        except ActionError as exc:
            exc.action = self.store.record(
                self.session,
                name,
                params,
                started,
                self.stamp(),
                exc.outcome,
                result=exc.result,
                error=str(exc),
            )
            raise
        except Exception as exc:
            error = ActionError(f"{type(exc).__name__}: {exc}", status=500)
            error.action = self.store.record(
                self.session, name, params, started, self.stamp(), "failed", error=str(error)
            )
            raise error from exc
        return self.store.record(
            self.session, name, params, started, self.stamp(), "ok", result=result
        )

    async def start_session(self) -> Action:
        """Open a session and record the deployed baseline fleet configuration."""
        return await self.act("session.start", {}, self._start, needs_session=False, exclusive=True)

    async def _start(self, session: Session | None) -> Mapping[str, Any]:
        if session is not None:
            raise refused(f"session {session.id} is active")
        source = self.config.fleet
        try:
            load_fleet(source)
            baseline = json.loads(source.read_text())
        except (OSError, ValueError) as exc:
            raise ActionError(
                f"baseline fleet configuration is invalid: {exc}", status=500
            ) from exc
        self.session = self.store.open_session(self._now(), str(source), baseline)
        self._session_mark = mark(self.config.router.journal)
        self._job_count = 0
        if self.jobs is not None:
            self.jobs.clear()
        return {"session": self.session.id, "baseline_digest": canonical_digest(baseline)}

    async def end_session(self) -> Action:
        """Stop any load job, close the session and return the changes it leaves."""
        action = await self.act("session.end", {}, self._end, exclusive=True)
        self.session = None
        return action

    async def _end(self, session: Session | None) -> Mapping[str, Any]:
        assert session is not None
        if self.jobs is not None and self.jobs.busy:
            await self._perform("job.stop", {}, self._stop_job, True)
        journal = self.journal_extract(session.directory, SESSION_EXTRACT, self._session_mark)
        session.ended_at = self.stamp()
        return {"changes": session_changes(session), "journal": journal}

    def journal_extract(self, directory: Path, name: str, start: Mark | None) -> dict[str, Any]:
        """Copy the router journal rows appended since `start` into the session directory."""
        router = self.config.router
        return extract(router.journal, start, directory, name, router.journal_max_bytes)

    async def run_hook(
        self, hook: Hook, session: Session, extra_env: Mapping[str, str] | None = None
    ) -> HookResult:
        """Run a configured hook with the session's environment."""
        env = {key: value for key, value in self._env.items() if key != self.config.token_env}
        env.update(
            NARWHAL_CONTROL_SESSION=session.id,
            NARWHAL_CONTROL_RUN_DIR=str(session.directory.resolve()),
            NARWHAL_CONTROL_BASELINE=str(session.baseline_path.resolve()),
        )
        env.update(extra_env or {})
        logs = session.directory / "hooks"
        index = len(list(logs.glob("*.log"))) + 1 if logs.is_dir() else 1
        return await run_hook(hook, env, logs / f"{index:03d}-{hook.name}.log")

    async def start_job(self, params: Mapping[str, Any]) -> Action:
        """Start a load job in the single slot, refusing while another one runs."""

        async def start(session: Session | None) -> Mapping[str, Any]:
            assert session is not None
            if self.jobs is None:
                raise refused("no load-job runner is configured", 501)
            if self.jobs.busy and self.jobs.job is not None:
                raise refused(f"load job {self.jobs.job.id} is running")
            try:
                normalized = self.jobs.runner.validate(params)
            except ValueError as exc:
                raise refused(str(exc), 422) from exc
            self._job_count += 1
            job_id = f"job-{self._job_count:03d}"
            directory = session.directory / "jobs" / job_id
            directory.mkdir(mode=0o700, parents=True)
            self._job_marks[job_id] = mark(self.config.router.journal)
            job = self.jobs.start(Job(job_id, normalized, directory, self.stamp()))
            return {"job": job.document()}

        return await self.act("job.start", params, start)

    async def stop_job(self) -> Action:
        """Stop the running load job."""
        return await self.act("job.stop", {}, self._stop_job)

    async def _stop_job(self, session: Session | None) -> Mapping[str, Any]:
        job = None if self.jobs is None else await self.jobs.stop()
        if job is None:
            raise refused("no load job is running")
        return {"job": job.document()}

    def _job_finished(self, job: Job) -> None:
        # The job directory is <session>/jobs/<job id>.
        start = self._job_marks.pop(job.id, None)
        job.journal = self.journal_extract(job.directory.parent.parent, f"{job.id}.jsonl", start)
        self.store.record(
            self.session,
            "job.complete",
            {"job": job.id},
            job.started_at,
            job.finished_at or self.stamp(),
            "failed" if job.state == "failed" else "ok",
            result=job.document(),
            error=job.error,
        )

    def status(self) -> dict[str, Any]:
        """Return the active session, the current load job and any exclusive action."""
        job = None if self.jobs is None else self.jobs.job
        return {
            "session": None if self.session is None else self.session.id,
            "job": None if job is None else job.document(),
            "in_progress": self._exclusive,
            "in_progress_since": self._exclusive_since,
        }

    async def close(self) -> None:
        """Stop a running load job when the service shuts down."""
        if self.jobs is not None and self.jobs.busy:
            await self._perform("job.stop", {"reason": "service shutdown"}, self._stop_job, False)
