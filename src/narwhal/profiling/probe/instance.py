from __future__ import annotations

import math
from typing import Any

import httpx

from ...engines.dialect import EngineDialect, VllmDialect
from ...engines.metrics import EngineMetrics
from ..fitting import (
    CACHED_FIT_MIN_CASES,
    cached_fit_possible,
    decode_cross_validation_mape,
    decode_mape,
    fit_decode_plane,
    fit_prefill_samples,
)
from ..model import Profile
from .decode import probe_decode
from .engine import cache_block_tokens, kv_capacity, prefix_cache_hits
from .prefill import probe_prefill
from .sweep import Sweep, warm_cases
from .warm import apply_cached_fit, probe_cached_prefill


def prefill_fields(
    coefficients: tuple[float, float, float, float | None], block_tokens: int | None
) -> dict[str, Any]:
    a, b, c, split = coefficients
    return {
        "ttft_a": a,
        "ttft_b": b,
        "ttft_c": c,
        "ttft_block_tokens": None if split is None else block_tokens,
        "ttft_split": split,
    }


async def _require_cold(
    client: httpx.AsyncClient,
    iid: str,
    url: str,
    metrics: EngineMetrics,
    before: int | None,
    timeout_s: float | None,
    evidence: dict[str, object] | None,
) -> None:
    after = await prefix_cache_hits(client, url, metrics, timeout_s or 30.0)
    hit_tokens = None if before is None or after is None or after < before else after - before
    if evidence is not None:
        evidence["prefix_cache_hit_tokens"] = hit_tokens
    if hit_tokens:
        raise RuntimeError(
            f"{iid} served {hit_tokens} prompt tokens from its prefix cache during cold "
            "profiling; reserve the engine for profiling and repeat the sweep"
        )


async def profile_instance(
    client: httpx.AsyncClient,
    iid: str,
    url: str,
    model: str,
    sweep: Sweep | None = None,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
    *,
    metrics: EngineMetrics,
    evidence: dict[str, object] | None = None,
    max_model_len: int | None = None,
    observation_timeout_s: float | None = None,
) -> Profile:
    s = sweep or Sweep()
    dialect = dialect or VllmDialect()
    print(f"  {iid}")
    hits_before = await prefix_cache_hits(client, url, metrics, observation_timeout_s or 30.0)
    block_tokens = await cache_block_tokens(client, url, metrics, observation_timeout_s or 30.0)
    prefill = await probe_prefill(
        client,
        url,
        model,
        s.prefill_lens,
        s.prefill_repeats,
        dialect,
        chars_per_token,
        max_model_len,
        observation_timeout_s,
    )
    if evidence is not None:
        evidence["prefill"] = prefill
    await _require_cold(client, iid, url, metrics, hits_before, observation_timeout_s, evidence)
    prefill_fit, representatives, prefill_fit_mape = fit_prefill_samples(prefill, block_tokens)
    split = prefill_fit[3]
    print(
        f"    prefill median fit MAPE {prefill_fit_mape:.1%}"
        + (f"; split step {split * 1000:.1f} ms past {block_tokens}" if split is not None else "")
    )
    if evidence is not None:
        evidence.update(
            prefill_fit_points=representatives,
            prefill_fit_mape=prefill_fit_mape,
            prefill_block_tokens=block_tokens,
        )
    decode_intervals: list[dict[str, object]] = []
    decode = await probe_decode(
        client,
        url,
        model,
        s.decode_concurrency,
        s.decode_tokens,
        dialect,
        chars_per_token,
        s.decode_input_lens,
        evidence=decode_intervals,
        max_model_len=max_model_len,
        repeats=s.decode_repeats,
        observation_timeout_s=observation_timeout_s,
    )
    decode_tokens_used = [
        used for row in decode_intervals if isinstance(used := row.get("tokens"), int)
    ]
    if evidence is not None:
        evidence.update(decode=decode, decode_intervals=decode_intervals)
    await _require_cold(client, iid, url, metrics, hits_before, observation_timeout_s, evidence)
    cached: list[dict[str, Any]] = []
    reason: str | None
    cases = warm_cases(s, max_model_len)
    if hits_before is None:
        reason = "engine exports no prefix-cache hit counter"
    elif not cached_fit_possible(cases):
        reason = (
            f"the warm sweep has {len(cases)} cases within the engine context; a warm fit needs "
            f"two prefix lengths, two suffix lengths and {CACHED_FIT_MIN_CASES} cases"
        )
    else:
        cached, reason = await probe_cached_prefill(
            client,
            url,
            model,
            s,
            dialect,
            metrics=metrics,
            observation_timeout_s=observation_timeout_s,
            max_model_len=max_model_len,
            block_tokens=block_tokens,
        )
    slope, request_slope, intercept = fit_decode_plane(decode)
    coefficients = (slope, request_slope, intercept)
    capacity = await kv_capacity(client, url, metrics, observation_timeout_s or 30.0)
    profile = Profile(
        iid=iid,
        **prefill_fields(prefill_fit, block_tokens),
        tpot_slope=slope,
        tpot_intercept=intercept,
        kv_capacity_tokens=capacity,
        tpot_request_slope=request_slope,
        decode_min_requests=max(1, math.ceil(min(row[0] for row in decode))),
        decode_max_requests=max(1, math.floor(max(row[0] for row in decode))),
        decode_min_kv_tokens=max(1, math.ceil(min(row[1] for row in decode))),
        decode_max_kv_tokens=max(1, math.floor(max(row[1] for row in decode))),
        decode_fit_mape=decode_mape(decode, coefficients),
        decode_cv_mape=decode_cross_validation_mape(decode),
        prefill_min_tokens=math.floor(min(row[0] for row in prefill)),
        prefill_max_tokens=math.ceil(max(row[0] for row in prefill)),
        decode_min_output_tokens=1,
        decode_max_output_tokens=max(decode_tokens_used, default=s.decode_tokens),
    )
    try:
        if reason is not None:
            raise ValueError(reason)
        profile, fit = apply_cached_fit(profile, cached)
    except ValueError as exc:
        print(f"    cached prefill kept cold: {exc}")
        if evidence is not None:
            evidence["cached_prefill"] = {"samples": cached, "reason": str(exc)}
        return profile
    if evidence is not None:
        evidence["cached_prefill"] = {"samples": cached, **fit}
    print(
        f"    cached prefill held-out MAPE {fit['cv_mape']:.1%}; "
        f"suffix-on-cold-curve MAPE {fit['suffix_on_cold_curve_mape']:.1%}"
    )
    return profile
