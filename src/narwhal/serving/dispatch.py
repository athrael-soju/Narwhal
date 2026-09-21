"""Select an eligible engine using its current role and resident request count."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..runtime.standby import control_ready
from ..types import Instance, Phase, Request, Role
from .admission import AdmissionQueue

if TYPE_CHECKING:
    from .router import NarwhalRouter


class Dispatcher:
    """Bound phase waiters by admitted work and select using current capacity."""

    def __init__(self, router: NarwhalRouter) -> None:
        self.router = router
        self.queues = {
            phase: AdmissionQueue[Instance](
                router.max_concurrent, router.cfg.request_timeout_s, clock=router._clock
            )
            for phase in Phase
        }

    def notify(self) -> None:
        """Recheck both phase queues after a release or controller pass."""
        for queue in self.queues.values():
            queue.notify()

    async def place(self, request: Request, *, deadline: float) -> Instance:
        """Select after waiting; the caller must reserve before its next await."""
        router = self.router
        policy = router.cfg.serving
        phase = request.phase
        role = Role.PREFILL if phase is Phase.PREFILL else Role.DECODE
        limit = policy.prefill_concurrency if phase is Phase.PREFILL else policy.decode_concurrency

        def reserve() -> Instance | None:
            if router.lifecycle_blocked or router.monitoring_degraded:
                return None
            if phase is Phase.PREFILL and (router.failover_blocked or not control_ready(router)):
                return None
            live = router.scheduler.live_instances()
            pool = [inst for inst in live if inst.role is role] or live
            candidates = {
                inst.iid
                for inst in pool
                if not limit or len(inst.prefill if phase is Phase.PREFILL else inst.decode) < limit
            }
            if not candidates:
                return None
            excluded = set(router.monitor.instances) - candidates
            return router.scheduler.schedule(request, exclude=excluded)

        router.monitor.waiting[request.rid] = request
        try:
            return await self.queues[phase].acquire(reserve, deadline=deadline)
        finally:
            router.monitor.waiting.pop(request.rid, None)
