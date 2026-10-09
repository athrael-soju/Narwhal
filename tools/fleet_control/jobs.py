"""The single load-job slot and the runner interface that load jobs implement."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

JobState = Literal["running", "succeeded", "failed", "stopped"]


class SlotBusy(RuntimeError):
    """A load job is already running."""


class JobRunner(Protocol):
    """Validate load-job parameters and run one job to completion.

    `run` executes inside the service's event loop. The slot cancels it to stop the job, so a
    runner that starts a client process must stop that process when cancelled.
    """

    def validate(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return normalized parameters, or raise ValueError for an invalid request."""
        ...

    async def run(self, job: Job) -> Mapping[str, Any]:
        """Run `job` and return its client results for the run record."""
        ...


@dataclass
class Job:
    """One load job: its parameters, output directory, state and client results."""

    id: str
    params: Mapping[str, Any]
    directory: Path
    started_at: str
    state: JobState = "running"
    finished_at: str | None = None
    result: Mapping[str, Any] | None = None
    error: str | None = None
    # The router journal rows the job produced, set when it finishes.
    journal: Mapping[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        """Return the job's status document."""
        return {
            "id": self.id,
            "params": dict(self.params),
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": None if self.result is None else dict(self.result),
            "error": self.error,
            "journal": None if self.journal is None else dict(self.journal),
        }


class JobSlot:
    """Hold at most one running load job.

    `start` checks and fills the slot without yielding to the event loop, so concurrent
    requests cannot both pass the check.
    """

    def __init__(
        self,
        runner: JobRunner,
        on_finish: Callable[[Job], None],
        clock: Callable[[], str],
    ) -> None:
        self.runner = runner
        self._on_finish = on_finish
        self._clock = clock
        self._job: Job | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def job(self) -> Job | None:
        """Return the running job, or the last one to finish."""
        return self._job

    @property
    def busy(self) -> bool:
        """Return whether a job occupies the slot."""
        return self._task is not None

    def clear(self) -> None:
        """Forget the last finished job."""
        if self._task is None:
            self._job = None

    def start(self, job: Job) -> Job:
        """Start `job`, or raise SlotBusy while another job runs."""
        if self._task is not None and self._job is not None:
            raise SlotBusy(f"load job {self._job.id} is running")
        self._job = job
        self._task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    async def stop(self) -> Job | None:
        """Cancel the running job and wait until its final state is recorded."""
        task, job = self._task, self._job
        if task is None or job is None:
            return None
        task.cancel()
        await asyncio.wait({task})
        # A task cancelled before its first step never enters `_run`.
        if self._task is task:
            self._close(job, "stopped")
        return job

    async def _run(self, job: Job) -> None:
        try:
            job.result = await self.runner.run(job)
        except asyncio.CancelledError:
            self._close(job, "stopped")
            raise
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            self._close(job, "failed")
        else:
            self._close(job, "succeeded")

    def _close(self, job: Job, state: JobState) -> None:
        job.state = state
        job.finished_at = self._clock()
        self._task = None
        self._on_finish(job)
