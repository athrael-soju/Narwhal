from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx

from ...engines.dialect import EngineDialect
from ...engines.metrics import EngineMetrics
from ..fitting import fit_cached_prefill, relative_error
from ..model import Profile
from .engine import completion_body, count_tokens, make_prompt, prefix_cache_hits
from .sweep import Sweep

# Words a primer may add to end past its prefix's last full block.
PRIMER_PAD_WORDS = 8


async def _complete(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    timeout_s: float | None,
) -> tuple[int, float]:
    start = time.monotonic()
    r = await client.post(f"{url}/v1/completions", json=body, timeout=timeout_s or 300.0)
    elapsed = time.monotonic() - start
    if r.status_code != 200:
        raise RuntimeError(
            f"cached prefill probe failed on {url} ({r.status_code}): {r.text[:200]}"
        )
    try:
        response = r.json()
    except ValueError as exc:
        raise RuntimeError(f"cached prefill probe returned no JSON on {url}") from exc
    if (
        not isinstance(response, dict)
        or response.get("error") is not None
        or not isinstance(response.get("choices"), list)
        or not response["choices"]
        or not isinstance(response.get("usage"), dict)
    ):
        raise RuntimeError(f"cached prefill probe returned an invalid completion on {url}")
    usage = response["usage"]
    if type(usage.get("prompt_tokens")) is not int or usage.get("completion_tokens") != 1:
        raise RuntimeError("cached prefill probe lacks exact token usage")
    return usage["prompt_tokens"], elapsed


