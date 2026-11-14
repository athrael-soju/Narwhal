from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

from ...engines.metrics import EngineMetrics

_CACHED_TOKENS = re.compile(
    r"^llamacpp:prompt_tokens_cached_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE
)
# llama.cpp publishes neither series, so both readers return None.
_ABSENT = re.compile(r"(?!)")


class LlamacppMetrics(EngineMetrics):
    kv_capacity_pattern = _ABSENT
    block_tokens_pattern = _ABSENT
    dashboard_series: ClassVar[Mapping[str, str]] = {
        "running": "sum by(iid) (llamacpp:requests_processing{@sel})",
        "waiting": "sum by(iid) (llamacpp:requests_deferred{@sel})",
        # prompt_tokens_total counts computed tokens only; cached ones have their own counter.
        "prefix_hit_ratio": (
            "(sum by(iid) (rate(llamacpp:prompt_tokens_cached_total{@sel}[$__rate_interval])) / "
            'sum by(iid) (rate({__name__=~"llamacpp:prompt_tokens_total|'
            'llamacpp:prompt_tokens_cached_total",@sel}[$__rate_interval])))'
        ),
        "prompt_tokens": (
            "sum by(iid) (rate(llamacpp:prompt_tokens_total{@sel}[$__rate_interval]))"
        ),
        "output_tokens": (
            "sum by(iid) (rate(llamacpp:tokens_predicted_total{@sel}[$__rate_interval]))"
        ),
        "processed_tokens": (
            'sum by(iid) ({__name__=~"llamacpp:prompt_tokens_total|'
            'llamacpp:tokens_predicted_total",@sel})'
        ),
    }

    def prefix_cache_hits(self, metrics: str) -> int | None:
        values = [float(match) for match in _CACHED_TOKENS.findall(metrics)]
        return round(sum(values)) if values else None

    def transfer_totals(self, metrics: str) -> tuple[float, float] | None:
        return None
