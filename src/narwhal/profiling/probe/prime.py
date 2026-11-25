"""Serve discarded requests on an engine before its sweep, so first-use costs stay out of it."""

from __future__ import annotations

import httpx

from ...engines.dialect import EngineDialect
from .engine import completion_body, make_prompt

# The prompt starts apart from the sweep prompts, so no cached prefix carries into the sweep.
PRIME_PREFIX = "narwhal warm-up "


async def prime_engine(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    dialect: EngineDialect,
    requests: int,
    input_tokens: int,
    output_tokens: int,
    chars_per_token: float = 3.8,
    observation_timeout_s: float | None = None,
) -> None:
    """Send `requests` completions of `output_tokens` one at a time and discard their timings."""
    if requests < 1:
        return
    prompt, _ = await make_prompt(
        client,
        url,
        model,
        input_tokens,
        dialect,
        chars_per_token,
        timeout_s=observation_timeout_s or 30.0,
        prefix=PRIME_PREFIX,
        # The prefix adds tokens, so the bound keeps the prompt within the context limit.
        max_input_tokens=input_tokens if dialect.tokenize_path is not None else None,
    )
    for _ in range(requests):
        body = completion_body(model, prompt, output_tokens, dialect, dialect.cold_probe_extras())
        r = await client.post(
            f"{url}/v1/completions", json=body, timeout=observation_timeout_s or 300.0
        )
        if r.status_code != 200:
            raise RuntimeError(f"warm-up request failed on {url} ({r.status_code}): {r.text[:200]}")
