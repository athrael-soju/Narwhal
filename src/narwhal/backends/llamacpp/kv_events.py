from __future__ import annotations

from ...engines.kv_events import CacheEvent, KvEventDecoder


class NoKvEvents(KvEventDecoder):
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]:
        raise ValueError("llama.cpp publishes no cache events")
