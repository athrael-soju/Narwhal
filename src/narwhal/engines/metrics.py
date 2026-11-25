from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar


class EngineMetrics(ABC):
    # The engine route whose text the methods below read.
    path: ClassVar[str] = "/metrics"
    dashboard_series: ClassVar[Mapping[str, str]] = {}
    transfer_series: ClassVar[str | None] = None
    # Each match's first group is one series value: the KV cache size in tokens, and the
    # tokens per cache block.
    kv_capacity_pattern: ClassVar[re.Pattern[str]]
    block_tokens_pattern: ClassVar[re.Pattern[str]]

    def kv_capacity(self, metrics: str) -> int | None:
        values = [int(float(match)) for match in self.kv_capacity_pattern.findall(metrics)]
        return min(values) if values else None

    def cache_block_tokens(self, metrics: str) -> int | None:
        values = {int(float(match)) for match in self.block_tokens_pattern.findall(metrics)}
        return values.pop() if len(values) == 1 and min(values) > 0 else None

    def held_requests(self, metrics: str) -> int | None:
        """Requests the engine runs or queues now, or None when its metrics do not report them."""
        return None

    def finished_requests(self, metrics: str) -> float | None:
        """Client requests the engine has finished since it started, or None when unreported."""
        return None

    @abstractmethod
    def prefix_cache_hits(self, metrics: str) -> int | None: ...

    @abstractmethod
    def transfer_totals(self, metrics: str) -> tuple[float, float] | None: ...
