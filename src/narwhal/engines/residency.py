"""Bounded record of the prefix blocks one engine process holds on its GPU.

The index applies an engine's ordered cache-event batches. It knows the
engine's residency only when it has applied every batch since a known
state: the engine's first batch, or a cache reset. A sequence gap, a first
batch after the engine's start, an unreadable batch or an exceeded bound
makes the residency unknown until the next cache reset. Unknown residency
serves no blocks, so callers price that engine cold.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Collection, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .kv_events import (
    GPU_MEDIUM,
    CacheCleared,
    CacheEvent,
    RemovedBlocks,
    StoredBlocks,
    matched_identities,
    stored_identities,
)

MAX_RESIDENT_BLOCKS = 1_000_000
MAX_RETAINED_CHANGES = 10_000
MAX_RETAINED_CHANGE_BLOCKS = 1_000_000
# vLLM KV cache group kinds by what a prefix hit needs from the group:
# every leading block, the trailing attention window, or boundary state only.
FULL_KINDS = frozenset({None, "full_attention", "mla_attention", "sink_full_attention"})
WINDOW_KINDS = frozenset({"sliding_window", "sliding_window_mla"})
BOUNDARY_KINDS = frozenset({"mamba"})


@dataclass
class _Group:
    kind: str | None
    window: int | None = None
    # Backend hash to its Narwhal identity and resident copies. vLLM can hold two physical
    # copies of one hash and reports each store and eviction. The router rejects vLLM's full
    # cache reports, so every store event is a new copy. None marks an unnamed block.
    blocks: dict[Hashable, list[Any]] = field(default_factory=dict)
    # Resident copies per named identity.
    names: Counter[bytes] = field(default_factory=Counter)

    def add(self, block_hash: Hashable, identity: bytes | None) -> None:
        entry = self.blocks.setdefault(block_hash, [identity, 0])
        if entry[0] is None and identity is not None:
            entry[0] = identity
            self.names[identity] += entry[1]
        entry[1] += 1
        if entry[0] is not None:
            self.names[entry[0]] += 1

    def discard(self, block_hash: Hashable) -> None:
        entry = self.blocks.get(block_hash)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] == 0:
            del self.blocks[block_hash]
        if entry[0] is not None:
            self.names[entry[0]] -= 1
            if self.names[entry[0]] == 0:
                del self.names[entry[0]]


class ResidencyIndex:
    """Apply one engine process's cache events and serve its named resident blocks."""

    def __init__(
        self,
        model: str,
        tokenizer: str,
        *,
        max_blocks: int = MAX_RESIDENT_BLOCKS,
        max_changes: int = MAX_RETAINED_CHANGES,
        max_change_blocks: int = MAX_RETAINED_CHANGE_BLOCKS,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.max_blocks = max_blocks
        self._lock = threading.Lock()
        self._groups: dict[int | None, _Group] = {}
        self._named: dict[Hashable, bytes] = {}
        self._changes: deque[dict[str, Any]] = deque()
        self.max_changes = max_changes
        self.max_change_blocks = max_change_blocks
        self._change_blocks = 0
        # Presence before the current batch of each identity it touched, per group.
        self._touched: dict[int | None, dict[bytes, bool]] = {}
        self.known = False
        self.reason = "no cache events observed"
        self.sequence: int | None = None
        self.block_size: int | None = None
        # A feed replaying history holds a consistent but stale state until it reaches the stream.
        self.current = True

    def set_current(self, current: bool) -> None:
        """Record whether applied batches have reached the engine's live stream."""
        with self._lock:
            self.current = current

    def apply(self, sequence: int, events: Sequence[CacheEvent | None] | None) -> None:
        """Apply the batch numbered `sequence`; None marks a batch that failed to decode."""
        with self._lock:
            if self.sequence is not None and sequence <= self.sequence:
                return
            if self.sequence is None and sequence != 0:
                self._lose(f"history before sequence {sequence} is unavailable")
            elif self.sequence is not None and sequence != self.sequence + 1:
                self._lose(f"sequence gap after {self.sequence}")
            elif self.sequence is None:
                self.known = True
                self.reason = "complete event history"
            elif self.sequence == -1 and self.known:
                self.reason = "complete event history"
            self.sequence = sequence
            if events is None or any(event is None for event in events):
                self._lose(f"unreadable cache event in sequence {sequence}")
                return
            cleared = False
            self._touched = {}
            # A partial group's blocks take names from complete groups stored in the same run,
            # which vLLM can list after it.
            deferred: list[StoredBlocks] = []
            for event in events:
                if isinstance(event, StoredBlocks) and not event.complete:
                    deferred.append(event)
                    continue
                if not isinstance(event, StoredBlocks):
                    for partial in deferred if self.known else ():
                        self._store(partial)
                    deferred.clear()
                if isinstance(event, CacheCleared):
                    self._reset("cache reset")
                    cleared = True
                    self._touched = {}
                elif self.known and isinstance(event, StoredBlocks):
                    self._store(event)
                elif self.known and isinstance(event, RemovedBlocks):
                    self._remove(event)
            for partial in deferred if self.known else ():
                self._store(partial)
            if self.known and sum(len(g.blocks) for g in self._groups.values()) > self.max_blocks:
                self._lose(f"index exceeds {self.max_blocks} resident blocks")
            if self.known:
                self._record(sequence, cleared)

    def _record(self, sequence: int, cleared: bool) -> None:
        """Retain the batch's net change per group, which followers apply in any order."""
        groups: dict[str, dict[str, Any]] = {}
        size = 0
        for key, touched in self._touched.items():
            group = self._groups.get(key)
            names = group.names if group is not None else Counter()
            stored = [i for i, before in touched.items() if not before and names[i] > 0]
            removed = [i for i, before in touched.items() if before and names[i] == 0]
            size += len(stored) + len(removed)
            groups[str(key)] = {
                "kind": None if group is None else group.kind,
                "sliding_window": None if group is None else group.window,
                "stored": stored,
                "removed": removed,
            }
        self._touched = {}
        self._changes.append(
            {"sequence": sequence, "cleared": cleared, "groups": groups, "size": size}
        )
        self._change_blocks += size
        while len(self._changes) > 1 and (
            len(self._changes) > self.max_changes or self._change_blocks > self.max_change_blocks
        ):
            self._change_blocks -= self._changes.popleft()["size"]

    def mark_empty(self) -> None:
        """Record that the engine has published no batch, so it holds no cached block."""
        with self._lock:
            if self.sequence is None:
                self._reset("no cache events published")
                self.sequence = -1

    def lose(self, reason: str) -> None:
        """Mark residency unknown after a feed failure outside the event stream."""
        with self._lock:
            self._lose(reason)

    def _lose(self, reason: str) -> None:
        self._clear()
        self.known = False
        self.reason = f"{reason}; waiting for a cache reset"

    def _reset(self, reason: str) -> None:
        self._clear()
        self.known = True
        self.reason = reason

    def _clear(self) -> None:
        self._groups.clear()
        self._named.clear()
        self._changes.clear()
        self._change_blocks = 0

    def _touch(self, key: int | None, group: _Group, identity: bytes | None) -> None:
        touched = self._touched.setdefault(key, {})
        if identity is not None and identity not in touched:
            touched[identity] = group.names[identity] > 0

    @staticmethod
    def _gpu(medium: str | None) -> bool:
        return medium is None or medium == GPU_MEDIUM

    def _store(self, event: StoredBlocks) -> None:
        if not self._gpu(event.medium):
            return
        if self.block_size is None:
            self.block_size = event.block_size
        elif event.block_size != self.block_size:
            self._lose(
                f"cache group {event.group} reports block size {event.block_size}, "
                f"other groups {self.block_size}"
            )
            return
        group = self._groups.setdefault(event.group, _Group(event.kind, event.sliding_window))
        self._touched.setdefault(event.group, {})
        if event.complete:
            parent = None if event.parent_hash is None else self._named.get(event.parent_hash)
            identities: Sequence[bytes | None] | None = stored_identities(
                event, self.model, self.tokenizer, parent
            )
            if identities is None:
                identities = [None] * len(event.block_hashes)
        else:
            identities = matched_identities(event, self._named)
        for block_hash, identity in zip(event.block_hashes, identities, strict=True):
            self._touch(event.group, group, identity)
            group.add(block_hash, identity)
            if identity is not None:
                self._named[block_hash] = identity

    def _remove(self, event: RemovedBlocks) -> None:
        if not self._gpu(event.medium):
            return
        groups = (
            list(self._groups.items())
            if event.group is None
            else [(event.group, self._groups[event.group])]
            if event.group in self._groups
            else []
        )
        for block_hash in event.block_hashes:
            for key, group in groups:
                if block_hash in group.blocks:
                    self._touch(key, group, group.blocks[block_hash][0])
                    group.discard(block_hash)
            if all(block_hash not in g.blocks for g in self._groups.values()):
                self._named.pop(block_hash, None)

    def snapshot(self) -> dict[str, Any]:
        """Return the named resident blocks with the last applied sequence."""
        with self._lock:
            groups = []
            if self.known:
                for key, group in sorted(self._groups.items(), key=lambda item: str(item[0])):
                    groups.append(
                        {
                            "group": key,
                            "kind": group.kind,
                            "sliding_window": group.window,
                            "identities": [i.hex() for i in group.names],
                            "unnamed": sum(
                                1 for entry in group.blocks.values() if entry[0] is None
                            ),
                        }
                    )
            if not self.current and self.known:
                return {
                    "known": False,
                    "reason": "replaying buffered history",
                    "sequence": self.sequence,
                    "block_size": self.block_size,
                    "groups": [],
                }
            return {
                "known": self.known,
                "reason": self.reason,
                "sequence": self.sequence,
                "block_size": self.block_size,
                "groups": groups,
            }

    def changes_after(self, sequence: int) -> tuple[int, list[dict[str, Any]]] | None:
        """Return the last applied sequence and the ordered batch changes after `sequence`.

        Each change lists a group's identities that the batch made resident or
        evicted. None means the caller needs a snapshot.
        """
        with self._lock:
            if not self.known or not self.current or self.sequence is None:
                return None
            if sequence > self.sequence:
                return None
            if sequence == self.sequence:
                return self.sequence, []
            retained = list(self._changes)
            if not retained or retained[0]["sequence"] > sequence + 1:
                return None
            return self.sequence, [
                {
                    "sequence": c["sequence"],
                    "cleared": c["cleared"],
                    "groups": {
                        key: {
                            **delta,
                            "stored": [i.hex() for i in delta["stored"]],
                            "removed": [i.hex() for i in delta["removed"]],
                        }
                        for key, delta in c["groups"].items()
                    },
                }
                for c in retained
                if c["sequence"] > sequence
            ]

    def cached_prefix_blocks(self, identities: Sequence[bytes]) -> int:
        """Return how many leading prompt blocks this engine can reuse."""
        with self._lock:
            if not self.known or not self.current:
                return 0
            groups = [(g.kind, g.window, set(g.names)) for g in self._groups.values()]
            block_size = self.block_size
        return cached_prefix_blocks(groups, identities, block_size)


def cached_prefix_blocks(
    groups: Iterable[tuple[str | None, int | None, Collection[bytes | None]]],
    identities: Sequence[bytes],
    block_size: int | None,
) -> int:
    """Return how many leading prompt blocks the engine can reuse from every KV cache group.

    Pass the identities of the prompt without its final token, which vLLM always
    computes. Full-attention groups must hold each leading block. Sliding-window groups
    must hold the contiguous blocks covering the window before the prefix end.
    Boundary groups, such as Mamba state, must hold the block at the prefix
    end. A group of any other kind, or without a known window, prices cold.
    """
    groups = list(groups)
    if not groups or any(
        kind not in FULL_KINDS | WINDOW_KINDS | BOUNDARY_KINDS
        or (kind in WINDOW_KINDS and (not window or not block_size))
        for kind, window, _ in groups
    ):
        return 0
    full = [blocks for kind, _, blocks in groups if kind in FULL_KINDS]
    windows = [
        (blocks, -(-(window - 1) // block_size))
        for kind, window, blocks in groups
        if kind in WINDOW_KINDS and window and block_size
    ]
    boundary = [blocks for kind, _, blocks in groups if kind in BOUNDARY_KINDS]
    # Leading blocks every full-attention group holds.
    limit = len(identities)
    for blocks in full:
        count = 0
        while count < limit and identities[count] in blocks:
            count += 1
        limit = count
    if not windows:
        for count in range(limit, 0, -1):
            if all(identities[count - 1] in blocks for blocks in boundary):
                return count
        return 0
    # Consecutive resident blocks ending at the current block, per window group.
    runs = [0] * len(windows)
    best = 0
    for count, identity in enumerate(identities[:limit], start=1):
        for index, (blocks, _) in enumerate(windows):
            runs[index] = runs[index] + 1 if identity in blocks else 0
        if all(identity in blocks for blocks in boundary) and all(
            run >= min(count, needed) for run, (_, needed) in zip(runs, windows, strict=True)
        ):
            best = count
    return best
