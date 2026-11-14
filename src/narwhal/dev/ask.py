"""Send prompts through a launched instance's router and report what the client observed."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import lifecycle

REQUEST_TIMEOUT_S = 300.0
# The journal row of a request is written when its stream ends.
JOURNAL_WAIT_S = 2.0
# A request's row is among the last the journal holds.
JOURNAL_TAIL_BYTES = 1 << 20
PHASES = ("launched", "ready", "degraded")
PROMPTS = (
    "What is the capital of Japan?",
    "Explain in one sentence why the sky is blue.",
    "List three uses of a paperclip.",
    "What is 17 multiplied by 6?",
    "Give a synonym for the word quick.",
    "Describe the taste of a lemon in one sentence.",
    "Name two planets that have rings.",
    "What does a thermometer measure?",
    "Write a one-sentence weather report for a rainy day.",
    "Which is heavier, a kilogram of iron or a kilogram of feathers?",
    "Translate the word water into French.",
    "Why do people sleep? Answer in one sentence.",
)


@dataclass(frozen=True)
class Answer:
    ok: bool
    text: str = ""
    error: str = ""
    request_id: str | None = None
    ttft_s: float | None = None
    # Seconds per output token after the first chunk of output, which counts as one token.
    tpot_s: float | None = None
    output_tokens: int | None = None
    seconds: float = 0.0


def _body(model: str, prompt: str, max_tokens: int, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        **extra,
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _piece(chunk: dict[str, Any]) -> tuple[str, bool]:
    """A stream chunk's reply text, and whether it carries output of any kind."""
    text, output = "", False
    choices = chunk.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            text += content
        output = output or bool(content or delta.get("reasoning_content"))
    return text, output


async def ask(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], clock: Any = time.monotonic
) -> Answer:
    started = clock()
    first = last = None
    text, tokens, request_id = "", None, None
    try:
        async with client.stream("POST", url + "/v1/chat/completions", json=body) as response:
            request_id = response.headers.get("x-request-id")
            if response.status_code != 200:
                detail = (await response.aread()).decode(errors="replace")[:200]
                return Answer(False, error=f"HTTP {response.status_code}: {detail}")
            async for line in response.aiter_lines():
                if not line.startswith("data: {"):
                    continue
                chunk = json.loads(line[6:])
                if chunk.get("error"):
                    return Answer(False, error=json.dumps(chunk["error"])[:200])
                piece, output = _piece(chunk)
                text += piece
                if output:
                    last = clock()
                    if first is None:
                        first = last
                usage = chunk.get("usage")
                if isinstance(usage, dict) and type(usage.get("completion_tokens")) is int:
                    tokens = usage["completion_tokens"]
    except (httpx.HTTPError, ValueError) as exc:
        return Answer(False, error=f"{type(exc).__name__}: {exc}"[:200], request_id=request_id)
    ended = clock()
    interval = (
        (last - first) / (tokens - 1)
        if first is not None and last is not None and last > first and tokens and tokens > 1
        else None
    )
    return Answer(
        True,
        text=text,
        request_id=request_id,
        ttft_s=None if first is None else first - started,
        tpot_s=interval,
        output_tokens=tokens,
        seconds=ended - started,
    )


def percentile(values: list[float], fraction: float) -> float | None:
    """The nearest-rank percentile, None without values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _spread(values: list[float]) -> dict[str, float | None]:
    return {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def _rows(journal: Path) -> list[dict]:
    """The journal's last rows, newest first; a line still being written is skipped."""
    try:
        with journal.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - JOURNAL_TAIL_BYTES))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _legs(journal: Path, request_id: str | None) -> dict[str, Any]:
    """The engines that served a request, read from its journal row."""
    deadline = time.monotonic() + JOURNAL_WAIT_S
    while request_id is not None:
        for row in _rows(journal):
            if row.get("rid") == request_id:
                return {"prefill": row.get("prefill_iid"), "decode": row.get("decode_iid")}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return {"prefill": None, "decode": None}


async def _many(
    url: str, bodies: list[dict[str, Any]], concurrency: int, transport: Any
) -> tuple[list[Answer], float]:
    gate = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S,
        trust_env=False,
        transport=transport,
        # A request waiting for a pooled connection would count that wait in its first token.
        limits=httpx.Limits(max_connections=concurrency),
    ) as client:

        async def one(body: dict[str, Any]) -> Answer:
            async with gate:
                return await ask(client, url, body)

        started = time.monotonic()
        answers = await asyncio.gather(*(one(body) for body in bodies))
        return list(answers), time.monotonic() - started


def run(
    root: Path,
    *,
    prompt: str | None = None,
    repeat: int | None = None,
    concurrency: int = 1,
    max_tokens: int = 128,
    transport: Any = None,
) -> dict:
    if (prompt is None) == (repeat is None):
        raise ValueError("give one prompt, or --repeat for the built-in prompts")
    if (repeat is not None and repeat < 1) or concurrency < 1 or max_tokens < 1:
        raise ValueError("--repeat, --concurrency and --max-tokens must be positive")
    config = lifecycle.instance(root)
    state = lifecycle._read_state(root) if (root / "lifecycle.json").exists() else {}
    if state.get("phase") not in PHASES:
        raise ValueError(
            f"dev ask requires a launched instance; this one is {state.get('phase', 'stopped')}"
        )
    run_directory = Path(state["run"])
    model = lifecycle.read(run_directory / "fleet.json")["model"]
    extra = lifecycle.read(root / "template.json").get("request", {})
    url = config["router_url"]
    prompts = (
        [prompt] if prompt is not None else [PROMPTS[i % len(PROMPTS)] for i in range(repeat or 0)]
    )
    bodies = [_body(model, text, max_tokens, extra) for text in prompts]
    answers, seconds = asyncio.run(_many(url, bodies, concurrency, transport))
    if prompt is not None:
        answer = answers[0]
        if not answer.ok:
            raise lifecycle.LifecycleOperationError(f"the request failed: {answer.error}")
        return {
            "status": "answered",
            "reply": answer.text,
            "ttft_s": answer.ttft_s,
            "output_tokens": answer.output_tokens,
            "tokens_per_second": None if not answer.tpot_s else 1 / answer.tpot_s,
            "engines": _legs(run_directory / "journal.jsonl", answer.request_id),
        }
    served = [answer for answer in answers if answer.ok]
    failed = len(answers) - len(served)
    counts = [answer.output_tokens for answer in served if answer.output_tokens is not None]
    return {
        "status": "completed",
        "requests": len(answers),
        "failed": failed,
        "concurrency": concurrency,
        "seconds": seconds,
        "ttft_s": _spread([a.ttft_s for a in served if a.ttft_s is not None]),
        "tpot_s": _spread([a.tpot_s for a in served if a.tpot_s is not None]),
        "requests_per_second": len(served) / seconds if seconds > 0 else None,
        # Over the replies that reported a token count.
        "output_tokens_per_second": sum(counts) / seconds if counts and seconds > 0 else None,
        "replies_without_token_count": len(served) - len(counts),
        **({"errors": sorted({a.error for a in answers if not a.ok})[:5]} if failed else {}),
    }
