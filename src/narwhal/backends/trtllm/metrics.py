from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

from ...engines.metrics import EngineMetrics

# The server's Prometheus route, served while the launch's configuration sets
# return_perf_metrics. Reads leave its values in place, unlike the JSON /metrics route.
METRICS_PATH = "/prometheus/metrics"


def _gauge(name: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)$", re.MULTILINE)


_MAX_BLOCKS = _gauge("trtllm_kv_cache_max_blocks")
_BLOCK_TOKENS = _gauge("trtllm_kv_cache_tokens_per_block")
_CACHED_TOKENS = _gauge("trtllm_prompt_cached_tokens_total")


class TrtllmMetrics(EngineMetrics):
    path = METRICS_PATH
    kv_capacity_pattern = _MAX_BLOCKS
    block_tokens_pattern = _BLOCK_TOKENS
    dashboard_series: ClassVar[Mapping[str, str]] = {
        "running": "sum by(iid) (trtllm_num_requests_running{@sel})",
        "waiting": "sum by(iid) (trtllm_num_requests_waiting{@sel})",
        "kv_usage": "max by(iid) (trtllm_kv_cache_utilization{@sel})",
        "prefix_hit_ratio": (
            "(sum by(iid) (rate(trtllm_prompt_cached_tokens_total{@sel}[$__rate_interval])) / "
            "sum by(iid) (rate(trtllm_prompt_tokens_total{@sel}[$__rate_interval])))"
        ),
        # A generation engine also counts the prompt of each request it decodes.
        "prompt_tokens": (
            "(sum by(iid) (rate(trtllm_prompt_tokens_total{@sel}[$__rate_interval])) unless "
            'on(iid) (max by(iid) (narwhal_instance_role{job="narwhal-router",'
            'instance=~"$router",role="decode"}) == 1))'
        ),
        "output_tokens": (
            "sum by(iid) (rate(trtllm_generation_tokens_total{@sel}[$__rate_interval]))"
        ),
        # A counter that moves whenever the engine computes prompt tokens or generates output.
        "processed_tokens": (
            'sum by(iid) ({__name__=~"trtllm_prompt_tokens_total|trtllm_generation_tokens_total",'
            "@sel})"
        ),
    }

    def kv_capacity(self, metrics: str) -> int | None:
        tokens = self.cache_block_tokens(metrics)
        blocks = [int(float(match)) for match in _MAX_BLOCKS.findall(metrics)]
        if tokens is None or not blocks or min(blocks) <= 0:
            return None
        return min(blocks) * tokens

    def prefix_cache_hits(self, metrics: str) -> int | None:
        values = [float(match) for match in _CACHED_TOKENS.findall(metrics)]
        return round(sum(values)) if values else None

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        return None
