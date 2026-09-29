"""Measure and fit per-engine prefill and decode cost curves."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .. import command_results as results
from ..cli_errors import failure
from ..cli_support import add_version_argument
from ..config import FleetConfig
from ..contracts import PROFILES, versioned
from ..engines.dialect import EngineDialect, VllmDialect
from ..engines.dialect import lookup as lookup_dialect
from ..engines.stream import event_choices, event_object, token_ids
from ..provenance import stamp
from ..types import Role
from .fitting import (
    CACHED_FIT_MIN_CASES,
    cached_fit_possible,
    decode_cross_validation_mape,
    decode_mape,
    fit_cached_prefill,
    fit_decode_plane,
    fit_prefill_samples,
)
from .generation import read_generation
from .model import CACHED_PROFILE_FIELDS, Profile, decode_evidence_problems
from .store import ProfileStore

# Candidate lengths are bounded by each live engine's reported context limit.
# 256 and 4096 end on a block boundary for 16- and 512-token blocks, so the fit measures the
# one-step regime twice; the rest end between block boundaries, where served prompts usually end.
PREFILL_LENS = (256, 700, 1300, 2300, 4096, 4300, 8300, 12300, 16300)
DECODE_CONCURRENCY = (1, 4, 16, 48)
DECODE_INPUT_LENS = (512, 4096, 8192)
DECODE_TOKENS = 64
PREFILL_REPEATS = 3
# Cached-prefix sweep: prompts share a salted prefix that an earlier request cached.
CACHED_PREFIX_LENS = (2048, 4096, 8192)
# Suffixes past one cache block, like most served suffixes.
CACHED_SUFFIX_LENS = (700, 1300, 2600)
CACHED_REPEATS = 3
# Words a warm primer may add to end inside the block after its prefix's last full block.
PRIMER_PAD_WORDS = 8
_KV_CAPACITY = re.compile(r'kv_cache_size_tokens="([0-9]+(?:\.[0-9]+)?)"')
_BLOCK_TOKENS = re.compile(r'^vllm:cache_config_info\{[^}]*\bblock_size="([0-9]+)"', re.MULTILINE)
# A warm fit whose held-out error exceeds this leaves the engine priced cold.
MAX_CACHED_CV_MAPE = 0.20
# vLLM counts prompt tokens served from its prefix cache for new requests only.
_PREFIX_CACHE_HITS = re.compile(
    r"^vllm:prefix_cache_hits_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE
)


def parse_kv_capacity(metrics: str) -> int | None:
    """Read vLLM's physical KV token capacity from its info metric."""
    values = [int(float(match)) for match in _KV_CAPACITY.findall(metrics)]
    return min(values) if values else None


