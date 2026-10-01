"""Peer release rounds after an engine ejection or drain.

A vLLM NIXL consumer keeps a producer's KV memory mapped until a consume request
finds that producer idle for longer than the connector's `engine_ttl`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ..engines.validation import can_consume, can_produce
from ..types import LEG_CONNECTION

if TYPE_CHECKING:
    from ..serving.router import NarwhalRouter

log = logging.getLogger("narwhal.peer_release")

# Seconds after the engine leaves placement. The first round follows the launcher's 60 s
# engine_ttl; the last follows vLLM's 3600 s default.
RELEASE_AFTER_S = (65.0, 125.0, 245.0, 485.0, 965.0, 1925.0, 3845.0)
# Seconds before the one retry of a round's missed consumers; vLLM sends lease heartbeats at
# most every 5 s.
RETRY_AFTER_S = 5.0
# Outcomes a retry within the round leaves unchanged.
SETTLED = ("sent", "no producer", "no model")
# Lifecycle states that hold an engine out of placement after its drain.
RELEASED_STATES = ("drained", "deadline_exceeded", "validating", "blocked")


class PeerRelease:
    """Release-round schedule for each engine out of placement."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._rounds: dict[str, tuple[str, float, int]] = {}
        self.tasks: set[asyncio.Task[dict[str, str]]] = set()
        self.missed: set[str] = set()
        self.retry_at = 0.0

    def track(self, released: Mapping[str, str]) -> None:
        """Start an engine's schedule when it leaves placement or changes state."""
        now = self._clock()
        for iid in set(self._rounds) - set(released):
            del self._rounds[iid]
        for iid, reason in released.items():
            current = self._rounds.get(iid)
            if current is None or current[0] != reason:
                self._rounds[iid] = (reason, now, 0)
        if not released:
            self.missed.clear()

    def due(self) -> list[str]:
        """Return engines with a due round and advance their schedules."""
        now = self._clock()
        due = []
        for iid, (reason, since, done) in sorted(self._rounds.items()):
            if done < len(RELEASE_AFTER_S) and now - since >= RELEASE_AFTER_S[done]:
                self._rounds[iid] = (reason, since, done + 1)
                due.append(iid)
        return due

    def finish(self, task: asyncio.Task[dict[str, str]], *, retry: bool) -> None:
        """Keep a round's missed consumers for one retry."""
        if task.cancelled() or task.exception() is not None:
            return
        if retry:
            self.missed = set()
            return
        self.missed = {iid for iid, outcome in task.result().items() if outcome not in SETTLED}
        self.retry_at = self._clock() + RETRY_AFTER_S

    def retry_due(self) -> bool:
        """Return whether missed consumers are due their retry."""
        return bool(self.missed) and self._clock() >= self.retry_at

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
            for iid, (_, since, done) in sorted(self._rounds.items())
        }


def released_engines(router: NarwhalRouter) -> dict[str, str]:
    """Map each engine out of placement to its lifecycle state, or `ejected`."""
    released = dict.fromkeys(router.scheduler.ejected, "ejected")
    for iid, record in router.lifecycle.records.items():
        if record.state in RELEASED_STATES:
            released[iid] = record.state
    return released


def release_peers(router: NarwhalRouter) -> None:
    """Start a due release round, or the retry for the consumers a round missed."""
    release = router.peer_release
    if router.cfg.engine_restart_policy != "individual":
        return
    released = released_engines(router)
    release.track(released)
    if release.tasks:
        return
    gone = release.due()
    only: set[str] | None = None
    if gone:
        release.missed = set()
    elif released and release.retry_due():
        gone, only = sorted(released), release.missed
    else:
        return
    task = asyncio.create_task(release_round(router, gone, only))
    release.tasks.add(task)
    task.add_done_callback(release.tasks.discard)
    task.add_done_callback(functools.partial(release.finish, retry=only is not None))


async def release_round(
    router: NarwhalRouter, gone: list[str], only: set[str] | None = None
) -> dict[str, str]:
    """Send one transfer probe through each live consumer and return each outcome."""
    specs = {spec.iid: spec for spec in router.cfg.engines}
    live = [inst for inst in router.scheduler.live_instances() if inst.iid in specs]
    producers = [inst for inst in live if can_produce(specs[inst.iid])]
    consumers = [inst for inst in live if can_consume(specs[inst.iid])]
    deadline = router.cfg.probe_deadline_s()
    # A retry takes the next producer in each consumer's rotation.
    shift = 0 if only is None else 1

    async def probe(index: int, consumer_iid: str, url: str) -> str:
        others = [inst for inst in producers if inst.iid != consumer_iid]
        if not others:
            return "no producer"
        producer = others[(index + shift) % len(others)]
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

    targets = [
        (index, inst) for index, inst in enumerate(consumers) if only is None or inst.iid in only
    ]
    answers = await asyncio.gather(
        *(probe(index, inst.iid, inst.url) for index, inst in targets),
        return_exceptions=True,
    )
    outcomes = {
        inst.iid: answer if isinstance(answer, str) else f"error {type(answer).__name__}"
        for (_, inst), answer in zip(targets, answers, strict=True)
    }
    summary = ", ".join(f"{iid} {outcome}" for iid, outcome in outcomes.items())
    if not outcomes or "no producer" in outcomes.values():
        log.warning(
            "peer release for %s: %s; recover with a whole-wave restart",
            ", ".join(gone),
            summary or "no live consumer",
        )
    else:
        log.info("peer release for %s: %s", ", ".join(gone), summary)
    return outcomes
