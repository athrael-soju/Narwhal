from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

from ...engines.metrics import EngineMetrics

TRANSFER_SERIES = "sglang:per_stage_req_latency_seconds"
# The consumer records a completed KV receive under this stage.
TRANSFER_STAGE = "decode_transferred"


def _gauge(name: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)$", re.MULTILINE)


_KV_CAPACITY = _gauge("sglang:max_total_num_tokens")
_PAGE_SIZE = _gauge("sglang:page_size")
_CACHED_TOKENS = _gauge("sglang:cached_tokens_total")
_TRANSFER = re.compile(
    rf"^{TRANSFER_SERIES}_(count|sum)\{{([^}}]*)\}}\s+([0-9.eE+-]+)$", re.MULTILINE
)


class SglangMetrics(EngineMetrics):
    transfer_series: ClassVar[str | None] = TRANSFER_SERIES
    dashboard_series: ClassVar[Mapping[str, str]] = {
        "running": "sglang:num_running_reqs",
        "waiting": "sglang:num_queue_reqs",
        "kv_usage": "sglang:token_usage",
        "prompt_tokens": "sglang:prompt_tokens_total",
        "generation_tokens": "sglang:generation_tokens_total",
        "prefix_cache_hits": "sglang:cached_tokens_total",
        "bootstrap_queue": "sglang:num_prefill_bootstrap_queue_reqs",
        "transfer_queue": "sglang:num_decode_transfer_queue_reqs",
    }

    def kv_capacity(self, metrics: str) -> int | None:
        values = [int(float(match)) for match in _KV_CAPACITY.findall(metrics)]
        return min(values) if values else None

    def cache_block_tokens(self, metrics: str) -> int | None:
        values = {int(float(match)) for match in _PAGE_SIZE.findall(metrics)}
        return values.pop() if len(values) == 1 and min(values) > 0 else None

    def prefix_cache_hits(self, metrics: str) -> int | None:
        # One series per cache source: device, host and storage hits all skip prefill.
        values = [float(match) for match in _CACHED_TOKENS.findall(metrics)]
        return round(sum(values)) if values else None

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        totals = {"count": 0.0, "sum": 0.0}
        found = False
        for name, labels, value in _TRANSFER.findall(metrics):
            if f'stage="{TRANSFER_STAGE}"' in labels:
                totals[name] += float(value)
                found = True
        return (totals["count"], totals["sum"]) if found else None
