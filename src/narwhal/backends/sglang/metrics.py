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
_RUNNING = _gauge("sglang:num_running_reqs")
# The scheduler's running and waiting requests, requests waiting for a grammar, and the
# disaggregation queues that hold a request before and during its KV transfer. The decode host
# receive queue is a subset of the decode transfer queue.
_HELD = [
    re.compile(rf"^{re.escape(name)}(\{{[^}}]*\}})?\s+([0-9.eE+-]+)$", re.MULTILINE)
    for name in (
        "sglang:num_running_reqs",
        "sglang:num_queue_reqs",
        "sglang:num_grammar_queue_reqs",
        "sglang:num_prefill_bootstrap_queue_reqs",
        "sglang:num_prefill_inflight_queue_reqs",
        "sglang:num_decode_prealloc_queue_reqs",
        "sglang:num_decode_transfer_queue_reqs",
    )
]
# With priority scheduling, the engine also splits each queue by priority under this label.
_PRIORITY = re.compile(r'\bpriority="[^"]+"')
# Health checks finish without counting here.
_FINISHED = _gauge("sglang:num_requests_total")
_TRANSFER = re.compile(
    rf"^{TRANSFER_SERIES}_(count|sum)\{{([^}}]*)\}}\s+([0-9.eE+-]+)$", re.MULTILINE
)


class SglangMetrics(EngineMetrics):
    transfer_series: ClassVar[str | None] = TRANSFER_SERIES
    kv_capacity_pattern = _KV_CAPACITY
    block_tokens_pattern = _PAGE_SIZE
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
        # A counter that moves whenever the engine computes prompt tokens or generates output.
        "processed_tokens": (
            'sum by(iid) ({__name__=~"sglang:prompt_tokens_total|sglang:generation_tokens_total",'
            "@sel})"
        ),
    }

    def held_requests(self, metrics: str) -> int | None:
        # The engine creates these series at its first scheduler report; before it, it holds none.
        return round(
            sum(
                float(value)
                for pattern in _HELD
                for labels, value in pattern.findall(metrics)
                if not _PRIORITY.search(labels)
            )
        )

    def finished_requests(self, metrics: str) -> float | None:
        # One series per streaming mode, created at the first finished request.
        return sum(float(value) for value in _FINISHED.findall(metrics))

    def prefix_cache_hits(self, metrics: str) -> int | None:
        # One series per cache source: device, host and storage hits all skip prefill.
        values = [float(match) for match in _CACHED_TOKENS.findall(metrics)]
        if values:
            return round(sum(values))
        # SGLang creates the hit series at the engine's first cache hit.
        return 0 if _RUNNING.search(metrics) else None

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        totals = {"count": 0.0, "sum": 0.0}
        found = False
        for name, labels, value in _TRANSFER.findall(metrics):
            if f'stage="{TRANSFER_STAGE}"' in labels:
                totals[name] += float(value)
                found = True
        return (totals["count"], totals["sum"]) if found else None
