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
        return RemovedBlocks(_hashes(item["block_hashes"]), None, item.get("medium"))
    if kind != "BlockStored":
        return None
    parent = item.get("parent_block_hash")
    tokens = item["token_ids"]
    if not isinstance(tokens, list) or any(type(t) is not int for t in tokens):
        raise ValueError("stored-block token IDs must be integers")
    if parent is not None and not isinstance(parent, bytes | int):
        raise ValueError("parent block hash must be bytes or an integer")
    adapter = item.get("lora_id")
    if adapter is not None and not isinstance(adapter, str | int):
        raise ValueError("LoRA ID must be a string or an integer")
    hashes = _hashes(item["block_hashes"])
    salt = item.get("cache_salt")
    if salt is not None and not isinstance(salt, str):
        raise ValueError("cache salt must be a string")
    named = () if adapter is None else (str(adapter),)
    # SGLang reports the salt beside the blocks; it keys the first block of a root only.
    extras = tuple(
        named + ((salt,) if salt is not None and index == 0 and parent is None else ())
        for index in range(len(hashes))
    )
    return StoredBlocks(
        hashes,
        parent,
        tuple(tokens),
        int(item["block_size"]),
        None if adapter is None else str(adapter),
        extras if any(extras) else None,
        medium=item.get("medium"),
    )


class SglangKvEvents(KvEventDecoder):
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]:
        try:
            batch = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            # SGLang appends the data-parallel rank: [timestamp, events, dp_rank].
            if not isinstance(batch, list) or len(batch) < 2 or not isinstance(batch[1], list):
                raise ValueError("event batch must be [timestamp, events, dp_rank]")
            return [_event(item) for item in batch[1]]
        except (msgpack.UnpackException, KeyError, TypeError) as exc:
            raise ValueError(f"malformed cache event batch: {exc}") from exc
