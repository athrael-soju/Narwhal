from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from ...engines.kv_events import MsgpackKvEvents, StoredBlocks, block_hashes


class VllmKvEvents(MsgpackKvEvents):
    def removed_group(self, item: dict[str, Any]) -> int | None:
        return item.get("group_idx")

    def stored(
        self, item: dict[str, Any], parent: Hashable | None, tokens: list[int]
    ) -> StoredBlocks:
        extras = item.get("extra_keys")
        if extras is not None and not isinstance(extras, list):
            raise ValueError("extra keys must be a list")
        adapter = item.get("lora_name")
        if adapter is not None and not isinstance(adapter, str):
            raise ValueError("LoRA name must be a string")
        return StoredBlocks(
            block_hashes(item["block_hashes"]),
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
