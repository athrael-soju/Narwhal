from __future__ import annotations

from typing import Any

import httpx

from ...engines.dialect import EngineDialect
from ...engines.metrics import EngineMetrics


async def _engine_metrics(client: httpx.AsyncClient, url: str, timeout_s: float) -> str | None:
    try:
        response = await client.get(f"{url}/metrics", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.text


async def kv_capacity(
    client: httpx.AsyncClient, url: str, metrics: EngineMetrics, timeout_s: float = 30.0
) -> int | None:
    text = await _engine_metrics(client, url, timeout_s)
    return None if text is None else metrics.kv_capacity(text)


async def cache_block_tokens(
    client: httpx.AsyncClient, url: str, metrics: EngineMetrics, timeout_s: float = 30.0
) -> int | None:
    text = await _engine_metrics(client, url, timeout_s)
    return None if text is None else metrics.cache_block_tokens(text)


async def prefix_cache_hits(
    client: httpx.AsyncClient, url: str, metrics: EngineMetrics, timeout_s: float = 30.0
) -> int | None:
    text = await _engine_metrics(client, url, timeout_s)
    return None if text is None else metrics.prefix_cache_hits(text)


async def _tokenize_response(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    dialect: EngineDialect,
    timeout_s: float = 30.0,
) -> dict:
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


async def count_tokens(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    dialect: EngineDialect,
    timeout_s: float = 30.0,
) -> int:
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
    dialect: EngineDialect,
    chars_per_token: float = 3.8,
    timeout_s: float = 30.0,
    prefix: str = "",
    max_input_tokens: int | None = None,
) -> tuple[str, int]:
    word = "benchmark "
    if max_input_tokens is not None and max_input_tokens < 1:
        raise ValueError("max_input_tokens must be positive")
    if dialect.tokenize_path is None:
        if max_input_tokens is not None:
            raise RuntimeError("bounded prompt sizing requires an exact-count tokenizer route")
        text = (prefix + word * max(1, target))[: max(1, int(target * chars_per_token))]
        return text, max(1, round(len(text) / chars_per_token))
    text = prefix + word * max(1, target)
    minimum_chars = max(1, len(prefix)) if max_input_tokens is not None else 1
    got = await count_tokens(client, url, model, text, dialect, timeout_s)
    if got != target:
        scaled = max(minimum_chars, int(len(text) * target / got))
        text = text[:scaled]
        got = await count_tokens(client, url, model, text, dialect, timeout_s)
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
            got = await count_tokens(client, url, model, text, dialect, timeout_s)
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
    return {
        "model": model,
        "prompt": prompt,
        "max_tokens": tokens,
        "temperature": 0.0,
        "stream": False,
        **dialect.decode_probe_extras(tokens),
        **(extras or {}),
    }
