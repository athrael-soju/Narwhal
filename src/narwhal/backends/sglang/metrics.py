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
        "running": "sum by(iid) (sglang:num_running_reqs{@sel})",
        "waiting": "sum by(iid) (sglang:num_queue_reqs{@sel})",
        "kv_usage": "max by(iid) (sglang:token_usage{@sel})",
        "prefix_hit_ratio": (
            "(sum by(iid) (rate(sglang:cached_tokens_total{@sel}[$__rate_interval])) / "
            "sum by(iid) (rate(sglang:prompt_tokens_total{@sel}[$__rate_interval])))"
        ),
        # A decode engine also counts the prompt of each request it decodes.
        "prompt_tokens": (
            "(sum by(iid) (rate(sglang:prompt_tokens_total{@sel}[$__rate_interval])) unless "
            'on(iid) (max by(iid) (narwhal_instance_role{job="narwhal-router",'
            'instance=~"$router",role="decode"}) == 1))'
        ),
        "output_tokens": (
            "sum by(iid) (rate(sglang:generation_tokens_total{@sel}[$__rate_interval]))"
        ),
        "handoff": (
            'sum by(iid) ({__name__=~"sglang:num_prefill_bootstrap_queue_reqs|'
            'sglang:num_decode_prealloc_queue_reqs|sglang:num_decode_transfer_queue_reqs",@sel})'
        ),
    }
    dashboard_text: ClassVar[Mapping[str, str]] = {
        "handoff_title": "KV handoff queue",
        "handoff_description": "Requests waiting for a KV handoff on each engine: bootstrap on "
        "prefill engines, preallocation and transfer on decode engines.",
        "handoff_unit": "short",
        "handoff_color": "#F2CC0C",
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
