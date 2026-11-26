"""Show the role controller at work: long replies, then long prompts, under the live view."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx

from . import ask, lifecycle, watch

PHASE_SECONDS = 30.0
CONCURRENCY = 8
REPLY_TOKENS = 200
PROMPT_TOKENS = 2
# Seconds the live view stays after the load ends, so the last switches show.
SETTLE_S = 4.0
# A refused request returns at once; the pause keeps a worker from spinning on refusals.
RETRY_PAUSE_S = 0.2
# About 450 words: prefill outweighs decode, and an engine on a CPU still answers in seconds.
LONG_PROMPT = "Summarise these notes in one word. " + " ".join(
    [
        "The quarterly report covers revenue, costs, hiring, product launches and regional "
        "performance across every market."
    ]
    * 30
)


@dataclass
class Phase:
    name: str
    max_tokens: int
    # None sends the built-in short prompts.
    prompt: str | None
    requests: int = 0
    failed: int = 0
    started: float | None = None


class Load:
    """Two timed phases of concurrent requests, sent from a background thread."""

    def __init__(
        self,
        url: str,
        model: str,
        extra: dict[str, Any],
        seconds: float,
        concurrency: int,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url, self.model, self.extra = url, model, extra
        self.seconds, self.concurrency = seconds, concurrency
        self.transport, self.clock = transport, clock
        self.phases = [
            Phase("long replies", REPLY_TOKENS, None),
            Phase("long prompts", PROMPT_TOKENS, LONG_PROMPT),
        ]
        self.current: Phase | None = None
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.ended: float | None = None
        self.error: BaseException | None = None

    def note(self) -> str | None:
        """What the live view says about the load now."""
        phase = self.current
        if phase is None or phase.started is None:
            return "load finished" if self.done.is_set() else None
        left = max(0.0, self.seconds - (self.clock() - phase.started))
        number = self.phases.index(phase) + 1
        return (
            f"phase {number} of {len(self.phases)}: {phase.name}, {left:.0f} s left, "
            f"{self.concurrency} in flight"
        )

    def settled(self) -> bool:
        """Whether the load ended long enough ago for its last switches to show."""
        return self.ended is not None and self.clock() - self.ended >= SETTLE_S

    async def _phase(self, phase: Phase) -> None:
        phase.started = self.clock()
        self.current = phase
        deadline = phase.started + self.seconds
        async with httpx.AsyncClient(
            timeout=ask.REQUEST_TIMEOUT_S,
            trust_env=False,
            transport=self.transport,
            limits=httpx.Limits(max_connections=self.concurrency),
        ) as client:

            async def worker(slot: int) -> None:
                sent = slot
                while self.clock() < deadline and not self.cancelled.is_set():
                    text = phase.prompt or ask.PROMPTS[sent % len(ask.PROMPTS)]
                    sent += self.concurrency
                    body = ask._body(self.model, text, phase.max_tokens, self.extra)
                    answer = await ask.ask(client, self.url, body)
                    phase.requests += 1
                    if not answer.ok:
                        phase.failed += 1
                        await asyncio.sleep(RETRY_PAUSE_S)

            await asyncio.gather(*(worker(slot) for slot in range(self.concurrency)))

    def run(self) -> None:
        try:
            for phase in self.phases:
                if self.cancelled.is_set():
                    break
                asyncio.run(self._phase(phase))
        except Exception as exc:  # reported in the result
            self.error = exc
        finally:
            self.current = None
            self.ended = self.clock()
            self.done.set()


def run(
    root: Path,
    *,
    seconds: float = PHASE_SECONDS,
    concurrency: int = CONCURRENCY,
    interval: float = 1.0,
    fetch: watch.Fetch | None = None,
    transport: Any = None,
    out: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Send long replies, then long prompts, to a launched instance; in a terminal, show the
    live view until the load ends. Return what the load sent and the roles it left.
    """
    if not seconds > 0 or concurrency < 1:
        raise ValueError("--phase-seconds and --concurrency must be positive")
    config = lifecycle.instance(root)
    state = lifecycle._read_state(root) if (root / "lifecycle.json").exists() else {}
    if state.get("phase") not in ask.PHASES:
        raise ValueError(
            f"dev demo requires a launched instance; this one is {state.get('phase', 'stopped')}"
        )
    url = config["router_url"]
    model = lifecycle.read(Path(state["run"]) / "fleet.json")["model"]
    extra = lifecycle.read(root / "template.json").get("request", {})
    fetch = fetch or watch.http_fetch
    out = out or sys.stdout
    load = Load(url, model, extra, seconds, concurrency, transport, clock)
    sender = threading.Thread(target=load.run, name="dev-demo-load", daemon=True)
    sender.start()
    interrupted = False
    if out.isatty():
        watch._live(
            url,
            watch._context(root, config, interval),
            interval,
            None,
            fetch,
            out,
            clock,
            sleep,
            stop=load.settled,
            note=load.note,
        )
        interrupted = not load.done.is_set()
    try:
        while sender.is_alive():
            sender.join(0.2)
    except KeyboardInterrupt:
        interrupted = True
    if interrupted:
        load.cancelled.set()
        sender.join()
    if load.error is not None:
        raise load.error
    final = watch.view(None, watch.parse(clock(), *fetch(url)))
    roles = [engine["role"] for engine in final["engines"]]
    return {
        "status": "interrupted" if interrupted else "completed",
        "concurrency": concurrency,
        "phase_seconds": seconds,
        "phases": [
            {"name": phase.name, "requests": phase.requests, "failed": phase.failed}
            for phase in load.phases
        ],
        "requests": sum(phase.requests for phase in load.phases),
        "failed": sum(phase.failed for phase in load.phases),
        "role_switches": final["role_switches"],
        "split": {"prefill": roles.count("prefill"), "decode": roles.count("decode")},
    }
