from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar


class EngineMetrics(ABC):
    # PromQL per dashboard query, one series per iid; "@sel" takes the panel's label selector.
    # The dashboard leaves out a panel that queries a key this mapping lacks.
    dashboard_series: ClassVar[Mapping[str, str]] = {}
    # Panel text that differs by backend.
    dashboard_text: ClassVar[Mapping[str, str]] = {}
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
