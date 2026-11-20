from __future__ import annotations

import asyncio
import math
import random
import statistics
import time
from contextlib import aclosing, nullcontext

import httpx

from ...engines.dialect import EngineDialect
from ...engines.stream import event_choices, sse_events, token_ids
from ..model import Profile
from ..tasks import cancel_tasks
from .engine import completion_body, make_prompt
from .sweep import DECODE_CONCURRENCY, DECODE_INPUT_LENS, DECODE_TOKENS, PREFILL_SHARES

# Each retry sizes the cohort's tokens from the previous attempt's join lag.
DECODE_RETRIES = 3
# A cohort sized to the KV cache leaves the rest free for a lagged cohort's longer retry.
LARGEST_FIT_KV_SHARE = 0.8
# Reruns of a cohort after a request error or a stalled stream, such as a lost KV transfer.
ENGINE_ERROR_RETRIES = 2


class DecodeEngineError(RuntimeError):
    """An engine refused a decode probe request or reported an error in its stream."""


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
                raise DecodeEngineError(
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
                        detail = str(obj.get("error"))[:200] if obj is not None else "no data"
                        raise DecodeEngineError(
                            f"decode probe returned an error on {url}: {detail}"
                        )
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


class PrefillLegs:
    """Send one-token prefill requests at Poisson times while a decode cohort runs."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        model: str,
        prompt: str,
        dialect: EngineDialect,
        rate: float,
        *,
        seed: int,
        timeout_s: float | None = None,
    ) -> None:
        self.client = client
        self.url = url
        self.model = model
        self.prompt = prompt
        self.dialect = dialect
        self.rate = rate
        self.seed = seed
        self.timeout_s = timeout_s
        # Send times of the current run's legs.
        self.sent: list[float] = []
        self._legs: list[asyncio.Task[None]] = []
        self._run: asyncio.Task[None] | None = None

    async def __aenter__(self) -> PrefillLegs:
        self.sent, self._legs = [], []
        self._run = asyncio.create_task(self._send())
        return self

    async def __aexit__(self, kind: object, error: BaseException | None, trace: object) -> None:
        """Cancel the legs in flight; raise a finished leg's failure when the cohort succeeded."""
        failed = next(
            (
                failure
                for task in self._legs
                if task.done() and not task.cancelled() and (failure := task.exception())
            ),
            None,
        )
        await cancel_tasks([*([self._run] if self._run is not None else []), *self._legs])
        if error is None and failed is not None:
            raise failed

    async def _send(self) -> None:
        # A fixed seed repeats each run's arrival times.
        rng = random.Random(self.seed)  # noqa: S311
        while True:
            await asyncio.sleep(rng.expovariate(self.rate))
            self.sent.append(time.monotonic())
            self._legs.append(asyncio.create_task(self._leg()))

    async def _leg(self) -> None:
        body = completion_body(
            self.model, self.prompt, 1, self.dialect, self.dialect.cold_probe_extras()
        )
        r = await self.client.post(
            f"{self.url}/v1/completions", json=body, timeout=self.timeout_s or 300.0
        )
        if r.status_code != 200:
            raise DecodeEngineError(
                f"prefill leg failed on {self.url} ({r.status_code}): {r.text[:200]}"
            )


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


async def _retried_cohort(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    input_len: int,
    cohort: int,
    tokens: int,
    dialect: EngineDialect,
    beside: PrefillLegs | None = None,
) -> tuple[list[tuple[float, float, float]], dict[str, float]]:
    """Run a cohort, rerunning it whole when an engine reports a request error or a stream
    times out or drops.

    `beside` sends its prefill legs while each run lasts. A rerun discards the failed attempt's
    intervals. The last error propagates after `ENGINE_ERROR_RETRIES` reruns.
    """
    for attempt in range(ENGINE_ERROR_RETRIES + 1):
        try:
            async with beside or nullcontext():
                return await _decode_cohort(
                    client, url, model, prompt, input_len, cohort, tokens, dialect
                )
        except (DecodeEngineError, httpx.TransportError) as exc:
            if attempt == ENGINE_ERROR_RETRIES:
                raise
            detail = str(exc) or type(exc).__name__
            print(
                f"    decode  isl={input_len:<6} c={cohort:<3} rerun after engine error: {detail}"
            )
    raise AssertionError("unreachable")


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
                await _retried_cohort(client, url, model, prompt, input_len, c, tokens, dialect)
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
                observed, state = await _retried_cohort(
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
                    observed, state = await _retried_cohort(
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


def share_cohort(profile: Profile, max_model_len: int | None = None) -> tuple[int, int, int]:
    """Return the prefill-share step's cohort, and each stream's input and output tokens.

    Half of `decode_max_requests` streams end together at the middle of the measured KV range,
    each with as many output tokens as input tokens.
    """
    requests, low, high = (
        profile.decode_max_requests,
        profile.decode_min_kv_tokens,
        profile.decode_max_kv_tokens,
    )
    if requests is None or low is None or high is None:
        raise ValueError(f"{profile.iid}: the prefill share step needs measured decode bounds")
    cohort = max(1, requests // 2)
    stream = (low + high) // 2 // cohort
    if max_model_len is not None:
        stream = min(stream, max_model_len)
    # Two intervals need three output tokens.
    if stream < 6:
        raise ValueError(
            f"{profile.iid}: {cohort} streams in {(low + high) // 2} KV tokens leave "
            f"{stream} tokens per stream; the prefill share step needs at least 6"
        )
    input_len = stream // 2
    return cohort, input_len, stream - input_len


async def probe_prefill_share(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    dialect: EngineDialect,
    profile: Profile,
    prefill_len: int,
    *,
    shares: tuple[float, ...] = PREFILL_SHARES,
    chars_per_token: float = 3.8,
    max_model_len: int | None = None,
    observation_timeout_s: float | None = None,
    evidence: list[dict[str, object]] | None = None,
) -> list[tuple[float, float]]:
    """Return (prefill share, mean resident token interval) for each offered share.

    The `share_cohort` streams decode while prefill legs of `prefill_len` prompt tokens arrive
    at Poisson times, at the offered share of the engine's time priced with the profile's
    prefill fit. The returned share counts the legs sent while the whole cohort was resident.
    """
    cohort, target, tokens = share_cohort(profile, max_model_len)
    timeout = observation_timeout_s or 30.0
    prompt, input_len = await make_prompt(
        client, url, model, target, dialect, chars_per_token, timeout_s=timeout
    )
    leg_prompt, leg_len = await make_prompt(
        client, url, model, prefill_len, dialect, chars_per_token, timeout_s=timeout
    )
    if max_model_len is not None and (
        input_len + tokens > max_model_len or leg_len + 1 > max_model_len
    ):
        raise ValueError(
            f"prefill share streams of {input_len}+{tokens} tokens or legs of {leg_len}+1 "
            f"tokens exceed {url} max_model_len {max_model_len}"
        )
    leg_s = profile.prefill_time(leg_len)
    if leg_s <= 0:
        raise ValueError(f"{profile.iid}: the prefill fit prices a {leg_len}-token leg at 0 s")
    # The cohort's first decode at its batch size can include kernel compilation.
    await _retried_cohort(client, url, model, prompt, input_len, cohort, tokens, dialect)
    samples: list[tuple[float, float]] = []
    for index, offered in enumerate(shares):
        legs = (
            PrefillLegs(
                client,
                url,
                model,
                leg_prompt,
                dialect,
                offered / leg_s,
                seed=index,
                timeout_s=observation_timeout_s,
            )
            if offered > 0
            else None
        )
        observed, state = await _retried_cohort(
            client, url, model, prompt, input_len, cohort, tokens, dialect, legs
        )
        start, end = state.get("last_join_at"), state.get("left_at")
        if len(observed) < 2 * cohort or start is None or end is None or end <= start:
            raise RuntimeError(
                f"prefill share probe has insufficient complete-cohort intervals: got "
                f"{len(observed)}, need {2 * cohort}, share={offered}, c={cohort}, "
                f"tokens={tokens}"
            )
        sent = sum(start <= at < end for at in legs.sent) if legs is not None else 0
        share = sent * leg_s / (end - start)
        interval = statistics.mean(gap for _, _, gap in observed)
        if evidence is not None:
            evidence.append(
                {
                    "offered_share": offered,
                    "share": share,
                    "legs": sent,
                    "leg_tokens": leg_len,
                    "leg_prefill_s": leg_s,
                    "window_s": end - start,
                    "input_tokens": input_len,
                    "concurrency": cohort,
                    "tokens": tokens,
                    "interval_s": interval,
                    "intervals": observed,
                }
            )
        print(
            f"    share   u={offered:<4.2f} c={cohort:<3} measured u={share:.2f}, {sent} legs "
            f"-> {interval * 1000:6.2f} ms/token"
        )
        samples.append((share, interval))
    return samples
