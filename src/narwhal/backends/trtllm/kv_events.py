from __future__ import annotations

from ...engines.kv_events import CacheEvent, KvEventDecoder


class NoKvEvents(KvEventDecoder):
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]:
        # The server serves its cache events only by polling, without sequence numbers or replay.
        raise ValueError("TensorRT-LLM publishes no cache-event stream")
