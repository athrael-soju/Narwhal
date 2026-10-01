"""Peer release rounds after an engine ejection or drain.

A vLLM NIXL consumer keeps a producer's KV memory mapped until a consume request
finds that producer idle for longer than the connector's `engine_ttl`. Each round
sends one transfer probe through every live consumer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..engines.validation import can_consume, can_produce
from ..types import LEG_CONNECTION

if TYPE_CHECKING:
    from ..serving.router import NarwhalRouter

log = logging.getLogger("narwhal.peer_release")

# Seconds after ejection or drain. The first round follows the launcher's 60 s engine_ttl;
# the last follows vLLM's 3600 s default.
RELEASE_AFTER_S = (65.0, 125.0, 245.0, 485.0, 965.0, 1925.0, 3845.0)
# Seconds between attempts for consumers a round missed; vLLM sends lease heartbeats at most
# every 5 s.
RETRY_AFTER_S = 5.0


class PeerRelease:
    """Release-round schedule for each ejected or drained engine."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._rounds: dict[str, tuple[float, int]] = {}
        self.tasks: set[asyncio.Task[dict[str, str]]] = set()
        self.missed: set[str] = set()
        self.retry_at = 0.0

    def finish(self, task: asyncio.Task[dict[str, str]]) -> None:
        """Keep consumers without a delivered probe for the next attempt."""
        if task.cancelled() or task.exception() is not None:
            return
        self.missed = {iid for iid, outcome in task.result().items() if outcome != "sent"}
        self.retry_at = self._clock() + RETRY_AFTER_S

    def retry_due(self) -> bool:
        """Return whether missed consumers are due another attempt."""
        return bool(self.missed) and self._clock() >= self.retry_at

    def due(self, engines: Iterable[str]) -> list[str]:
        """Return engines with a due round and advance their schedules."""
        now = self._clock()
        current = set(engines)
        for iid in set(self._rounds) - current:
            del self._rounds[iid]
        due = []
        for iid in sorted(current):
            since, done = self._rounds.setdefault(iid, (now, 0))
            if done < len(RELEASE_AFTER_S) and now - since >= RELEASE_AFTER_S[done]:
                self._rounds[iid] = (since, done + 1)
                due.append(iid)
        return due

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        """Completed rounds and seconds until the next round per engine."""
        now = self._clock()
        return {
            iid: {
                "rounds": done,
                "next_round_s": (
                    round(max(0.0, since + RELEASE_AFTER_S[done] - now), 3)
                    if done < len(RELEASE_AFTER_S)
                    else None
                ),
            }
            for iid, (since, done) in sorted(self._rounds.items())
        }


def released_engines(router: NarwhalRouter) -> set[str]:
    """Return ejected engines and engines that a lifecycle drain released."""
    held = {
        iid
        for iid, record in router.lifecycle.records.items()
        if record.state not in ("active", "draining")
    }
    return set(router.scheduler.ejected) | held


def release_peers(router: NarwhalRouter) -> None:
    """Start a due release round, or retry the consumers the last round missed."""
    release = router.peer_release
    if router.cfg.engine_restart_policy != "individual" or release.tasks:
        return
    released = released_engines(router)
    gone = release.due(released)
    only: set[str] | None = None
    if not gone:
        if not released or not release.retry_due():
            return
        gone, only = sorted(released), release.missed
    task = asyncio.create_task(release_round(router, gone, only))
    release.tasks.add(task)
    task.add_done_callback(release.tasks.discard)
    task.add_done_callback(release.finish)


async def release_round(
    router: NarwhalRouter, gone: list[str], only: set[str] | None = None
) -> dict[str, str]:
    """Send one transfer probe through each live consumer and return each outcome."""
    specs = {spec.iid: spec for spec in router.cfg.engines}
    live = [inst for inst in router.scheduler.live_instances() if inst.iid in specs]
    producers = [inst for inst in live if can_produce(specs[inst.iid])]
    consumers = [
        inst for inst in live if can_consume(specs[inst.iid]) and (only is None or inst.iid in only)
    ]
    deadline = max(router.cfg.first_token_timeout_s or 0.0, router.cfg.health_timeout_s)

    async def probe(index: int, consumer_iid: str, url: str) -> str:
        others = [inst for inst in producers if inst.iid != consumer_iid]
        if not others:
            return "no producer"
        producer = others[index % len(others)]
        result = await router.engines.probe_inference(
            url, prefill_url=producer.url, deadline_s=deadline
        )
        if result is None:
            return "no model"
        for name, leg in (
            ("producer " + producer.iid, result.prefill),
            ("consumer", result.decode),
        ):
            if leg.inconclusive:
                return f"{name} pool timeout"
            if leg.failed is not None and (name != "consumer" or leg.failed == LEG_CONNECTION):
                return f"{name} {leg.failed}"
        return "sent"

    answers = await asyncio.gather(
        *(probe(index, inst.iid, inst.url) for index, inst in enumerate(consumers)),
        return_exceptions=True,
    )
    outcomes = {
        inst.iid: answer if isinstance(answer, str) else f"error {type(answer).__name__}"
        for inst, answer in zip(consumers, answers, strict=True)
    }
    log.info(
        "peer release for %s: %s",
        ", ".join(gone),
        ", ".join(f"{iid} {outcome}" for iid, outcome in outcomes.items()) or "no live consumer",
    )
    return outcomes
