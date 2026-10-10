"""Engine-neutral readings from an engine's metrics text."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar


class EngineMetrics(ABC):
    """Read a backend's Prometheus metrics."""

    # Dashboard quantity -> backend series.
    dashboard_series: ClassVar[Mapping[str, str]] = {}

    @abstractmethod
    def kv_capacity(self, metrics: str) -> int | None:
        """Return the KV capacity in tokens."""

    @abstractmethod
    def cache_block_tokens(self, metrics: str) -> int | None:
        """Return the tokens per cache block when all ranks agree."""

    @abstractmethod
    def prefix_cache_hits(self, metrics: str) -> int | None:
        """Return the cumulative prompt tokens served from the prefix cache."""

    @abstractmethod
    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        """Return the cumulative KV transfer count and seconds."""