async def kv_capacity(client: httpx.AsyncClient, url: str, timeout_s: float = 30.0) -> int | None:
    """Read physical KV capacity when the engine exports it."""
    try:
        response = await client.get(f"{url}/metrics", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return parse_kv_capacity(response.text)


def parse_cache_block_tokens(metrics: str) -> int | None:
    """Read vLLM's cache block size from its info metric."""
    values = {int(match) for match in _BLOCK_TOKENS.findall(metrics)}
    return values.pop() if len(values) == 1 and min(values) > 0 else None


async def cache_block_tokens(
    client: httpx.AsyncClient, url: str, timeout_s: float = 30.0
) -> int | None:
    """Read the engine's cache block size when it exports one."""
    try:
        response = await client.get(f"{url}/metrics", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return parse_cache_block_tokens(response.text)


def parse_prefix_cache_hits(metrics: str) -> int | None:
    """Sum vLLM's prefix-cache hit tokens across its engine label sets."""
    values = [float(match) for match in _PREFIX_CACHE_HITS.findall(metrics)]
    return round(sum(values)) if values else None


async def prefix_cache_hits(
    client: httpx.AsyncClient, url: str, timeout_s: float = 30.0
) -> int | None:
    """Read cumulative prefix-cache hit tokens when the engine exports them."""
    try:
        response = await client.get(f"{url}/metrics", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return parse_prefix_cache_hits(response.text)


async def _tokenize_response(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    dialect: EngineDialect,
    timeout_s: float = 30.0,
) -> dict:
    """Read the engine's tokenization response for `prompt`.

    Profiling fails on a bad response because token count defines both fits' x axes.
    """
    path = dialect.tokenize_path
    if path is None:
        raise RuntimeError(f"the {dialect.name} dialect has no exact-count route")
    try:
        r = await client.post(
            f"{url}{path}",
            json=dialect.tokenize_request(model, {"prompt": prompt}),
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"tokenize probe failed on {url}: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"tokenize probe failed on {url} ({r.status_code}): {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as exc:
        raise RuntimeError(f"tokenize probe returned no count on {url}: {r.text[:200]}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"tokenize probe returned an invalid response on {url}")
    return body


async def _tokenize(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    dialect: EngineDialect,
    timeout_s: float = 30.0,
) -> int:
    """Return the engine's exact token count for `prompt`."""
    body = await _tokenize_response(client, url, model, prompt, dialect, timeout_s)
    count = dialect.tokenize_response(body)
    if count is None:
        raise RuntimeError(f"tokenize probe returned no count on {url}")
    if count < 1:
        raise RuntimeError(f"tokenize probe counted {count} tokens on {url}")
    return count


async def engine_context_limit(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    dialect: EngineDialect,
    timeout_s: float = 30.0,
) -> int:
    """Read the live serving limit from the same tokenizer used for prompt sizing."""
    body = await _tokenize_response(client, url, model, "benchmark ", dialect, timeout_s)
    limit = body.get("max_model_len")
    if type(limit) is not int or limit < 1:
        raise RuntimeError(f"tokenize probe returned no valid max_model_len on {url}")
    return limit


async def make_prompt(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    target: int,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
    timeout_s: float = 30.0,
    prefix: str = "",
    max_input_tokens: int | None = None,
) -> tuple[str, int]:
    """Build a prompt near `target` tokens and return its fitted-axis count.

    Dialects without an exact-count route use the configured character ratio.
    An optional input bound requires exact counts and preserves the supplied
    prefix while shortening the remainder. Sizing fails if it cannot fit.
    """
    word = "benchmark "
    dialect = dialect or VllmDialect()
    if max_input_tokens is not None and max_input_tokens < 1:
        raise ValueError("max_input_tokens must be positive")
    if dialect.tokenize_path is None:
        if max_input_tokens is not None:
            raise RuntimeError("bounded prompt sizing requires an exact-count tokenizer route")
        text = (prefix + word * max(1, target))[: max(1, int(target * chars_per_token))]
        return text, max(1, round(len(text) / chars_per_token))
    text = prefix + word * max(1, target)
    minimum_chars = max(1, len(prefix)) if max_input_tokens is not None else 1
    got = await _tokenize(client, url, model, text, dialect, timeout_s)
    if got != target:
        scaled = max(minimum_chars, int(len(text) * target / got))
        text = text[:scaled]
        got = await _tokenize(client, url, model, text, dialect, timeout_s)
    if max_input_tokens is not None:
        for _ in range(16):
            if got <= max_input_tokens:
                break
            if len(text) <= minimum_chars:
                raise RuntimeError(
                    f"prompt prefix requires {got} tokens, exceeding input bound {max_input_tokens}"
                )
            scaled = max(minimum_chars, int(len(text) * max_input_tokens / got))
            text = text[: min(len(text) - 1, scaled)]
            got = await _tokenize(client, url, model, text, dialect, timeout_s)
        if got > max_input_tokens:
            raise RuntimeError(
                f"prompt still has {got} tokens after 16 sizing attempts "
                f"for input bound {max_input_tokens}"
            )
    return text, got


async def probe_prefill(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    lens: tuple[int, ...] = PREFILL_LENS,
    repeats: int = PREFILL_REPEATS,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
    max_model_len: int | None = None,
    observation_timeout_s: float | None = None,
) -> list[tuple[float, float]]:
    """Measure one-token request latency across the input-length sweep."""
    dialect = dialect or VllmDialect()
    samples: list[tuple[float, float]] = []
    for target in lens:
        prompt, n = await make_prompt(
            client,
            url,
            model,
            target,
            dialect,
            chars_per_token,
            timeout_s=observation_timeout_s or 30.0,
        )
        if max_model_len is not None and n + 1 > max_model_len:
            raise ValueError(
                f"prefill input {n} plus one output token exceeds {url} max_model_len "
                f"{max_model_len}; choose shorter --prefill-lens"
            )
        first = len(samples)
        for _ in range(repeats):
            body = {
                "model": model,
                "prompt": prompt,
                "max_tokens": 1,
                "temperature": 0.0,
                "stream": False,
                **dialect.decode_probe_extras(1),
                **dialect.cold_probe_extras(),
            }
            start = time.monotonic()
            r = await client.post(
                f"{url}/v1/completions", json=body, timeout=observation_timeout_s or 300.0
            )
            elapsed = time.monotonic() - start
            if r.status_code != 200:
                raise RuntimeError(
                    f"prefill probe failed on {url} ({r.status_code}): {r.text[:200]}"
                )
            try:
                result = r.json()
                usage = result["usage"]
                choices = result["choices"]
                valid = (
                    not result.get("error")
                    and isinstance(usage, dict)
                    and type(usage.get("prompt_tokens")) is int
                    and usage["prompt_tokens"] == n
                    and type(usage.get("completion_tokens")) is int
                    and usage["completion_tokens"] == 1
                    and isinstance(choices, list)
                    and len(choices) == 1
                    and choices[0]["finish_reason"] == "length"
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise RuntimeError("prefill probe lacks a complete response with exact token usage")
            samples.append((float(n), elapsed))
        median = statistics.median(row[1] for row in samples[first:])
        print(f"    prefill {n:>6} tok -> {median * 1000:7.1f} ms median")
    return samples


async def _one_decode_stream(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    input_len: int,
    state: dict[str, int],
    samples: list[tuple[float, float, float]],
    tokens: int = DECODE_TOKENS,
    dialect: EngineDialect | None = None,
) -> None:
    """Measure exact-token gaps while the complete cohort is decoding.

    Estimate the resident batch from client observations and exclude intervals
    crossing a cohort arrival or departure.
    """
    dialect = dialect or VllmDialect()
    if not dialect.token_ids:
        raise RuntimeError("decode profiling requires exact output token IDs")
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": tokens,
        "temperature": 0.0,
        "stream": True,
        **dialect.decode_probe_extras(tokens),
        **dialect.cold_probe_extras(),
        "return_token_ids": True,
        "stream_interval": 1,
    }
    mine = 0
    last: float | None = None
    last_epoch = -1
    done = False
    finished = False
    try:
        async with client.stream("POST", f"{url}/v1/completions", json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")
                raise RuntimeError(
                    f"decode probe failed on {url} ({r.status_code}): {detail[:200]}"
                )
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    obj = event_object(line)
                except ValueError as exc:
                    raise RuntimeError("decode probe returned malformed SSE") from exc
                if obj is None or obj.get("error"):
                    raise RuntimeError("decode probe returned an error")
                try:
                    choices = event_choices(obj)
                except ValueError as exc:
                    raise RuntimeError("decode probe returned invalid choices") from exc
                if len(choices) > 1:
                    raise RuntimeError("decode probe returned invalid choices")
                ids = token_ids(choices)
                if ids is None:
                    raise RuntimeError("decode probe SSE event lacks exact token IDs")
                if any(choice.get("finish_reason") is not None for choice in choices):
                    if choices[0]["finish_reason"] != "length":
                        raise RuntimeError("decode probe stopped before its forced token limit")
                    if finished or mine + len(ids) != tokens:
                        raise RuntimeError("decode probe has an invalid terminal token count")
                    finished = True
                if not ids:
                    continue
                if mine + len(ids) > tokens:
                    raise RuntimeError("decode probe exceeded its forced token limit")
                now = time.monotonic()
                if mine == 0:
                    state["resident"] += input_len
                    state["requests"] += 1
                    state["epoch"] += 1
                mine += len(ids)
                state["resident"] += len(ids)
                if (
                    len(ids) == 1
                    and last is not None
                    and last_epoch == state["epoch"]
                    and state["requests"] == state["cohort"]
                    and now > last
                ):
                    samples.append((float(state["requests"]), float(state["resident"]), now - last))
                # vLLM can bundle final token IDs even with stream_interval=1.
                # Keep exact token counts, but discard gaps around that event.
                last = now if len(ids) == 1 else None
                last_epoch = state["epoch"]
        if not done or not finished or mine != tokens:
            raise RuntimeError(
                f"decode probe incomplete: tokens={mine}/{tokens}, done={done}, finished={finished}"
            )
    finally:
        if mine:
            state["resident"] -= input_len + mine
            state["requests"] -= 1
            state["epoch"] += 1


async def probe_decode(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    concurrency: tuple[int, ...] = DECODE_CONCURRENCY,
    tokens: int = DECODE_TOKENS,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
    input_lens: tuple[int, ...] = DECODE_INPUT_LENS,
    *,
    evidence: list[dict[str, object]] | None = None,
    max_model_len: int | None = None,
    repeats: int = 1,
    observation_timeout_s: float | None = None,
) -> list[tuple[float, float, float]]:
    """Measure decode gaps across input-length and concurrency combinations."""
    dialect = dialect or VllmDialect()
    samples: list[tuple[float, float, float]] = []
    for target in input_lens:
        prompt, input_len = await make_prompt(
            client,
            url,
            model,
            target,
            dialect,
            chars_per_token,
            timeout_s=observation_timeout_s or 30.0,
        )
        if max_model_len is not None and input_len + tokens > max_model_len:
            raise ValueError(
                f"decode input {input_len} plus {tokens} output tokens exceeds {url} "
                f"max_model_len {max_model_len}; choose shorter --decode-input-lens"
            )
        for c in concurrency:
            replicates: list[tuple[float, float, float]] = []
            for repeat in range(repeats):
                state = {"resident": 0, "requests": 0, "epoch": 0, "cohort": c}
                observed: list[tuple[float, float, float]] = []
                tasks = [
                    asyncio.create_task(
                        _one_decode_stream(
                            client, url, model, prompt, input_len, state, observed, tokens, dialect
                        )
                    )
                    for _ in range(c)
                ]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                if len(observed) < 2 * c:
                    raise RuntimeError(
                        f"decode probe has insufficient complete-cohort intervals: "
                        f"got {len(observed)}, need {2 * c}, isl={input_len}, c={c}"
                    )
                if evidence is not None:
                    evidence.append(
                        {
                            "input_tokens": input_len,
                            "concurrency": c,
                            "repeat": repeat,
                            "intervals": observed,
                        }
                    )
                replicates.append(
                    (
                        statistics.median(s[0] for s in observed),
                        statistics.median(s[1] for s in observed),
                        # Under colocated load, stalled tokens can arrive in a
                        # burst. Mean gap retains the total service time;
                        # median gap can report only the catch-up burst.
                        statistics.mean(s[2] for s in observed),
                    )
                )
            requests, batch, gap = (
                statistics.median(row[i] for row in replicates) for i in range(3)
            )
            samples.append((requests, batch, gap))
            print(
                f"    decode  isl={input_len:<6} c={c:<3} "
                f"batch~{batch:>7.0f} tok, seq~{requests:>4.0f} "
                f"-> {gap * 1000:6.2f} ms/token"
            )
    return samples


async def _complete(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    timeout_s: float | None,
) -> tuple[int, float]:
    """Send one forced one-token completion and return its prompt tokens and latency."""
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
    observation_timeout_s: float | None = None,
    max_model_len: int | None = None,
    block_tokens: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Measure prefill with a cached prefix and an uncached suffix, plus cold controls.

    Each case caches a primer under a fresh salt, then times prefix plus suffix under
    the same salt. The primer is the prefix and the suffix's first words; with an
    exact-count tokenizer and `block_tokens`, it ends inside the block after the
    prefix's last full block. The engine's hit counter supplies the cached token
    count each sample records. A cold control repeats the prompt under another salt.
    A case longer than `max_model_len` is skipped.
    Returns the samples and, when the sweep stopped early or measured no case, why.
    """
    timeout = observation_timeout_s or 30.0

    def body(prompt: str, extras: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": model,
            "prompt": prompt,
            "max_tokens": 1,
            "temperature": 0.0,
            "stream": False,
            **dialect.decode_probe_extras(1),
            **extras,
        }

    exact = dialect.tokenize_path is not None
    samples: list[dict[str, Any]] = []
    for prefix_target in sweep.cached_prefix_lens:
        prefix, _ = await make_prompt(client, url, model, prefix_target, dialect, timeout_s=timeout)
        # A hybrid engine keeps boundary state only for a prompt that runs past a block
        # boundary, so the primer runs at least one word past the prefix.
        primer = prefix + " context"
        if exact and block_tokens is not None:
            boundary = prefix_target // block_tokens * block_tokens
            for _ in range(PRIMER_PAD_WORDS):
                count = await _tokenize(client, url, model, primer, dialect, timeout)
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
                length = await _tokenize(client, url, model, full, dialect, timeout)
                if length + 1 > max_model_len:
                    print(
                        f"    cached  prefix~{prefix_target:<6} suffix~{suffix_target:<6} "
                        f"skipped: {length} tokens exceed max_model_len {max_model_len}"
                    )
                    continue
            for repeat in range(sweep.cached_repeats):
                salt = dialect.cold_probe_extras()
                await _complete(client, url, body(primer, salt), observation_timeout_s)
                before = await prefix_cache_hits(client, url, timeout)
                tokens, elapsed = await _complete(
                    client, url, body(full, salt), observation_timeout_s
                )
                after = await prefix_cache_hits(client, url, timeout)
                if before is None or after is None:
                    return samples, "the prefix-cache hit counter became unreadable"
                cold_before = after
                cold_tokens, cold_elapsed = await _complete(
                    client, url, body(full, dialect.cold_probe_extras()), observation_timeout_s
                )
                cold_after = await prefix_cache_hits(client, url, timeout)
                cached = after - before
                if cached <= 0:
                    return (
                        samples,
                        f"case prefix~{prefix_target} suffix~{suffix_target} "
                        "reused no cached prefix",
                    )
                if cold_after is None or cold_after != cold_before:
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
    """Fit warm prefill from retained samples and compare it with cold pricing.

    Each target case contributes the medians of its repeats. A fit whose held-out
    error exceeds `MAX_CACHED_CV_MAPE` raises ValueError, leaving the engine cold.
    """
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
    (a, b, c, d), groups, cv_mape = fit_cached_prefill(warm)
    if cv_mape > MAX_CACHED_CV_MAPE:
        raise ValueError(
            f"warm prefill held-out error {cv_mape:.1%} exceeds {MAX_CACHED_CV_MAPE:.0%}"
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
        return statistics.mean(abs(predict(p, s) - y) / max(y, 1e-9) for p, s, y in groups)

    controls = [
        (statistics.median(c["cold_tokens"]), statistics.median(c["cold"]))
        for c in cases.values()
        if c["cold"]
    ]
    return fitted, {
        "fit_points": groups,
        "cv_mape": cv_mape,
        # Pricing only the suffix on the cold curve ignores attention to the cached prefix.
        "suffix_on_cold_curve_mape": error(lambda p, s: profile.prefill_time(int(s))),
        "full_prompt_cold_mape": error(lambda p, s: profile.prefill_time(int(p + s))),
        # The measured cold controls against the cold curve at the same prompt lengths.
        "cold_control_curve_mape": statistics.mean(
            abs(profile.prefill_time(int(n)) - y) / max(y, 1e-9) for n, y in controls
        )
        if controls
        else None,
    }


async def _require_cold(
    client: httpx.AsyncClient,
    iid: str,
    url: str,
    before: int | None,
    timeout_s: float | None,
    evidence: dict[str, object] | None,
) -> None:
    """Fail when the engine served prompt tokens from its prefix cache since `before`.

    A missing counter, or one that went backwards after an engine restart,
    records no count; profile generation checks reject a restarted engine.
    """
    after = await prefix_cache_hits(client, url, timeout_s or 30.0)
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
    evidence: dict[str, object] | None = None,
    max_model_len: int | None = None,
    observation_timeout_s: float | None = None,
) -> Profile:
    """Run both sweeps and fit one engine profile."""
    s = sweep or Sweep()
    dialect = dialect or VllmDialect()
    print(f"  {iid}")
    hits_before = await prefix_cache_hits(client, url, observation_timeout_s or 30.0)
    block_tokens = await cache_block_tokens(client, url, observation_timeout_s or 30.0)
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
    # Check before the decode sweep so cached prefill fails early.
    await _require_cold(client, iid, url, hits_before, observation_timeout_s, evidence)
    (a, b, c, split), representatives, prefill_fit_mape = fit_prefill_samples(prefill, block_tokens)
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
    if evidence is not None:
        evidence.update(decode=decode, decode_intervals=decode_intervals)
    await _require_cold(client, iid, url, hits_before, observation_timeout_s, evidence)
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
            observation_timeout_s=observation_timeout_s,
            max_model_len=max_model_len,
            block_tokens=block_tokens,
        )
    slope, request_slope, intercept = fit_decode_plane(decode)
    coefficients = (slope, request_slope, intercept)
    capacity = await kv_capacity(client, url, observation_timeout_s or 30.0)
    profile = Profile(
        iid=iid,
        ttft_a=a,
        ttft_b=b,
        ttft_c=c,
        ttft_block_tokens=None if split is None else block_tokens,
        ttft_split=split,
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
        decode_max_output_tokens=s.decode_tokens,
    )
    try:
        if reason is not None:
            raise ValueError(reason)
        profile, fit = apply_cached_fit(profile, cached)
    except ValueError as exc:
        # A warm fit that cannot be formed leaves the engine priced cold.
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


@dataclass(frozen=True)
class Sweep:
    """Prompt lengths, concurrency levels, and repetition counts for profiling."""

    prefill_lens: tuple[int, ...] = PREFILL_LENS
    decode_concurrency: tuple[int, ...] = DECODE_CONCURRENCY
    decode_tokens: int = DECODE_TOKENS
    prefill_repeats: int = PREFILL_REPEATS
    decode_input_lens: tuple[int, ...] = DECODE_INPUT_LENS
    decode_repeats: int = 1
    cached_prefix_lens: tuple[int, ...] = CACHED_PREFIX_LENS
    cached_suffix_lens: tuple[int, ...] = CACHED_SUFFIX_LENS
    cached_repeats: int = CACHED_REPEATS


@dataclass(frozen=True)
class ColocatedWorkload:
    """Explicit offered traffic for each neighbour of a profiled engine."""

    prefill_rps: float
    decode_rps: float
    prefill_tokens: int
    decode_input_tokens: int
    decode_output_tokens: int


class NeighbourLoad:
    """Pace direct completion requests on the other engines in one GPU group."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        peers: list[tuple[str, str, Role]],
        model: str,
        dialect: EngineDialect,
        chars_per_token: float,
        workload: ColocatedWorkload,
        observation_timeout_s: float | None = None,
    ) -> None:
        self.client = client
        self.peers = peers
        self.model = model
        self.dialect = dialect
        self.chars_per_token = chars_per_token
        self.workload = workload
        self.observation_timeout_s = observation_timeout_s
        self.tasks: list[asyncio.Task[None]] = []
        self.counts = {iid: 0 for iid, _, _ in peers}
        self.errors: dict[str, str] = {}
        self.started = 0.0
        self.elapsed: float | None = None

    async def start(self) -> None:
        """Tokenize each peer's request shape before starting the timed load."""
        jobs = []
        for index, (iid, url, role) in enumerate(self.peers):
            rate = self.workload.prefill_rps if role is Role.PREFILL else self.workload.decode_rps
            target = (
                self.workload.prefill_tokens
                if role is Role.PREFILL
                else self.workload.decode_input_tokens
            )
            output = 1 if role is Role.PREFILL else self.workload.decode_output_tokens
            prompt, count = await make_prompt(
                self.client,
                url,
                self.model,
                target,
                self.dialect,
                self.chars_per_token,
                timeout_s=self.observation_timeout_s or 30.0,
            )
            limit = await engine_context_limit(
                self.client,
                url,
                self.model,
                self.dialect,
                timeout_s=self.observation_timeout_s or 30.0,
            )
            if count + output > limit:
                raise ValueError(f"neighbour {iid}: {count}+{output} exceeds max_model_len {limit}")
            jobs.append((iid, url, role, rate, prompt, output, index / len(self.peers) / rate))
        self.started = time.monotonic()
        self.tasks = [asyncio.create_task(self._serve(*job)) for job in jobs]
        # Observe a full period before fitting the target. Reset counters so
        # stored rates describe only the same interval as the latency sweep.
        await asyncio.sleep(max(1.0 / job[3] for job in jobs))
        self._collect_task_errors()
        if self.errors:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            raise RuntimeError("neighbour load failed during warmup: " + self._error_detail())
        self.counts = dict.fromkeys(self.counts, 0)
        self.started = time.monotonic()

    async def _serve(
        self, iid: str, url: str, role: Role, rate: float, prompt: str, output: int, phase_s: float
    ) -> None:
        interval = 1.0 / rate
        next_at = self.started + phase_s
        body = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": output,
            "temperature": 0.0,
            "stream": False,
            **self.dialect.decode_probe_extras(output),
        }
        while True:
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            try:
                response = await self.client.post(
                    f"{url}/v1/completions", json={**body, **self.dialect.cold_probe_extras()}
                )
                response.raise_for_status()
                result = response.json()
                if (
                    result.get("error")
                    or result.get("usage", {}).get("completion_tokens") != output
                ):
                    raise RuntimeError("incomplete neighbour completion")
                self.counts[iid] += 1
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                self.errors[iid] = str(exc)
                return
            next_at = max(next_at + interval, time.monotonic())

    def _collect_task_errors(self) -> None:
        for (iid, _, _), task in zip(self.peers, self.tasks, strict=True):
            if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                self.errors[iid] = f"{type(error).__name__}: {error}"

    def _error_detail(self) -> str:
        return "; ".join(f"{iid}: {error}" for iid, error in self.errors.items())

    def evidence(self) -> dict[str, Any]:
        """Retain completed traffic and errors for every configured neighbour."""
        elapsed = self.elapsed or max(time.monotonic() - self.started, 1e-9)
        counts = {
            role: sum(self.counts[iid] for iid, _, peer_role in self.peers if peer_role is role)
            for role in Role
        }
        return {
            "elapsed_s": elapsed,
            "offered_prefill_rps_per_peer": self.workload.prefill_rps,
            "offered_decode_rps_per_peer": self.workload.decode_rps,
            "completed_prefill": counts[Role.PREFILL],
            "completed_decode": counts[Role.DECODE],
            "prefill_rps": counts[Role.PREFILL] / elapsed,
            "decode_rps": counts[Role.DECODE] / elapsed,
            "peers": {
                iid: {
                    "role": role.value,
                    "completed": self.counts[iid],
                    "rps": self.counts[iid] / elapsed,
                    "error": self.errors.get(iid),
                }
                for iid, _, role in self.peers
            },
        }

    async def stop(self) -> dict[str, Any]:
        """Require completed traffic from each peer before retaining its role mix."""
        self.elapsed = max(time.monotonic() - self.started, 1e-9)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self._collect_task_errors()
        for iid, count in self.counts.items():
            if count == 0:
                self.errors.setdefault(iid, "completed 0 requests during measurement")
        if self.errors:
            raise RuntimeError("neighbour load failed: " + self._error_detail())
        return self.evidence()


def warm_cases(sweep: Sweep, max_model_len: int | None = None) -> list[tuple[int, int]]:
    """Return the warm (prefix, suffix) cases whose prompt and output token fit the context."""
    return [
        (p, q)
        for p in sweep.cached_prefix_lens
        for q in sweep.cached_suffix_lens
        if max_model_len is None or p + q + 1 < max_model_len
    ]


def bounded_sweep(sweep: Sweep, max_model_len: int, max_num_seqs: int | None = None) -> Sweep:
    """Keep candidate lengths and cohorts within the serving engine's limits."""
    prefill = tuple(n for n in sweep.prefill_lens if n + 1 < max_model_len)
    decode = tuple(n for n in sweep.decode_input_lens if n + sweep.decode_tokens < max_model_len)
    if len(set(prefill)) < 3 or len(set(decode)) < 2:
        raise ValueError(
            f"max_model_len {max_model_len} leaves too few sweep points; choose shorter "
            "--prefill-lens and --decode-input-lens"
        )
    concurrency = sweep.decode_concurrency
    if max_num_seqs is not None:
        concurrency = tuple(n for n in concurrency if n <= max_num_seqs)
        if max_num_seqs < max(sweep.decode_concurrency) and max_num_seqs not in concurrency:
            concurrency += (max_num_seqs,)
        if len(set(concurrency)) < 2:
            raise ValueError(
                f"max_num_seqs {max_num_seqs} leaves fewer than two decode concurrency "
                "points; adjust the engine launch policy before profiling"
            )
    # A cached case needs its prefix, its suffix and one output token inside the context.
    shortest_suffix = min(sweep.cached_suffix_lens, default=0)
    shortest_prefix = min(sweep.cached_prefix_lens, default=0)
    prefixes = tuple(p for p in sweep.cached_prefix_lens if p + shortest_suffix + 1 < max_model_len)
    suffixes = tuple(s for s in sweep.cached_suffix_lens if shortest_prefix + s + 1 < max_model_len)
    return replace(
        sweep,
        prefill_lens=prefill,
        decode_input_lens=decode,
        decode_concurrency=concurrency,
        cached_prefix_lens=prefixes,
        cached_suffix_lens=suffixes,
    )


def load_sequence_limits(path: Path, engine_ids: set[str]) -> dict[str, int]:
    """Read the generated limits bound to the fleet's serving roles."""
    document = json.loads(path.read_text())
    if (
        not isinstance(document, dict)
        or document.get("schema") != "narwhal.profiling-limits"
        or document.get("schema_version") != 1
        or not isinstance(document.get("engines"), dict)
    ):
        raise ValueError(f"invalid profiling limits: {path}")
    limits = document["engines"]
    if set(limits) != engine_ids or any(
        type(value) is not int or value < 1 for value in limits.values()
    ):
        raise ValueError(
            f"profiling limits must name every fleet engine with a positive limit: {path}"
        )
    return limits


def refit_saved_prefill(samples_path: Path, output_path: Path, engine_ids: set[str]) -> int:
    """Rebuild TTFT coefficients from retained repeats while keeping measured decode fits."""
    sidecar_path = output_path.with_suffix(".samples.json")
    for path in (output_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"output exists: {path}; choose a fresh profile path")
    record = json.loads(samples_path.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("engines"), dict):
        raise ValueError(f"invalid profile samples: {samples_path}")
    rows = record["engines"]
    if set(rows) != engine_ids:
        raise ValueError("saved profile samples must cover every configured engine")
    profiles = []
    for iid in sorted(engine_ids):
        row = rows[iid]
        if not isinstance(row, dict) or not isinstance(row.get("prefill"), list):
            raise ValueError(f"{iid}: saved prefill samples are missing")
        if any(
            not isinstance(point, list)
            or len(point) != 2
            or any(type(value) not in (int, float) for value in point)
            for point in row["prefill"]
        ):
            raise ValueError(f"{iid}: saved prefill samples are invalid")
        try:
            samples = [tuple(point) for point in row["prefill"]]
            old = Profile(**row["profile"])
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError(f"{iid}: saved profile evidence is invalid") from exc
        if old.iid != iid:
            raise ValueError(f"{iid}: saved profile identity differs from the fleet")
        if old.generation_digest is None or not isinstance(row.get("generation_evidence"), dict):
            raise ValueError(f"{iid}: saved samples lack generation evidence; reprofile the engine")
        block_tokens = row.get("prefill_block_tokens")
        if block_tokens is not None and (type(block_tokens) is not int or block_tokens < 1):
            raise ValueError(f"{iid}: saved prefill block size is invalid")
        (a, b, c, split), representatives, error = fit_prefill_samples(samples, block_tokens)
        updated = Profile(
            **{
                **asdict(old),
                "ttft_a": a,
                "ttft_b": b,
                "ttft_c": c,
                "ttft_block_tokens": None if split is None else block_tokens,
                "ttft_split": split,
                **dict.fromkeys(CACHED_PROFILE_FIELDS),
            }
        )
        cached = row.get("cached_prefill") or {}
        if cached.get("reason") is not None and cached.get("cv_mape") is None:
            # The live run kept this engine cold; its samples may stop partway through the sweep.
            row["cached_prefill"] = {
                "samples": cached.get("samples", []),
                "reason": cached["reason"],
            }
        elif cached.get("samples"):
            try:
                refit, fit = apply_cached_fit(updated, cached["samples"])
            except (KeyError, TypeError) as exc:
                raise ValueError(f"{iid}: saved cached prefill samples are invalid") from exc
            except ValueError as exc:
                # The live run kept this engine cold for the same reason.
                row["cached_prefill"] = {"samples": cached["samples"], "reason": str(exc)}
            else:
                updated = refit
                row["cached_prefill"] = {"samples": cached["samples"], **fit}
        row.update(
            prefill_fit_points=representatives,
            prefill_fit_mape=error,
            profile=asdict(updated),
        )
        print(f"  {iid}: prefill median fit MAPE {error:.1%}")
        profiles.append(asdict(updated))
    record["method_version"] = 2
    record["prefill_refit_source"] = str(samples_path)
    profile_document = versioned(PROFILES, {"meta": stamp()["meta"], "profiles": profiles})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for path, document in ((output_path, profile_document), (sidecar_path, record)):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(document, indent=2) + "\n")
    print(f"refitted {len(profiles)} profile(s) to {output_path}")
    return 0


def _evidence_matches(saved: object, row: Profile) -> bool:
    """Return whether saved profile evidence describes `row`; absent optional fields are unset."""
    if not isinstance(saved, dict):
        return False
    try:
        return Profile(**saved) == row
    except (TypeError, ValueError):
        return False


def merge_profiles(sources: list[Path], output_path: Path, engine_ids: set[str]) -> int:
    """Combine separately measured role mixes, retaining source sidecars."""
    sidecar_path = output_path.with_suffix(".samples.json")
    for path in (output_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"output exists: {path}; choose a fresh profile path")
    store = ProfileStore(output_path, load=False)
    records = []
    profiles: list[Profile] = []
    seen: set[tuple[str, str | None, int | None, int | None, str | None]] = set()
    for source in sources:
        source_store = ProfileStore(source)
        samples = source.with_suffix(".samples.json")
        if not samples.is_file():
            raise FileNotFoundError(f"missing source measurement sidecar: {samples}")
        record = json.loads(samples.read_text())
        if not isinstance(record, dict) or not isinstance(record.get("engines"), dict):
            raise ValueError(f"invalid source measurement sidecar: {samples}")
        rows = source_store.all_profiles()
        if not rows:
            raise ValueError(f"empty source profiles: {source}")
        for row in rows:
            if row.iid not in engine_ids:
                raise ValueError(f"{source}: profile {row.iid} is outside the fleet")
            evidence = record["engines"].get(row.iid)
            if not isinstance(evidence, dict) or not _evidence_matches(
                evidence.get("profile"), row
            ):
                raise ValueError(
                    f"{samples}: profile {row.iid} lacks matching measurement evidence"
                )
            key = (
                row.iid,
                row.colocated_group,
                row.colocated_prefill_engines,
                row.colocated_decode_engines,
                row.colocated_target_role,
            )
            if key in seen:
                raise ValueError(f"duplicate measured profile variant: {key}")
            seen.add(key)
            profiles.append(row)
        records.append(
            {
                "profiles": str(source),
                "profiles_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "samples": str(samples),
                "samples_sha256": hashlib.sha256(samples.read_bytes()).hexdigest(),
            }
        )
    missing = engine_ids - {row.iid for row in profiles}
    if missing:
        raise ValueError(f"merged profiles miss fleet engines: {', '.join(sorted(missing))}")
    for row in profiles:
        store.put(row)
    sidecar_path.write_text(json.dumps({"method_version": 3, "sources": records}, indent=2) + "\n")
    print(f"merged {len(seen)} measured profiles to {output_path}")
    return 0


async def run(
    cfg: FleetConfig,
    only: set[str] | None,
    sweep: Sweep | None = None,
    *,
    overwrite: bool = False,
    limits_path: Path | None = None,
    colocated_workload: ColocatedWorkload | None = None,
    observation_timeout_s: float | None = None,
) -> int:
    """Profile selected healthy engines and write the store."""
    if observation_timeout_s is not None and (
        not math.isfinite(observation_timeout_s) or observation_timeout_s <= 0
    ):
        raise ValueError("profile observation timeout must be finite and positive")
    store = ProfileStore(cfg.profiles_path, load=False)
    evidence_path = store.path.with_suffix(".samples.json")
    if store.path == evidence_path or (
        store.path.exists() and evidence_path.exists() and store.path.samefile(evidence_path)
    ):
        raise ValueError("profile store and sample sidecar must have distinct paths")
    for path in (store.path, evidence_path):
        if path.is_symlink():
            raise ValueError(f"refusing symlink output: {path}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"output exists: {path}; use a new path or --overwrite")
    targets = [e for e in cfg.engines if not only or e.iid in only]
    if not targets:
        results.record_error(
            "engine_selection_empty", "Selection matched zero engines", field="only"
        )
        print("no matching instances", file=sys.stderr)
        return 2
    limits = (
        load_sequence_limits(limits_path, {engine.iid for engine in cfg.engines})
        if limits_path is not None
        else {}
    )

    print(f"profiling {len(targets)} instance(s) against model {cfg.model}")
    dialect = lookup_dialect(cfg.dialect)
    evidence_rows: dict[str, object] = {}
    measurement_record = {
        "method_version": 2,
        **stamp(),
        "model": cfg.model,
        "sweep": asdict(sweep or Sweep()),
        "observation_timeout_s": observation_timeout_s,
        "engines": evidence_rows,
    }
    connections = max((sweep or Sweep()).decode_concurrency) + len(cfg.engines)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            observation_timeout_s or 300.0,
            connect=min(10.0, observation_timeout_s or 300.0),
        ),
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
        headers=cfg.engine_headers(),
    ) as client:
        for spec in targets:
            r = await client.get(
                f"{spec.url}{dialect.health_path}", timeout=observation_timeout_s or 10.0
            )
            if r.status_code != 200:
                results.record_error(
                    "engine_unhealthy", "Health gate failed", stage="health", engine=spec.iid
                )
                print(f"  {spec.iid}: not healthy, aborting", file=sys.stderr)
                return 1
            if dialect.tokenize_path is None:
                raise ValueError(
                    f"{spec.iid}: the {dialect.name} dialect needs a tokenization route "
                    "that reports max_model_len before profiling"
                )
            max_model_len = await engine_context_limit(
                client, spec.url, cfg.model, dialect, observation_timeout_s or 30.0
            )
            max_num_seqs = limits.get(spec.iid)
            engine_sweep = bounded_sweep(sweep or Sweep(), max_model_len, max_num_seqs)
            print(
                f"  {spec.iid}: max_model_len {max_model_len}; "
                f"prefill up to {max(engine_sweep.prefill_lens)}, "
                f"decode input up to {max(engine_sweep.decode_input_lens)}, "
                f"decode concurrency up to {max(engine_sweep.decode_concurrency)}"
            )
            engine_evidence: dict[str, object] = {
                "max_model_len": max_model_len,
                "sweep": asdict(engine_sweep),
            }
            if max_num_seqs is not None:
                engine_evidence["max_num_seqs"] = max_num_seqs
            neighbour_load = None
            group = spec.shared_device.group if spec.shared_device is not None else None
            if colocated_workload is not None:
                if group is None:
                    raise ValueError(f"{spec.iid}: --colocated requires a shared_device group")
                peers = [
                    (peer.iid, peer.url, peer.role)
                    for peer in cfg.engines
                    if peer.iid != spec.iid
                    and peer.shared_device is not None
                    and peer.shared_device.group == group
                ]
                if not peers:
                    raise ValueError(f"{spec.iid}: no neighbours in shared_device group {group}")
                neighbour_load = NeighbourLoad(
                    client,
                    peers,
                    cfg.model,
                    dialect,
                    cfg.chars_per_token,
                    colocated_workload,
                    observation_timeout_s,
                )
            try:
                if neighbour_load is not None:
                    await neighbour_load.start()
                generation = await read_generation(
                    spec,
                    cfg.engine_contract,
                    timeout_s=observation_timeout_s or cfg.health_timeout_s,
                    headers=cfg.engine_headers(),
                )
                engine_evidence["generation_evidence"] = generation.document
                profile = await profile_instance(
                    client,
                    spec.iid,
                    spec.url,
                    cfg.model,
                    engine_sweep,
                    dialect,
                    cfg.chars_per_token,
                    evidence=engine_evidence,
                    max_model_len=max_model_len,
                    observation_timeout_s=observation_timeout_s,
                )
                if neighbour_load is not None:
                    measured = await neighbour_load.stop()
                    prefill_engines = sum(
                        peer.shared_device is not None
                        and peer.shared_device.group == group
                        and peer.role is Role.PREFILL
                        for peer in cfg.engines
                    )
                    decode_engines = sum(
                        peer.shared_device is not None
                        and peer.shared_device.group == group
                        and peer.role is Role.DECODE
                        for peer in cfg.engines
                    )
                    engine_evidence["colocated_load"] = measured
                    profile = replace(
                        profile,
                        colocated_group=group,
                        colocated_target_role=spec.role.value,
                        colocated_prefill_engines=prefill_engines,
                        colocated_decode_engines=decode_engines,
                        colocated_prefill_rps=float(measured["prefill_rps"]),
                        colocated_decode_rps=float(measured["decode_rps"]),
                    )
                current = await read_generation(
                    spec,
                    cfg.engine_contract,
                    timeout_s=observation_timeout_s or cfg.health_timeout_s,
                    headers=cfg.engine_headers(),
                )
                if generation.digest != current.digest:
                    raise ValueError(f"{spec.iid}: engine generation changed during profiling")
                profile = replace(profile, generation_digest=generation.digest)
                problems = decode_evidence_problems(
                    profile,
                    max_fit_mape=cfg.profile_validation.max_decode_fit_mape,
                    max_cv_mape=cfg.profile_validation.max_decode_cv_mape,
                )
                if problems:
                    raise RuntimeError("profile rejected: " + "; ".join(problems))
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                if neighbour_load is not None and neighbour_load.tasks:
                    for task in neighbour_load.tasks:
                        task.cancel()
                    await asyncio.gather(*neighbour_load.tasks, return_exceptions=True)
                    engine_evidence["colocated_load"] = neighbour_load.evidence()
                engine_evidence["error"] = str(exc)
                evidence_rows[spec.iid] = engine_evidence
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                with evidence_path.open(
                    "x" if len(evidence_rows) == 1 and not overwrite else "w", encoding="utf-8"
                ) as output:
                    output.write(json.dumps(measurement_record, indent=2) + "\n")
                raise
            engine_evidence["profile"] = asdict(profile)
            evidence_rows[spec.iid] = engine_evidence
            # Keep a completed engine's observations even if a later engine fails.
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            first = len(evidence_rows) == 1
            if first and not overwrite:
                # Reserve the store before writing; exclusive creation also closes
                # the race with another default-mode profiler after the initial check.
                with store.path.open("x", encoding="utf-8"):
                    pass
            with evidence_path.open(
                "w" if overwrite or not first else "x", encoding="utf-8"
            ) as output:
                output.write(json.dumps(measurement_record, indent=2) + "\n")
            store.put(profile)
            print(
                f"    fit: ttft = {profile.ttft_a:.3e}n^2 + {profile.ttft_b:.3e}n "
                f"+ {profile.ttft_c:.4f}"
            )
            print(
                f"         tpot = {profile.tpot_request_slope:.3e}q + "
                f"{profile.tpot_slope:.3e}b + {profile.tpot_intercept:.4f}"
            )
            cv = (
                f"{profile.decode_cv_mape:.1%}"
                if profile.decode_cv_mape is not None
                else "unavailable"
            )
            fit_error = profile.decode_fit_mape if profile.decode_fit_mape is not None else 0.0
            print(f"         decode fit MAPE {fit_error:.1%}; cross-validation {cv}")
    print(f"wrote {len(store)} profile(s) to {cfg.profiles_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the profiling CLI."""
    return results.invoke("narwhal-profile", argv, _main, operation="profile")


def _main(argv: list[str]) -> int:
    """Parse one profiling operation."""
    ap = argparse.ArgumentParser(
        description="Measure live prefill/decode curves into fleet profiles.path, refit saved "
        "TTFT samples, or merge measured role mixes. Refits and merges use --out and write "
        "a .samples.json sidecar; sweep and neighbour options apply to live measurement.",
    )
    add_version_argument(ap)
    results.add_format(ap)
    ap.add_argument("--fleet", required=True, help="fleet config JSON")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="live-sweep instance ID; repeatable (default: all configured engines)",
    )
    ap.add_argument(
        "--refit-samples",
        type=Path,
        help="refit TTFT from a generation-bound sample sidecar covering the complete fleet, "
        "retaining decode fits; requires --out; exclusive with --only and --merge",
    )
    ap.add_argument(
        "--merge",
        type=Path,
        action="append",
        default=[],
        help="measured profile store with matching sidecar; repeat at least twice and supply "
        "--out; combined stores must cover the fleet; exclusive with --refit-samples, "
        "--only and --overwrite",
    )
    ap.add_argument(
        "--out",
        type=Path,
        help="fresh profile path required for --refit-samples and --merge; "
        "also writes PATH with suffix replaced by .samples.json",
    )
    ap.add_argument(
        "--limits",
        type=Path,
        help="generated max_num_seqs limits for live decode cohorts "
        "(default: use the requested concurrency points)",
    )
    ap.add_argument(
        "--observation-timeout-s",
        type=float,
        help="positive diagnostic HTTP timeout for live health, tokenization, prefill, "
        "decode, metrics and generation probes (default: each probe's built-in limit)",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="replace live-sweep profiles and sample sidecar; --only retains selected engines "
        "(default: require fresh outputs); refits always require fresh outputs",
    )
    ap.add_argument(
        "--prefill-lens",
        default=",".join(str(n) for n in PREFILL_LENS),
        help="comma-separated positive input token lengths; at least three usable distinct "
        "points within live max_model_len (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-concurrency",
        default=",".join(str(n) for n in DECODE_CONCURRENCY),
        help="comma-separated positive simultaneous stream counts; at least two usable "
        "distinct points after --limits (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-input-lens",
        default=",".join(str(n) for n in DECODE_INPUT_LENS),
        help="comma-separated positive input token lengths; at least two usable distinct "
        "points with room for --decode-tokens (default: %(default)s)",
    )
    ap.add_argument(
        "--cached-prefix-lens",
        default=",".join(str(n) for n in CACHED_PREFIX_LENS),
        help="comma-separated cached prefix lengths for the warm prefill sweep, which runs "
        "when the engine reuses a cached prefix; at least two, and at least five cases "
        "with --cached-suffix-lens (default: %(default)s)",
    )
    ap.add_argument(
        "--cached-suffix-lens",
        default=",".join(str(n) for n in CACHED_SUFFIX_LENS),
        help="comma-separated uncached suffix lengths for the warm prefill sweep; "
        "at least two (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-tokens",
        type=int,
        default=DECODE_TOKENS,
        help="output tokens per decode stream, at least 3 (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-repeats",
        type=int,
        default=1,
        help="repetitions per decode input/concurrency point, at least 1 (default: %(default)s)",
    )
    ap.add_argument(
        "--prefill-repeats",
        type=int,
        default=PREFILL_REPEATS,
        help="repetitions per prefill length, at least 3; fit the median (default: %(default)s)",
    )
    ap.add_argument(
        "--colocated",
        action="store_true",
        help="load peers in each target's shared_device group during the live sweep; "
        "requires all five --neighbour-* options (default: target-only traffic)",
    )
    ap.add_argument(
        "--neighbour-prefill-rps",
        type=float,
        help="finite positive requests/second per prefill neighbour; required with --colocated",
    )
    ap.add_argument(
        "--neighbour-decode-rps",
        type=float,
        help="finite positive requests/second per decode neighbour; required with --colocated",
    )
    ap.add_argument(
        "--neighbour-prefill-tokens",
        type=int,
        help="positive input tokens per prefill neighbour request (one output token); "
        "required with --colocated",
    )
    ap.add_argument(
        "--neighbour-decode-input-tokens",
        type=int,
        help="positive input tokens per decode neighbour request; required with --colocated",
    )
    ap.add_argument(
        "--neighbour-decode-output-tokens",
        type=int,
        help="positive output tokens per decode neighbour request; input plus output must "
        "fit its live max_model_len; required with --colocated",
    )
    args = ap.parse_args(argv)
    if args.observation_timeout_s is not None and (
        not math.isfinite(args.observation_timeout_s) or args.observation_timeout_s <= 0
    ):
        ap.error("--observation-timeout-s must be finite and positive")
    if args.observation_timeout_s is not None and (args.merge or args.refit_samples):
        ap.error("--observation-timeout-s applies only to live profiling")
    try:
        cfg = FleetConfig.load(args.fleet)
    except (OSError, ValueError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-profile", f"load fleet {args.fleet}", exc, 2)
    if results.json_mode():
        results.protect_environment(cfg.engine_api_key_env)
        results.set_operation(
            "merge" if args.merge else "refit" if args.refit_samples else "profile"
        )
        output_path = args.out or cfg.profiles_path
        results.add_artifact("profiles", output_path)
        results.add_artifact("profile_samples", output_path.with_suffix(".samples.json"))
        results.set_data(
            {
                "engines": [
                    engine.iid for engine in cfg.engines if not args.only or engine.iid in args.only
                ]
            }
        )
    if any(
        path.resolve() == Path(args.fleet).resolve()
        or (path.exists() and path.samefile(args.fleet))
        for path in (cfg.profiles_path, cfg.profiles_path.with_suffix(".samples.json"))
    ):
        ap.error("profile outputs must not replace the fleet config")
    try:
        sweep = Sweep(
            prefill_lens=tuple(int(x) for x in args.prefill_lens.split(",") if x.strip()),
            decode_concurrency=tuple(
                int(x) for x in args.decode_concurrency.split(",") if x.strip()
            ),
            decode_input_lens=tuple(int(x) for x in args.decode_input_lens.split(",") if x.strip()),
            decode_tokens=args.decode_tokens,
            decode_repeats=args.decode_repeats,
            prefill_repeats=args.prefill_repeats,
            cached_prefix_lens=tuple(
                int(x) for x in args.cached_prefix_lens.split(",") if x.strip()
            ),
            cached_suffix_lens=tuple(
                int(x) for x in args.cached_suffix_lens.split(",") if x.strip()
            ),
        )
    except ValueError:
        ap.error(
            "--prefill-lens, --decode-input-lens, --decode-concurrency, --cached-prefix-lens "
            "and --cached-suffix-lens take comma-separated integers"
        )
    prefixes, suffixes = set(sweep.cached_prefix_lens), set(sweep.cached_suffix_lens)
    if len(prefixes) < 2 or len(suffixes) < 2 or len(prefixes) * len(suffixes) < 5:
        ap.error(
            "the warm prefill fit needs two distinct prefix lengths, two distinct suffix "
            "lengths and at least five prefix and suffix cases"
        )
    if any(value <= 0 for value in (*sweep.cached_prefix_lens, *sweep.cached_suffix_lens)):
        ap.error("cached prefix and suffix lengths must be positive")
    if len(set(sweep.prefill_lens)) < 3:
        ap.error("the sweep needs at least three distinct prefill lengths")
    if len(set(sweep.decode_input_lens)) < 2 or len(set(sweep.decode_concurrency)) < 2:
        ap.error(
            "the decode fit needs two distinct input lengths and two distinct concurrency steps"
        )
    if any(value <= 0 for value in (*sweep.prefill_lens, *sweep.decode_input_lens)):
        ap.error("profile lengths must be positive")
    if any(value < 1 for value in sweep.decode_concurrency):
        ap.error("decode concurrency must be at least 1")
    if sweep.decode_tokens < 3 or sweep.prefill_repeats < 3 or sweep.decode_repeats < 1:
        ap.error(
            "--decode-tokens needs at least 3 (two intervals need three tokens); "
            "--prefill-repeats at least 3 for a repeat median; --decode-repeats at least 1"
        )
    neighbour_values = (
        args.neighbour_prefill_rps,
        args.neighbour_decode_rps,
        args.neighbour_prefill_tokens,
        args.neighbour_decode_input_tokens,
        args.neighbour_decode_output_tokens,
    )
    if args.colocated:
        if any(
            value is None or not math.isfinite(value) or value <= 0 for value in neighbour_values
        ):
            ap.error("--colocated requires positive neighbour rates and token lengths")
        if any(type(value) is not int for value in neighbour_values[2:]):
            ap.error("neighbour token lengths must be integers")
        colocated_workload = ColocatedWorkload(*neighbour_values)
    else:
        if any(value is not None for value in neighbour_values):
            ap.error("neighbour load options require --colocated")
        colocated_workload = None
    try:
        if args.merge:
            if args.refit_samples is not None or args.out is None or args.only or args.overwrite:
                ap.error("--merge requires --out and cannot combine with refit, only, or overwrite")
            if len(args.merge) < 2:
                ap.error("--merge needs at least two measured profile stores")
            return merge_profiles(args.merge, args.out, {engine.iid for engine in cfg.engines})
        if args.refit_samples is not None or args.out is not None:
            if args.refit_samples is None or args.out is None or args.only:
                ap.error("--refit-samples requires --out and a complete fleet selection")
            return refit_saved_prefill(
                args.refit_samples, args.out, {engine.iid for engine in cfg.engines}
            )
        return asyncio.run(
            run(
                cfg,
                set(args.only) or None,
                sweep,
                overwrite=args.overwrite,
                limits_path=args.limits,
                colocated_workload=colocated_workload,
                observation_timeout_s=args.observation_timeout_s,
            )
        )
    except (OSError, ValueError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-profile", f"profile fleet {args.fleet}", exc, 2)
    except (RuntimeError, httpx.HTTPError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-profile", f"profile fleet {args.fleet}", exc, 1)


if __name__ == "__main__":
    raise SystemExit(main())
