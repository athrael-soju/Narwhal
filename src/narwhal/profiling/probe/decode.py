from __future__ import annotations

import asyncio
import math
import statistics
import time
from contextlib import aclosing

import httpx

from ...engines.dialect import EngineDialect
from ...engines.stream import event_choices, sse_events, token_ids
from ..tasks import cancel_tasks
from .engine import make_prompt
from .sweep import DECODE_CONCURRENCY, DECODE_INPUT_LENS, DECODE_TOKENS

# Each retry sizes the cohort's tokens from the previous attempt's join lag.
DECODE_RETRIES = 3
# A cohort sized to the KV cache leaves the rest free for a lagged cohort's longer retry.
LARGEST_FIT_KV_SHARE = 0.8


async def _one_decode_stream(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    input_len: int,
    state: dict[str, float],
    samples: list[tuple[float, float, float]],
    dialect: EngineDialect,
    tokens: int = DECODE_TOKENS,
) -> None:
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
    state: dict[str, float] = {"resident": 0, "requests": 0, "epoch": 0, "cohort": cohort}
    observed: list[tuple[float, float, float]] = []
    tasks = [
        asyncio.create_task(
            _one_decode_stream(
                client, url, model, prompt, input_len, state, observed, dialect, tokens
            )
        )
        for _ in range(cohort)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        await cancel_tasks(tasks)
    return observed, state


def stream_kv_tokens(input_len: int, tokens: int, block_tokens: int | None, reserved: int) -> int:
    """Return the KV tokens an engine sets aside for one decode stream."""
    need = input_len + max(tokens, reserved)
    if block_tokens:
        need = math.ceil(need / block_tokens) * block_tokens
    return need


def usable_kv_tokens(
    kv_capacity_tokens: int, block_tokens: int | None, reserved_blocks: int = 0
) -> int:
    """Return the KV tokens decode streams can use from the engine's reported capacity."""
    return kv_capacity_tokens - reserved_blocks * (block_tokens or 0)


def decode_cells(
    input_len: int,
    concurrency: tuple[int, ...],
    tokens: int,
    kv_capacity_tokens: int | None,
    *,
    block_tokens: int | None = None,
    reserved: int = 0,
    reserved_blocks: int = 0,
) -> tuple[int, ...]:
    """Return the cohorts measured at `input_len`.

    Cohorts whose streams exceed the KV cache are skipped, and the largest cohort that fills at
    most `LARGEST_FIT_KV_SHARE` of it is measured after the ones that fit. An input length
    where fewer than two cohorts fit is skipped whole.
    """
    if kv_capacity_tokens is None:
        return concurrency
    cells = _fitting_cells(
        input_len, concurrency, tokens, kv_capacity_tokens, block_tokens, reserved, reserved_blocks
    )
    return cells if len(cells) >= 2 else ()


def _fitting_cells(
    input_len: int,
    concurrency: tuple[int, ...],
    tokens: int,
    kv_capacity_tokens: int,
    block_tokens: int | None,
    reserved: int,
    reserved_blocks: int,
) -> tuple[int, ...]:
    """Return the cohorts that fit the KV cache at `input_len`, however many there are."""
    need = stream_kv_tokens(input_len, tokens, block_tokens, reserved)
    usable = usable_kv_tokens(kv_capacity_tokens, block_tokens, reserved_blocks)
    cells = tuple(c for c in concurrency if c * need <= usable)
    if len(cells) < len(concurrency):
        largest = int(usable * LARGEST_FIT_KV_SHARE) // need
        if largest > max(cells, default=0):
            cells += (largest,)
    return cells


def _retry_kv_limit(
    kv_capacity_tokens: int,
    cohort: int,
    input_len: int,
    block_tokens: int | None,
    reserved_blocks: int,
) -> int:
    # A retry's longer streams must still fit in the KV cache, block by block.
    per_stream = usable_kv_tokens(kv_capacity_tokens, block_tokens, reserved_blocks) // cohort
    if block_tokens:
        per_stream -= per_stream % block_tokens
    return per_stream - input_len


def _too_few_lengths(
    url: str, kv_capacity_tokens: int | None, tokens: int, dropped: list[str] | None = None
) -> ValueError:
    if dropped:
        return ValueError(
            f"{url} measured two decode concurrency points at fewer than two decode input "
            f"lengths after dropping {'; '.join(dropped)}; choose shorter --decode-input-lens "
            "or lower --decode-concurrency"
        )
    return ValueError(
        f"{url} holds {kv_capacity_tokens} KV tokens, which fit two decode concurrency points "
        f"at fewer than two decode input lengths with {tokens} output tokens; choose shorter "
        "--decode-input-lens or lower --decode-concurrency"
    )


def _overlapping_tokens(state: dict[str, float], tokens: int, limit: int | None) -> int | None:
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
    dialect: EngineDialect,
    concurrency: tuple[int, ...] = DECODE_CONCURRENCY,
    tokens: int = DECODE_TOKENS,
    chars_per_token: float = 3.8,
    input_lens: tuple[int, ...] = DECODE_INPUT_LENS,
    *,
    evidence: list[dict[str, object]] | None = None,
    max_model_len: int | None = None,
    repeats: int = 1,
    observation_timeout_s: float | None = None,
    kv_capacity_tokens: int | None = None,
    block_tokens: int | None = None,
    skipped: list[dict[str, object]] | None = None,
) -> list[tuple[float, float, float]]:
    reserved = dialect.decode_reserved_tokens
    reserved_blocks = dialect.kv_reserved_blocks

    def cells_at(input_len: int) -> tuple[int, ...]:
        return decode_cells(
            input_len,
            concurrency,
            tokens,
            kv_capacity_tokens,
            block_tokens=block_tokens,
            reserved=reserved,
            reserved_blocks=reserved_blocks,
        )

    if kv_capacity_tokens is not None and sum(bool(cells_at(n)) for n in input_lens) < 2:
        raise _too_few_lengths(url, kv_capacity_tokens, tokens)
    samples: list[tuple[float, float, float]] = []
    measured_lengths: set[int] = set()
    # Each dropped cohort that left its input length with fewer than two cohorts.
    dropped_lengths: list[str] = []
    warmed: set[int] = set()
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
        cells = cells_at(input_len)
        # The cohorts that fit at a length skipped because fewer than two fit there.
        alone: tuple[int, ...] = ()
        if not cells and kv_capacity_tokens is not None:
            alone = _fitting_cells(
                input_len,
                concurrency,
                tokens,
                kv_capacity_tokens,
                block_tokens,
                reserved,
                reserved_blocks,
            )
        alone_reason = f"fewer than two cohorts fit at isl={input_len}"
        if skipped is not None:
            skipped.extend(
                {
                    "input_tokens": input_len,
                    "concurrency": c,
                    "kv_tokens": c * stream_kv_tokens(input_len, tokens, block_tokens, reserved),
                    "kv_capacity_tokens": kv_capacity_tokens,
                    **({"reason": alone_reason} if c in alone else {}),
                }
                for c in dict.fromkeys((*concurrency, *alone))
                if c not in cells
            )
        missing = [c for c in concurrency if c not in cells and c not in alone]
        if missing:
            largest = [c for c in cells if c not in concurrency]
            print(
                f"    decode  isl={input_len:<6} skipped c="
                + ",".join(str(c) for c in missing)
                + f": cohorts exceed {kv_capacity_tokens} KV tokens"
                + (f"; measuring c={largest[0]}" if largest else "")
            )
        if alone:
            print(
                f"    decode  isl={input_len:<6} skipped c="
                + ",".join(str(c) for c in alone)
                + f": {alone_reason}"
            )
        # An engine's first decode at each batch size can include kernel compilation; it stays
        # out of the fit.
        for c in cells:
            if c not in warmed:
                await _decode_cohort(client, url, model, prompt, input_len, c, tokens, dialect)
                warmed.add(c)
        # The length's cells and interval evidence, kept once two cohorts remain.
        length_samples: list[tuple[float, float, float]] = []
        length_cohorts: list[int] = []
        length_evidence: list[dict[str, object]] = []
        length_dropped: list[tuple[int, str]] = []
        for c in cells:
            replicates: list[tuple[float, float, float]] = []
            intervals: list[dict[str, object]] = []
            dropped: str | None = None
            for repeat in range(repeats):
                used = tokens
                observed, state = await _decode_cohort(
                    client, url, model, prompt, input_len, c, used, dialect
                )
                limit = max_model_len - input_len if max_model_len is not None else None
                if kv_capacity_tokens is not None:
                    fits = _retry_kv_limit(
                        kv_capacity_tokens, c, input_len, block_tokens, reserved_blocks
                    )
                    limit = fits if limit is None else min(limit, fits)
                for _ in range(DECODE_RETRIES):
                    if len(observed) >= 2 * c:
                        break
                    retry = _overlapping_tokens(state, used, limit)
                    if retry is None:
                        break
                    used = retry
                    observed, state = await _decode_cohort(
                        client, url, model, prompt, input_len, c, used, dialect
                    )
                if len(observed) < 2 * c:
                    problem = (
                        f"decode probe has insufficient complete-cohort intervals: "
                        f"got {len(observed)}, need {2 * c}, isl={input_len}, c={c}, "
                        f"tokens={used}"
                    )
                    # The KV-bound cohort adds a point past the requested cohorts, which carry
                    # the fit without it.
                    if c in concurrency:
                        raise RuntimeError(problem)
                    dropped = problem
                    break
                intervals.append(
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
            if dropped is not None:
                if skipped is not None:
                    skipped.append(
                        {
                            "input_tokens": input_len,
                            "concurrency": c,
                            "kv_tokens": c
                            * stream_kv_tokens(input_len, tokens, block_tokens, reserved),
                            "kv_capacity_tokens": kv_capacity_tokens,
                            "reason": dropped,
                        }
                    )
                print(f"    decode  isl={input_len:<6} dropped c={c}: {dropped}")
                length_dropped.append((c, dropped))
                continue
            length_evidence.extend(intervals)
            requests, batch, gap = (
                statistics.median(row[i] for row in replicates) for i in range(3)
            )
            length_samples.append((requests, batch, gap))
            length_cohorts.append(c)
            print(
                f"    decode  isl={input_len:<6} c={c:<3} "
                f"batch~{batch:>7.0f} tok, seq~{requests:>4.0f} "
                f"-> {gap * 1000:6.2f} ms/token"
            )
        if length_dropped and len(length_samples) < 2:
            reason = f"fewer than two cohorts remain at isl={input_len} after dropping " + (
                ", ".join(f"c={c}" for c, _ in length_dropped)
            )
            if skipped is not None:
                skipped.extend(
                    {
                        "input_tokens": input_len,
                        "concurrency": c,
                        "kv_tokens": c
                        * stream_kv_tokens(input_len, tokens, block_tokens, reserved),
                        "kv_capacity_tokens": kv_capacity_tokens,
                        "reason": reason,
                    }
                    for c in length_cohorts
                )
            print(
                f"    decode  isl={input_len:<6} dropped c="
                + ",".join(str(c) for c in length_cohorts)
                + f": {reason}"
            )
            dropped_lengths.extend(
                f"c={c} at isl={input_len} ({problem})" for c, problem in length_dropped
            )
            continue
        if evidence is not None:
            evidence.extend(length_evidence)
        samples.extend(length_samples)
        if length_samples:
            measured_lengths.add(input_len)
    # Tokenized prompts can run a token or two past their targets and drop a length at the limit.
    if kv_capacity_tokens is not None and len(measured_lengths) < 2:
        raise _too_few_lengths(url, kv_capacity_tokens, tokens, dropped_lengths)
    return samples
