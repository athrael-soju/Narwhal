from __future__ import annotations

import json
import re
from typing import Any

from ...engines.metrics import EngineMetrics

# The server's metrics are JSON iteration statistics, read by the methods below.
_ABSENT = re.compile(r"(?!)")


def iterations(metrics: str) -> list[dict[str, Any]]:
    """The iteration statistics in one read of the metrics route."""
    try:
        rows = json.loads(metrics)
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _cache(row: dict[str, Any], name: str) -> int | None:
    stats = row.get("kvCacheStats")
    value = stats.get(name) if isinstance(stats, dict) else None
    return value if type(value) is int and value > 0 else None


class TrtllmMetrics(EngineMetrics):
    # Each read returns the iterations since the previous read, and an idle engine none.
    drains = True
    kv_capacity_pattern = _ABSENT
    block_tokens_pattern = _ABSENT

    def kv_capacity(self, metrics: str) -> int | None:
        sizes = []
        for row in iterations(metrics):
            blocks, tokens = _cache(row, "maxNumBlocks"), _cache(row, "tokensPerBlock")
            if blocks is not None and tokens is not None:
                sizes.append(blocks * tokens)
        return min(sizes) if sizes else None

    def cache_block_tokens(self, metrics: str) -> int | None:
        sizes = {_cache(row, "tokensPerBlock") for row in iterations(metrics)} - {None}
        return sizes.pop() if len(sizes) == 1 else None

    def prefix_cache_hits(self, metrics: str) -> int | None:
        # The statistics give no prefix-cache hit count that profiling can difference.
        return None

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        return None
