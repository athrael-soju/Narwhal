"""vLLM KV cache events expressed as Narwhal block identities.

vLLM reports each newly cached run of full blocks for one KV cache group as
a stored-block event. The event carries the run's token IDs, the backend's
hash for each block it cached, the parent block's backend hash and the extra
hash keys vLLM mixed into each block. Groups that keep only some blocks,
such as Mamba state in `align` mode, omit hashes for the blocks they skip.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

import msgpack  # type: ignore[import-untyped]

from .prefix import CacheNamespace, block_identities

GPU_MEDIUM = "GPU"


@dataclass(frozen=True)
class StoredBlocks:
    """One stored-block event for a KV cache group."""

    block_hashes: tuple[Hashable, ...]
    parent_hash: Hashable | None
    token_ids: tuple[int, ...]
    block_size: int
    adapter: str | None = None
    # One entry per reported block hash; None when vLLM reports no extra keys.
    extra_keys: tuple[tuple[object, ...] | None, ...] | None = None
    group: int | None = None
    kind: str | None = None
    medium: str | None = None
    # Tokens a sliding-window group attends to; None for other groups.
    sliding_window: int | None = None

    @property
    def block_count(self) -> int:
        """Return the number of full blocks the event's token run spans."""
        return len(self.token_ids) // self.block_size

    @property
    def complete(self) -> bool:
        """Return whether the event reports a hash for every block it spans."""
        return (
            self.block_size > 0
            and len(self.token_ids) == self.block_count * self.block_size
            and len(self.block_hashes) == self.block_count > 0
        )


@dataclass(frozen=True)
class RemovedBlocks:
    """Blocks one KV cache group evicted, named by the backend's hashes."""

    block_hashes: tuple[Hashable, ...]
    group: int | None = None
    medium: str | None = None


@dataclass(frozen=True)
class CacheCleared:
    """The engine reset its prefix cache, so no block remains resident."""


CacheEvent = StoredBlocks | RemovedBlocks | CacheCleared


def _hashes(values: Any) -> tuple[Hashable, ...]:
    if not isinstance(values, list) or not all(isinstance(v, bytes | int) for v in values):
        raise ValueError("block hashes must be a list of bytes or integers")
    return tuple(values)


def _event(item: Any) -> CacheEvent | None:
    """Decode one tagged vLLM event map; an unrecognised type decodes to None."""
    if not isinstance(item, dict):
        raise ValueError("cache event must be a map")
    kind = item.get("type")
    if kind == "AllBlocksCleared":
        return CacheCleared()
    if kind == "BlockRemoved":
        return RemovedBlocks(
            _hashes(item["block_hashes"]), item.get("group_idx"), item.get("medium")
        )
    if kind != "BlockStored":
        return None
    parent = item.get("parent_block_hash")
    tokens = item["token_ids"]
    extras = item.get("extra_keys")
    if not isinstance(tokens, list) or any(type(t) is not int for t in tokens):
        raise ValueError("stored-block token IDs must be integers")
    if parent is not None and not isinstance(parent, bytes | int):
        raise ValueError("parent block hash must be bytes or an integer")
    if extras is not None and not isinstance(extras, list):
        raise ValueError("extra keys must be a list")
    return StoredBlocks(
        _hashes(item["block_hashes"]),
        parent,
        tuple(tokens),
        int(item["block_size"]),
        item.get("lora_name"),
        None if extras is None else tuple(None if k is None else tuple(k) for k in extras),
        item.get("group_idx"),
        item.get("kv_cache_spec_kind"),
        item.get("medium"),
        item.get("kv_cache_spec_sliding_window"),
    )


def decode_batch(payload: bytes) -> list[CacheEvent | None]:
    """Decode one msgpack event batch published by vLLM's ZeroMQ publisher.

    Raises ValueError for a payload that does not follow the batch layout.
    """
    try:
        batch = msgpack.unpackb(payload, raw=False, strict_map_key=False)
        if not isinstance(batch, list) or len(batch) < 2 or not isinstance(batch[1], list):
            raise ValueError("event batch must be [timestamp, events, ...]")
        return [_event(item) for item in batch[1]]
    except (msgpack.UnpackException, KeyError, TypeError) as exc:
        raise ValueError(f"malformed cache event batch: {exc}") from exc


def _block_extras(event: StoredBlocks, index: int) -> tuple[object, ...]:
    if event.extra_keys is None:
        return ()
    extras = event.extra_keys[index]
    return () if extras is None else tuple(extras)


def _cache_salt(event: StoredBlocks) -> tuple[bool, str | None]:
    """Read text-only extra keys; return (supported, salt of the first prompt block)."""
    if event.extra_keys is not None and len(event.extra_keys) != len(event.block_hashes):
        return False, None
    adapter = () if event.adapter is None else (event.adapter,)
    salt = None
    for index in range(len(event.block_hashes)):
        extras = _block_extras(event, index)
        if extras[: len(adapter)] != adapter:
            return False, None
        rest = extras[len(adapter) :]
        # vLLM mixes the cache salt into the first prompt block only.
        if index == 0 and event.parent_hash is None and len(rest) == 1:
            if not isinstance(rest[0], str):
                return False, None
            salt = rest[0]
        elif rest:
            # Multimodal and prompt-embedding keys have no Narwhal identity.
            return False, None
    return True, salt


def stored_identities(
    event: StoredBlocks, model: str, tokenizer: str, parent: bytes | None
) -> list[bytes] | None:
    """Return Narwhal identities aligned with a complete event's block hashes.

    `parent` is the identity of the block named by the event's parent hash.
    An event that starts a prompt has no parent. The result is None when the
    event skips blocks, carries unsupported extra keys, or continues from a
    parent whose identity is unknown.
    """
    if not event.complete or (event.parent_hash is not None and parent is None):
        return None
    supported, salt = _cache_salt(event)
    if not supported:
        return None
    namespace = CacheNamespace(model, tokenizer, event.adapter, salt)
    return block_identities(
        namespace,
        event.token_ids,
        event.block_size,
        parent=None if event.parent_hash is None else parent,
    )


def matched_identities(event: StoredBlocks, known: dict[Hashable, bytes]) -> list[bytes | None]:
    """Return identities for an event's hashes from blocks another group reported.

    vLLM gives every group the same hash for a block when their block sizes
    match. Hashes seen only in this event stay unknown.
    """
    return [known.get(block_hash) for block_hash in event.block_hashes]
