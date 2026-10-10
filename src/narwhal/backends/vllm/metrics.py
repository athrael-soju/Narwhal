from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

from ...engines.metrics import EngineMetrics

TRANSFER_SERIES = "vllm:nixl_xfer_time_seconds"
_KV_CAPACITY = re.compile(r'kv_cache_size_tokens="([0-9]+(?:\.[0-9]+)?)"')
_BLOCK_TOKENS = re.compile(r'^vllm:cache_config_info\{[^}]*\bblock_size="([0-9]+)"', re.MULTILINE)
# vLLM counts prompt tokens served from its prefix cache for new requests only.
_PREFIX_CACHE_HITS = re.compile(
    r"^vllm:prefix_cache_hits_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE
)
_TRANSFER = re.compile(
    rf"^{TRANSFER_SERIES}_(count|sum)(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)$", re.MULTILINE
)


class VllmMetrics(EngineMetrics):
    transfer_series: ClassVar[str | None] = TRANSFER_SERIES
    dashboard_series: ClassVar[Mapping[str, str]] = {
        "running": "sum by(iid) (vllm:num_requests_running{@sel})",
        "waiting": "sum by(iid) (vllm:num_requests_waiting{@sel})",
        "kv_usage": "max by(iid) (vllm:kv_cache_usage_perc{@sel})",
        "prefix_hit_ratio": (
            "sum by(iid) (rate(vllm:prefix_cache_hits_total{@sel}[$__rate_interval])) / "
            "sum by(iid) (rate(vllm:prefix_cache_queries_total{@sel}[$__rate_interval]))"
        ),
        # Prompt tokens computed or read from the local cache, not received from a producer.
        "prompt_tokens": (
            "sum by(iid) (rate(vllm:prompt_tokens_by_source_total"
            '{@sel,source=~"local_compute|local_cache_hit"}[$__rate_interval])) or '
            "sum by(iid) (rate(vllm:prompt_tokens_total{@sel}[$__rate_interval]))"
        ),
        "output_tokens": "sum by(iid) (rate(vllm:generation_tokens_total{@sel}[$__rate_interval]))",
        "handoff": (
            "sum by(iid) (rate(vllm:nixl_num_kv_expired_reqs_total{@sel}[$__rate_interval]))"
        ),
    }
    dashboard_text: ClassVar[Mapping[str, str]] = {
        "handoff_title": "Expired KV by producer",
        "handoff_description": "Requests per second whose KV expired on each producer engine, "
        "from the vLLM NIXL connector counter.",
        "handoff_unit": "ops",
        "handoff_color": "#C4162A",
    }

    def kv_capacity(self, metrics: str) -> int | None:
        values = [int(float(match)) for match in _KV_CAPACITY.findall(metrics)]
        return min(values) if values else None

    def cache_block_tokens(self, metrics: str) -> int | None:
        values = {int(match) for match in _BLOCK_TOKENS.findall(metrics)}
        return values.pop() if len(values) == 1 and min(values) > 0 else None

    def prefix_cache_hits(self, metrics: str) -> int | None:
        values = [float(match) for match in _PREFIX_CACHE_HITS.findall(metrics)]
        return round(sum(values)) if values else None

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        totals = {"count": 0.0, "sum": 0.0}
        matches = _TRANSFER.findall(metrics)
        for name, value in matches:
            totals[name] += float(value)
        return (totals["count"], totals["sum"]) if matches else None
