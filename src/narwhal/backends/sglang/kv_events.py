from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from ...engines.kv_events import MsgpackKvEvents, StoredBlocks, block_hashes


class SglangKvEvents(MsgpackKvEvents):
    # The launcher sets one-token pages. Per-token residency costs the router a lookup and an
    # identity per cached token.
    min_block_tokens = 64
    # SGLang appends the data-parallel rank.
    batch_shape = "[timestamp, events, dp_rank]"

    def stored(
        self, item: dict[str, Any], parent: Hashable | None, tokens: list[int]
    ) -> StoredBlocks:
        adapter = item.get("lora_id")
        if adapter is not None and not isinstance(adapter, str | int):
            raise ValueError("LoRA ID must be a string or an integer")
        hashes = block_hashes(item["block_hashes"])
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
