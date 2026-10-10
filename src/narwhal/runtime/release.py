from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ..engines.validation import can_consume, can_produce
from ..types import LEG_CONNECTION
from .fabric import FabricLifecycle

if TYPE_CHECKING:
    from ..serving.router.routing import NarwhalRouter

log = logging.getLogger("narwhal.peer_release")

# Outcomes a retry within the round leaves unchanged.
SETTLED = ("sent", "no producer", "no model")
# Lifecycle states that hold an engine out of placement after its drain.
RELEASED_STATES = ("drained", "deadline_exceeded", "validating", "blocked")


class PeerRelease:
    def __init__(self, clock: Callable[[], float], fabric: FabricLifecycle) -> None:
        self.fabric = fabric
        self._clock = clock
        self._rounds: dict[str, tuple[str, float, int]] = {}
        self.tasks: set[asyncio.Task[dict[str, str]]] = set()
        self.missed: set[str] = set()
        self.retry_at = 0.0

    def track(self, released: Mapping[str, str]) -> None:
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
        now = self._clock()
        due = []
        schedule = self.fabric.release_after_s
        for iid, (reason, since, done) in sorted(self._rounds.items()):
            if done < len(schedule) and now - since >= schedule[done]:
                self._rounds[iid] = (reason, since, done + 1)
                due.append(iid)
        return due

    def finish(self, task: asyncio.Task[dict[str, str]], *, retry: bool) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        if retry:
            self.missed = set()
            return
        self.missed = {iid for iid, outcome in task.result().items() if outcome not in SETTLED}
        self.retry_at = self._clock() + self.fabric.release_retry_s

    def retry_due(self) -> bool:
        return bool(self.missed) and self._clock() >= self.retry_at

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        now = self._clock()
        schedule = self.fabric.release_after_s
        return {
            iid: {
                "rounds": done,
                "next_round_s": (
                    round(max(0.0, since + schedule[done] - now), 3)
                    if done < len(schedule)
                    else None
                ),
            }
            for iid, (_, since, done) in sorted(self._rounds.items())
        }


def released_engines(router: NarwhalRouter) -> dict[str, str]:
    released = dict.fromkeys(router.scheduler.ejected, "ejected")
    for iid, record in router.lifecycle.records.items():
        if record.state in RELEASED_STATES:
            released[iid] = record.state
    return released


def release_peers(router: NarwhalRouter) -> None:
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
            url,
            prefill_url=producer.url,
            deadline_s=deadline,
            producer=router.launches.get(producer.iid),
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