async def probe_cached_prefill(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    sweep: Sweep,
    dialect: EngineDialect,
    *,
    metrics: EngineMetrics,
    observation_timeout_s: float | None = None,
    max_model_len: int | None = None,
    block_tokens: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    timeout = observation_timeout_s or 30.0
    exact = dialect.tokenize_path is not None
    samples: list[dict[str, Any]] = []
    for prefix_target in sweep.cached_prefix_lens:
        prefix, _ = await make_prompt(client, url, model, prefix_target, dialect, timeout_s=timeout)
        # Boundary state exists only for a prompt that runs past a block boundary.
        primer = prefix + " context"
        if exact and block_tokens is not None:
            boundary = prefix_target // block_tokens * block_tokens
            for _ in range(PRIMER_PAD_WORDS):
                count = await count_tokens(client, url, model, primer, dialect, timeout)
                if count > boundary and count % block_tokens:
                    break
                primer += " context"
            else:
                return samples, (
                    f"the primer for prefix~{prefix_target} ends on a {block_tokens}-token "
                    f"block boundary after {PRIMER_PAD_WORDS} padding words"
                )
        for suffix_target in sweep.cached_suffix_lens:
            full = prefix + " " + "context " * suffix_target
            if max_model_len is not None and exact:
                length = await count_tokens(client, url, model, full, dialect, timeout)
                if length + 1 > max_model_len:
                    print(
                        f"    cached  prefix~{prefix_target:<6} suffix~{suffix_target:<6} "
                        f"skipped: {length} tokens exceed max_model_len {max_model_len}"
                    )
                    continue
            for repeat in range(sweep.cached_repeats):
                salt = dialect.cold_probe_extras()
                await _complete(
                    client,
                    url,
                    completion_body(model, primer, 1, dialect, salt),
                    observation_timeout_s,
                )
                before = await prefix_cache_hits(client, url, metrics, timeout)
                tokens, elapsed = await _complete(
                    client,
                    url,
                    completion_body(model, full, 1, dialect, salt),
                    observation_timeout_s,
                )
                after = await prefix_cache_hits(client, url, metrics, timeout)
                if before is None or after is None:
                    return samples, "the prefix-cache hit counter became unreadable"
                cold_before = after
                cold_tokens, cold_elapsed = await _complete(
                    client,
                    url,
                    completion_body(model, full, 1, dialect, dialect.cold_probe_extras()),
                    observation_timeout_s,
                )
                cold_after = await prefix_cache_hits(client, url, metrics, timeout)
                cached = after - before
                if cached <= 0:
                    return (
                        samples,
                        f"case prefix~{prefix_target} suffix~{suffix_target} "
                        "reused no cached prefix",
                    )
                if cold_after is None:
                    return samples, "the prefix-cache hit counter became unreadable"
                if cold_after != cold_before:
                    raise RuntimeError("a cold control reused a cached prefix; reserve the engine")
                case = {"target_prefix": prefix_target, "target_suffix": suffix_target}
                samples.append(
                    {
                        **case,
                        "repeat": repeat,
                        "state": "warm",
                        "cache_evidence": "prefix_cache_hits",
                        "prefix_tokens": cached,
                        "suffix_tokens": tokens - cached,
                        "seconds": elapsed,
                    }
                )
                samples.append(
                    {
                        **case,
                        "repeat": repeat,
                        "state": "cold",
                        "cache_evidence": "prefix_cache_hits",
                        "prefix_tokens": 0,
                        "suffix_tokens": cold_tokens,
                        "seconds": cold_elapsed,
                    }
                )
            warm = [s["seconds"] for s in samples[-2 * sweep.cached_repeats :: 2]]
            print(
                f"    cached  prefix~{prefix_target:<6} suffix~{suffix_target:<6} "
                f"-> {statistics.median(warm) * 1000:7.1f} ms median"
            )
    return samples, None if samples else "every warm case exceeds the engine context"


def apply_cached_fit(
    profile: Profile, samples: list[dict[str, Any]]
) -> tuple[Profile, dict[str, Any]]:
    cases: dict[tuple[Any, Any], dict[str, list[float]]] = {}
    for sample in samples:
        case = cases.setdefault(
            (sample["target_prefix"], sample["target_suffix"]),
            {"prefix": [], "suffix": [], "warm": [], "cold": [], "cold_tokens": []},
        )
        if sample["state"] == "warm":
            case["prefix"].append(float(sample["prefix_tokens"]))
            case["suffix"].append(float(sample["suffix_tokens"]))
            case["warm"].append(float(sample["seconds"]))
        elif sample["state"] == "cold":
            case["cold"].append(float(sample["seconds"]))
            case["cold_tokens"].append(float(sample["suffix_tokens"]))
        else:
            raise ValueError(f"cached prefill sample state {sample['state']!r} is unknown")
    warm = [
        (
            statistics.median(c["prefix"]),
            statistics.median(c["suffix"]),
            statistics.median(c["warm"]),
        )
        for c in cases.values()
        if c["warm"]
    ]
    (a, b, c, d), groups, cv_mape = fit_cached_prefill(
        warm, profile.ttft_split, profile.ttft_block_tokens
    )
    fitted = replace(
        profile,
        cached_ttft_a=a,
        cached_ttft_b=b,
        cached_ttft_c=c,
        cached_ttft_d=d,
        cached_cv_mape=cv_mape,
        cached_min_prefix_tokens=math.floor(min(p for p, _, _ in warm)),
        cached_max_prefix_tokens=math.ceil(max(p for p, _, _ in warm)),
        cached_min_suffix_tokens=math.floor(min(s for _, s, _ in warm)),
        cached_max_suffix_tokens=math.ceil(max(s for _, s, _ in warm)),
    )

    def error(predict: Callable[[float, float], float]) -> float:
        return statistics.mean(relative_error(predict(p, s), y) for p, s, y in groups)

    controls = [
        (statistics.median(c["cold_tokens"]), statistics.median(c["cold"]))
        for c in cases.values()
        if c["cold"]
    ]
    return fitted, {
        "fit_points": groups,
        "cv_mape": cv_mape,
        "suffix_on_cold_curve_mape": error(lambda p, s: profile.prefill_time(int(s))),
        "full_prompt_cold_mape": error(lambda p, s: profile.prefill_time(int(p + s))),
        "cold_control_curve_mape": statistics.mean(
            relative_error(profile.prefill_time(int(n)), y) for n, y in controls
        )
        if controls
        else None,
    }
