from __future__ import annotations

from collections.abc import Hashable
from typing import Any

import msgpack  # type: ignore[import-untyped]

from ...engines.kv_events import (
    CacheCleared,
    CacheEvent,
    KvEventDecoder,
    RemovedBlocks,
    StoredBlocks,
)


def _hashes(values: Any) -> tuple[Hashable, ...]:
    if not isinstance(values, list) or not all(isinstance(v, bytes | int) for v in values):
        raise ValueError("block hashes must be a list of bytes or integers")
    return tuple(values)


def _event(item: Any) -> CacheEvent | None:
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
    adapter = item.get("lora_name")
    if adapter is not None and not isinstance(adapter, str):
        raise ValueError("LoRA name must be a string")
    return StoredBlocks(
        _hashes(item["block_hashes"]),
        parent,
        tuple(tokens),
        int(item["block_size"]),
        adapter,
        None if extras is None else tuple(None if k is None else tuple(k) for k in extras),
        item.get("group_idx"),
        item.get("kv_cache_spec_kind"),
        item.get("medium"),
        item.get("kv_cache_spec_sliding_window"),
    )


def decode_batch(payload: bytes) -> list[CacheEvent | None]:
    try:
        batch = msgpack.unpackb(payload, raw=False, strict_map_key=False)
        if not isinstance(batch, list) or len(batch) < 2 or not isinstance(batch[1], list):
            raise ValueError("event batch must be [timestamp, events, ...]")
        return [_event(item) for item in batch[1]]
    except (msgpack.UnpackException, KeyError, TypeError) as exc:
        raise ValueError(f"malformed cache event batch: {exc}") from exc


class VllmKvEvents(KvEventDecoder):
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]:
        return decode_batch(payload)
