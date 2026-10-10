from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar


class EngineMetrics(ABC):
    dashboard_series: ClassVar[Mapping[str, str]] = {}
    # Histogram whose _count and _sum transfer_totals reads.
    transfer_series: ClassVar[str | None] = None

    @abstractmethod
    def kv_capacity(self, metrics: str) -> int | None: ...

    @abstractmethod
    def cache_block_tokens(self, metrics: str) -> int | None: ...

    @abstractmethod
    def prefix_cache_hits(self, metrics: str) -> int | None: ...

    @abstractmethod
    def transfer_totals(self, metrics: str) -> tuple[float, float] | None: ...
