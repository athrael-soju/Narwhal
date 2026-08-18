"""A time-stepped fleet built from the Arrow paper's cost models (Arrow §3.1, §4.1, §5.4).

A KV transfer is charged whenever the decode instance differs from the prefill
instance.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from .local import LocalScheduler
from .monitor import InstanceMonitor
from .profiler import ProfileStore
from .types import Phase, Request


@dataclass
class TraceEntry:
    """One trace row: arrival time, id and the two lengths.

    `prefix_key` marks requests sharing a prompt head, the identity the
    prefix arms price from; None carries no reuse. `prefix_len` is how many
    tokens the head spans when it is not the whole prompt: same-key requests
    with different tails share this much and no more.
    """

    at: float
    rid: str
    input_len: int
    output_len: int
    prefix_key: int | None = None
    prefix_len: int | None = None


@dataclass
class _Live:
    entry: TraceEntry
    request: Request
    arrived: float
    first_token_at: float | None = None
    tokens: int = 0
    finished_at: float | None = None
    token_times: list[float] = field(default_factory=list)

    @property
    def ttft(self) -> float | None:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.arrived

    @property
    def tpot(self) -> float | None:
        """Arrow §4.3's `sum(t_j) / (m - 1)`."""
        if len(self.token_times) < 2:
            return None
        return (self.token_times[-1] - self.token_times[0]) / (len(self.token_times) - 1)


class Fleet:
    """The simulator: local schedulers stepped on a shared clock (README table)."""

    def __init__(
        self,
        monitor: InstanceMonitor,
        profiles: ProfileStore,
        clock: Callable[[], float],
        kv_transfer_s: float = 0.05,
        dt: float = 0.01,
        batch_tokens: int = 16384,
        chunk_max: int = 2048,
        cache_keys: int = 8,
        degradation: dict[str, float] | None = None,
    ) -> None:
        self.monitor = monitor
        self.profiles = profiles
        self.clock = clock
        self.kv_transfer_s = kv_transfer_s
        self.dt = dt
        # Maps iid to a slowdown multiplier on every iteration the engine
        # runs, unchanged name-by-name where it stays 1: the fleet-recovery
        # finding's engine that is measurably slower than its own profile.
        self.degradation: dict[str, float] = dict(degradation or {})
        self.live: dict[str, _Live] = {}
        self.local: dict[str, LocalScheduler] = {
            iid: LocalScheduler(batch_tokens=batch_tokens, chunk_max=chunk_max)
            for iid in monitor.instances
        }
        self._free_at: dict[str, float] = dict.fromkeys(monitor.instances, 0.0)
        self.awaiting_decode: list[str] = []
        # Engine-side prefix caches: the truth the router's warm map
        # only estimates. A completed prefill materializes its key; admission
        # carrying the same key skips the cached tokens. LRU at `cache_keys`
        # entries: caches are bounded in blocks, and a flood of more keys
        # than one engine holds thrashes whichever arm ignores the bound.
        self.cache: dict[str, OrderedDict[int, int]] = {
            iid: OrderedDict() for iid in monitor.instances
        }
        self.cache_keys = cache_keys

    def admit(self, entry: TraceEntry, iid: str) -> None:
        """A request arrives: track it live and queue its prefill on `iid`."""
        req = Request(
            rid=entry.rid,
            input_len=entry.input_len,
            phase=Phase.PREFILL,
            prefix_key=entry.prefix_key,
            prefix_len=entry.prefix_len,
        )
        cached = 0
        cache = self.cache[iid]
        if entry.prefix_key is not None and entry.prefix_key in cache:
            cached = cache[entry.prefix_key]
            cache.move_to_end(entry.prefix_key)
        self.live[entry.rid] = _Live(entry=entry, request=req, arrived=self.clock())
        self.monitor.dispatched(iid, req)
        self.local[iid].admit_prefill(entry.rid, entry.input_len, cached=cached)

    def dispatch_decode(self, rid: str, iid: str) -> None:
        """Place the decode leg; a crossed handoff waits out the KV transfer."""
        live = self.live[rid]
        live.request.phase = Phase.DECODE
        origin = live.request.prefill_instance
        self.monitor.dispatched(iid, live.request)
        crossed = origin is not None and origin != iid
        ready_at = self.clock() + self.kv_transfer_s if crossed else None
        self.local[iid].admit_decode(rid, ready_at)

    def step(self) -> None:
        """One engine iteration on every instance at the current clock."""
        now = self.clock()
        for iid in self.monitor.instances:
            profile = self.profiles.get(iid)
            if profile is None:
                continue
            ls = self.local[iid]
            ls.release_migrations(now)
            if now < self._free_at[iid]:
                continue

            inst = self.monitor.instances[iid]
            decode_tokens = {rid: r.length for rid, r in inst.decode.items()}
            it = ls.step(profile, decode_tokens)
            if it.seconds <= 0.0 and not it.decoded and not it.prefilled:
                continue
            # A token is stamped when its iteration completes, not when it starts.
            done_at = now + max(it.seconds * self.degradation.get(iid, 1.0), self.dt)
            self._free_at[iid] = done_at

            for rid in it.completed_prefill:
                live = self.live.get(rid)
                if live is None:
                    continue
                live.first_token_at = done_at
                live.tokens = 1
                live.token_times.append(done_at)
                self.monitor.first_token(iid, rid)
                self.awaiting_decode.append(rid)
                # The prefix is real once computed: the engine's cache holds
                # this key from here on, evicting the coldest when full.
                key = live.entry.prefix_key
                if key is not None:
                    shared = live.entry.prefix_len
                    cache = self.cache[iid]
                    cache[key] = (
                        min(shared, live.entry.input_len)
                        if shared is not None
                        else live.entry.input_len
                    )
                    cache.move_to_end(key)
                    while len(cache) > self.cache_keys:
                        cache.popitem(last=False)

            for rid in it.decoded:
                live = self.live.get(rid)
                if live is None:
                    continue
                live.tokens += 1
                live.token_times.append(done_at)
                self.monitor.output_token(iid, rid)
                if live.tokens >= live.entry.output_len:
                    live.finished_at = done_at
                    self.monitor.finished(iid, rid)
                    ls.drop(rid)

    def attainment(self, ttft_slo: float, tpot_slo: float) -> tuple[float, int, int]:
        """§6.1's metric. The denominator is what was offered, not what returned."""
        total = len(self.live)
        if not total:
            return 0.0, 0, 0
        met = sum(
            1
            for live in self.live.values()
            if live.finished_at is not None
            and (live.ttft or 0.0) <= ttft_slo
            and (live.tpot or 0.0) <= tpot_slo
        )
        return met / total, met, total
