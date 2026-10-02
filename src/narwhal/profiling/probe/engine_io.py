"""Engine metric scrapes, tokenization and request bodies shared by the probes."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from ...engines.dialect import EngineDialect, VllmDialect

_KV_CAPACITY = re.compile(r'kv_cache_size_tokens="([0-9]+(?:\.[0-9]+)?)"')


_BLOCK_TOKENS = re.compile(r'^vllm:cache_config_info\{[^}]*\bblock_size="([0-9]+)"', re.MULTILINE)


# vLLM counts prompt tokens served from its prefix cache for new requests only.
_PREFIX_CACHE_HITS = re.compile(
    r"^vllm:prefix_cache_hits_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE
)


async def _engine_metrics(client: httpx.AsyncClient, url: str, timeout_s: float) -> str | None:
    """Return the engine's metrics text, or None when the scrape fails."""
    try:
        response = await client.get(f"{url}/metrics", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.text


def parse_kv_capacity(metrics: str) -> int | None:
    """Read vLLM's physical KV token capacity from its info metric."""
    values = [int(float(match)) for match in _KV_CAPACITY.findall(metrics)]
    return min(values) if values else None


async def kv_capacity(client: httpx.AsyncClient, url: str, timeout_s: float = 30.0) -> int | None:
    """Read physical KV capacity when the engine exports it."""
    metrics = await _engine_metrics(client, url, timeout_s)
    return None if metrics is None else parse_kv_capacity(metrics)


def parse_cache_block_tokens(metrics: str) -> int | None:
    """Read vLLM's cache block size from its info metric."""
    values = {int(match) for match in _BLOCK_TOKENS.findall(metrics)}
    return values.pop() if len(values) == 1 and min(values) > 0 else None


async def cache_block_tokens(
    client: httpx.AsyncClient, url: str, timeout_s: float = 30.0
) -> int | None:
    """Read the engine's cache block size when it exports one."""
    metrics = await _engine_metrics(client, url, timeout_s)
    return None if metrics is None else parse_cache_block_tokens(metrics)


def parse_prefix_cache_hits(metrics: str) -> int | None:
    """Sum vLLM's prefix-cache hit tokens across its engine label sets."""
    values = [float(match) for match in _PREFIX_CACHE_HITS.findall(metrics)]
    return round(sum(values)) if values else None


async def prefix_cache_hits(
    client: httpx.AsyncClient, url: str, timeout_s: float = 30.0
) -> int | None:
    """Read cumulative prefix-cache hit tokens when the engine exports them."""
    metrics = await _engine_metrics(client, url, timeout_s)
    return None if metrics is None else parse_prefix_cache_hits(metrics)


async def _tokenize_response(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    dialect: EngineDialect,
    timeout_s: float = 30.0,
) -> dict:
    """Return the engine's tokenization response for `prompt`; raise on a bad response."""
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


async def tokenize(
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

    Without an exact-count route, counts use `chars_per_token`; `max_input_tokens`
    requires one and shortens only the text after `prefix`.
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
    got = await tokenize(client, url, model, text, dialect, timeout_s)
    if got != target:
        scaled = max(minimum_chars, int(len(text) * target / got))
        text = text[:scaled]
        got = await tokenize(client, url, model, text, dialect, timeout_s)
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
            got = await tokenize(client, url, model, text, dialect, timeout_s)
        if got > max_input_tokens:
            raise RuntimeError(
                f"prompt still has {got} tokens after 16 sizing attempts "
                f"for input bound {max_input_tokens}"
            )
    return text, got


def completion_body(
    model: str,
    prompt: str,
    tokens: int,
    dialect: EngineDialect,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a greedy, non-streaming completion body that forces `tokens` output tokens."""
    return {
        "model": model,
        "prompt": prompt,
        "max_tokens": tokens,
        "temperature": 0.0,
        "stream": False,
        **dialect.decode_probe_extras(tokens),
        **(extras or {}),
    }


async def cancel(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel `tasks` and wait for each to finish."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
