from __future__ import annotations

import statistics
import time

import httpx

from ...engines.dialect import EngineDialect
from .engine import completion_body, make_prompt
from .sweep import PREFILL_LENS, PREFILL_REPEATS


async def probe_prefill(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    dialect: EngineDialect,
    lens: tuple[int, ...] = PREFILL_LENS,
    repeats: int = PREFILL_REPEATS,
    chars_per_token: float = 3.8,
    max_model_len: int | None = None,
    observation_timeout_s: float | None = None,
) -> list[tuple[float, float]]:
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
            body = completion_body(model, prompt, 1, dialect, dialect.cold_probe_extras())
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
