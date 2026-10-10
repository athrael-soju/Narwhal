from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

from ...engines.metrics import EngineMetrics
from ...profiling.probe import engine

_TRANSFER = re.compile(
    r"^vllm:nixl_xfer_time_seconds_(count|sum)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE
)


class VllmMetrics(EngineMetrics):
    dashboard_series: ClassVar[Mapping[str, str]] = {
        "running": "vllm:num_requests_running",
        "waiting": "vllm:num_requests_waiting",
        "kv_usage": "vllm:kv_cache_usage_perc",
        "prompt_tokens": "vllm:prompt_tokens_total",
        "prompt_tokens_by_source": "vllm:prompt_tokens_by_source_total",
        "generation_tokens": "vllm:generation_tokens_total",
        "prefix_cache_hits": "vllm:prefix_cache_hits_total",
        "prefix_cache_queries": "vllm:prefix_cache_queries_total",
        "kv_expired_requests": "vllm:nixl_num_kv_expired_reqs_total",
    }

    def kv_capacity(self, metrics: str) -> int | None:
        return engine.parse_kv_capacity(metrics)

    def cache_block_tokens(self, metrics: str) -> int | None:
        return engine.parse_cache_block_tokens(metrics)

    def prefix_cache_hits(self, metrics: str) -> int | None:
        return engine.parse_prefix_cache_hits(metrics)

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        totals = {"count": 0.0, "sum": 0.0}
        matches = _TRANSFER.findall(metrics)
        for name, value in matches:
            totals[name] += float(value)
        return (totals["count"], totals["sum"]) if matches else None
