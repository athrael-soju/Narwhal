"""Decode token-gap probe across input lengths and concurrency."""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from contextlib import aclosing

import httpx

from ...engines.dialect import EngineDialect, VllmDialect
from ...engines.stream import event_choices, sse_events, token_ids
from ..tasks import cancel_tasks
from .engine import make_prompt
from .sweep import DECODE_CONCURRENCY, DECODE_INPUT_LENS, DECODE_TOKENS


async def _one_decode_stream(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    input_len: int,
    state: dict[str, float],
    samples: list[tuple[float, float, float]],
    tokens: int = DECODE_TOKENS,
    dialect: EngineDialect | None = None,
) -> None:
    """Append exact-token gaps measured while the complete cohort decodes to `samples`.

    Intervals crossing a cohort arrival or departure are excluded.
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
    last_epoch = -1.0
    done = False
    finished = False
    try:
        async with client.stream("POST", f"{url}/v1/completions", json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")
                raise RuntimeError(
                    f"decode probe failed on {url} ({r.status_code}): {detail[:200]}"
                )
            async with aclosing(sse_events(r.aiter_bytes())) as events:
                async for event in events:
                    if not event.line.startswith("data:"):
                        continue
                    if event.done:
                        done = True
                        break
                    if event.malformed:
                        raise RuntimeError("decode probe returned malformed SSE")
                    obj = event.data
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
                        state["joined"] = state.get("joined", 0) + 1
                        state.setdefault("first_at", now)
                        if state["joined"] == state["cohort"]:
                            state["last_join_at"] = now
                    mine += len(ids)
                    state["resident"] += len(ids)
                    if (
                        len(ids) == 1
                        and last is not None
                        and last_epoch == state["epoch"]
                        and state["requests"] == state["cohort"]
                        and now > last
                    ):
                        samples.append(
                            (float(state["requests"]), float(state["resident"]), now - last)
                        )
                    # vLLM can bundle final token IDs even with stream_interval=1.
                    # Gaps adjacent to a multi-token chunk are discarded.
                    last = now if len(ids) == 1 else None
                    last_epoch = state["epoch"]
        if not done or not finished or mine != tokens:
            raise RuntimeError(
                f"decode probe incomplete: tokens={mine}/{tokens}, done={done}, finished={finished}"
            )
    finally:
        if mine:
            state.setdefault("left_at", time.monotonic())
            state["resident"] -= input_len + mine
            state["requests"] -= 1
            state["epoch"] += 1


async def _decode_cohort(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    input_len: int,
    cohort: int,
    tokens: int,
    dialect: EngineDialect,
) -> tuple[list[tuple[float, float, float]], dict[str, float]]:
    """Run one decode cohort and return its complete-cohort intervals and timing state."""
    state: dict[str, float] = {"resident": 0, "requests": 0, "epoch": 0, "cohort": cohort}
    observed: list[tuple[float, float, float]] = []
    tasks = [
        asyncio.create_task(
            _one_decode_stream(
                client, url, model, prompt, input_len, state, observed, tokens, dialect
            )
        )
        for _ in range(cohort)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        await cancel_tasks(tasks)
    return observed, state


def _overlapping_tokens(state: dict[str, float], tokens: int, limit: int | None) -> int | None:
    """Size a retry so the first member still decodes when the last member joins.

    Return None when the cohort already overlapped or the size exceeds `limit`.
    """
    first, joined, left = state.get("first_at"), state.get("last_join_at"), state.get("left_at")
    if first is None or joined is None or left is None or joined < left or left <= first:
        return None
    sized = tokens + math.ceil(tokens * (joined - first) / (left - first))
    if limit is not None and sized > limit:
        return None
    return sized


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
                used = tokens
                observed, state = await _decode_cohort(
                    client, url, model, prompt, input_len, c, used, dialect
                )
                if len(observed) < 2 * c:
                    limit = max_model_len - input_len if max_model_len is not None else None
                    retry = _overlapping_tokens(state, tokens, limit)
                    if retry is not None:
                        used = retry
                        observed, state = await _decode_cohort(
                            client, url, model, prompt, input_len, c, used, dialect
                        )
                if len(observed) < 2 * c:
                    raise RuntimeError(
                        f"decode probe has insufficient complete-cohort intervals: "
                        f"got {len(observed)}, need {2 * c}, isl={input_len}, c={c}, "
                        f"tokens={used}"
                    )
                if evidence is not None:
                    evidence.append(
                        {
                            "input_tokens": input_len,
                            "concurrency": c,
                            "repeat": repeat,
                            "tokens": used,
                            "intervals": observed,
                        }
                    )
                replicates.append(
                    (
                        statistics.median(s[0] for s in observed),
                        statistics.median(s[1] for s in observed),
                        # Mean gap keeps total service time when stalled
                        # tokens arrive in a burst.
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
