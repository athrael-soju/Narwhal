"""Stream monitoring passes to Weights & Biases, without touching the loop.

Enabled by the fleet config's `wandb` block; without a project named there
the router carries no W&B state at all. Enabled, every monitoring pass
enqueues one point and a daemon thread forwards the queue, so the serving
path never waits on the network: a full queue drops the point, and any error
inside the worker disables the exporter for the life of the process.

Telemetry is configuration: a run's config file is its record, so the
destination lives beside the thresholds it was measured under. A harness
names each arm's run in the derived config it already writes.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import FleetConfig

log = logging.getLogger("narwhal.wandb")

QUEUE_POINTS = 1024


class Exporter:
    """A bounded queue in front of `wandb.log`, or nothing at all."""

    def __init__(self, project: str, run_name: str) -> None:
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_POINTS)
        self._dead = False
        self._step = 0
        self._worker = threading.Thread(target=self._drain, args=(project, run_name), daemon=True)
        self._worker.start()

    @classmethod
    def from_config(cls, cfg: FleetConfig | None) -> Exporter | None:
        """An exporter if the fleet config names a project, else None."""
        if cfg is None or not cfg.wandb_project:
            return None
        name = cfg.wandb_run or f"serve-{int(time.time())}"
        return cls(cfg.wandb_project, name)

    def log_pass(self, point: dict[str, Any]) -> None:
        """Enqueue one monitoring pass. Never blocks; a full queue drops."""
        if self._dead:
            return
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(point)

    def _drain(self, project: str, run_name: str) -> None:
        try:
            import wandb

            run = wandb.init(project=project, name=run_name, resume="allow")
            while True:
                point = self._q.get()
                run.log(point, step=self._step)
                self._step += 1
        except Exception as exc:
            self._dead = True
            log.warning("wandb exporter disabled: %s", exc)
