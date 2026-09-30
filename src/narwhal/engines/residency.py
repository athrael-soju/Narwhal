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
from collections import deque
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
# vLLM KV cache group kinds by what a prefix hit needs from the group:
# every leading block, the trailing attention window, or boundary state only.
FULL_KINDS = frozenset({None, "full_attention", "mla_attention", "sink_full_attention"})
WINDOW_KINDS = frozenset({"sliding_window", "sliding_window_mla"})
BOUNDARY_KINDS = frozenset({"mamba"})


@dataclass
class _Group:
    kind: str | None
    window: int | None = None
    # Backend hash to Narwhal identity; None marks a resident block Narwhal cannot name.
    blocks: dict[Hashable, bytes | None] = field(default_factory=dict)


class ResidencyIndex:
    """Apply one engine process's cache events and serve its named resident blocks."""

    def __init__(
        self,
        model: str,
        tokenizer: str,
        *,
        max_blocks: int = MAX_RESIDENT_BLOCKS,
        max_changes: int = MAX_RETAINED_CHANGES,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.max_blocks = max_blocks
        self._lock = threading.Lock()
        self._groups: dict[int | None, _Group] = {}
        self._named: dict[Hashable, bytes] = {}
        self._changes: deque[dict[str, Any]] = deque(maxlen=max_changes)
        self.known = False
        self.reason = "no cache events observed"
        self.sequence: int | None = None
        self.block_size: int | None = None

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
            self.sequence = sequence
            if events is None or any(event is None for event in events):
                self._lose(f"unreadable cache event in sequence {sequence}")
                return
            change: dict[str, Any] = {"sequence": sequence, "cleared": False, "groups": {}}
            # A partial group's blocks take names from complete groups stored in the same run,
            # which vLLM can list after it.
            deferred: list[StoredBlocks] = []
            for event in events:
                if isinstance(event, StoredBlocks) and not event.complete:
                    deferred.append(event)
                    continue
                if not isinstance(event, StoredBlocks):
                    for partial in deferred if self.known else ():
                        self._store(partial, change)
                    deferred.clear()
                if isinstance(event, CacheCleared):
                    self._reset("cache reset")
                    change = {"sequence": sequence, "cleared": True, "groups": {}}
                elif self.known and isinstance(event, StoredBlocks):
                    self._store(event, change)
                elif self.known and isinstance(event, RemovedBlocks):
                    self._remove(event, change)
            for partial in deferred if self.known else ():
                self._store(partial, change)
            if self.known and sum(len(g.blocks) for g in self._groups.values()) > self.max_blocks:
                self._lose(f"index exceeds {self.max_blocks} resident blocks")
            if self.known:
                self._changes.append(change)

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

    @staticmethod
    def _gpu(medium: str | None) -> bool:
        return medium is None or medium == GPU_MEDIUM

    def _store(self, event: StoredBlocks, change: dict[str, Any]) -> None:
        if not self._gpu(event.medium):
            return
        self.block_size = event.block_size
        group = self._groups.setdefault(event.group, _Group(event.kind, event.sliding_window))
        if event.complete:
            parent = None if event.parent_hash is None else self._named.get(event.parent_hash)
            identities: Sequence[bytes | None] | None = stored_identities(
                event, self.model, self.tokenizer, parent
            )
            if identities is None:
                identities = [None] * len(event.block_hashes)
        else:
            identities = matched_identities(event, self._named)
        stored = change["groups"].setdefault(
            str(event.group),
            {"kind": event.kind, "sliding_window": group.window, "stored": [], "removed": []},
        )
        for block_hash, identity in zip(event.block_hashes, identities, strict=True):
            group.blocks[block_hash] = identity
            if identity is not None:
                self._named[block_hash] = identity
                stored["stored"].append(identity.hex())

    def _remove(self, event: RemovedBlocks, change: dict[str, Any]) -> None:
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
                identity = group.blocks.pop(block_hash, None)
                if identity is not None:
                    removed = change["groups"].setdefault(
                        str(key),
                        {
                            "kind": group.kind,
                            "sliding_window": group.window,
                            "stored": [],
                            "removed": [],
                        },
                    )
                    removed["removed"].append(identity.hex())
            if all(block_hash not in g.blocks for g in self._groups.values()):
                self._named.pop(block_hash, None)

    def snapshot(self) -> dict[str, Any]:
        """Return the named resident blocks with the last applied sequence."""
        with self._lock:
            groups = []
            if self.known:
                for key, group in sorted(self._groups.items(), key=lambda item: str(item[0])):
                    named = [i.hex() for i in group.blocks.values() if i is not None]
                    groups.append(
                        {
                            "group": key,
                            "kind": group.kind,
                            "sliding_window": group.window,
                            "identities": named,
                            "unnamed": len(group.blocks) - len(named),
                        }
                    )
            return {
                "known": self.known,
                "reason": self.reason,
                "sequence": self.sequence,
                "block_size": self.block_size,
                "groups": groups,
            }

    def changes_after(self, sequence: int) -> tuple[int, list[dict[str, Any]]] | None:
        """Return the last applied sequence and the ordered changes after `sequence`.

        None means the caller needs a snapshot.
        """
        with self._lock:
            if not self.known or self.sequence is None or sequence > self.sequence:
                return None
            if sequence == self.sequence:
                return self.sequence, []
            retained = list(self._changes)
            if not retained or retained[0]["sequence"] > sequence + 1:
                return None
            return self.sequence, [c for c in retained if c["sequence"] > sequence]

    def cached_prefix_blocks(self, identities: Sequence[bytes]) -> int:
        """Return how many leading prompt blocks this engine can reuse."""
        with self._lock:
            if not self.known:
                return 0
            groups = [(g.kind, g.window, set(g.blocks.values())) for g in self._groups.values()]
            block_size = self.block_size
        return cached_prefix_blocks(groups, identities, block_size)


def cached_prefix_blocks(
    groups: Iterable[tuple[str | None, int | None, Collection[bytes | None]]],
    identities: Sequence[bytes],
    block_size: int | None,
) -> int:
    """Return how many leading prompt blocks the engine can reuse from every KV cache group.

    Full-attention groups must hold each leading block. Sliding-window groups
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
    best = 0
    for count, identity in enumerate(identities, start=1):
        if any(identity not in blocks for blocks in full):
            break
        if all(identity in blocks for blocks in boundary) and all(
            all(i in blocks for i in identities[max(0, count - needed) : count])
            for blocks, needed in windows
        ):
            best = count
    return best
