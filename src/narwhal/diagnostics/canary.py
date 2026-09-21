"""Low-rate exact-output canaries for serving correctness checks."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import secrets
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeGuard

import httpx

from ..contracts import CANARY, CANARY_CASES, STATE, validate_document, versioned
from ..engines.stream import event_choices, event_object
from ..engines.stream import token_ids as event_token_ids
from ..provenance import stamp_line


@dataclass(frozen=True)
class CanaryCase:
    """One prompt and its exact expected completion."""

    cid: str
    prompt: str
    expected: str
    expected_token_ids: tuple[int, ...]
    allowed_token_ids: tuple[int, ...]


@dataclass(frozen=True)
class StreamEvidence:
    """Transient completion evidence extracted from SSE frames."""

    text: str
    token_ids: tuple[int, ...]
    done: bool
    malformed: bool
    terminal_error: bool


@dataclass(frozen=True)
class CanaryOutcome:
    """Content-free retained result for one canary request."""

    seq: int
    canary_id: str
    scheduled_at: float
    started_at: float
    completed_at: float
    dispatch_delay_s: float
    ttft_s: float | None
    latency_s: float
    expected_tokens: int
    observed_tokens: int
    correct: bool
    status: str
    http_status: int | None = None
    observed_digest: str | None = None


@dataclass(frozen=True)
class ControlEvent:
    """Client-clock observation of a router or operator event."""

    event: str
    at: float
    source: str
    iid: str | None = None
    to: str | None = None


def load_cases(path: Path) -> tuple[str | None, list[CanaryCase]]:
    """Load and validate a canary case document."""
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("canary case file must be an object")
    validate_document(raw, CANARY_CASES)
    unknown = sorted(set(raw) - {"schema", "schema_version", "model", "cases"})
    if unknown:
        raise ValueError(f"unknown canary document field(s): {', '.join(unknown)}")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError("canary model must be a nonempty string")
    rows = raw.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("canary case file requires a nonempty cases list")

    cases = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"case {index} must be an object")
        unknown = sorted(
            set(row) - {"id", "prompt", "expected", "expected_token_ids", "allowed_token_ids"}
        )
        if unknown:
            raise ValueError(f"case {index} has unknown field(s): {', '.join(unknown)}")
        cid = row.get("id")
        prompt = row.get("prompt")
        expected = row.get("expected")
        expected_ids = row.get("expected_token_ids")
        allowed_ids = row.get("allowed_token_ids")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"case {index} requires a nonempty id")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError(f"case {cid} requires a nonempty prompt")
        if not isinstance(expected, str) or not expected:
            raise ValueError(f"case {cid} requires a nonempty expected completion")
        if not _token_ids(expected_ids):
            raise ValueError(f"case {cid} requires positive expected_token_ids")
        if not _token_ids(allowed_ids):
            raise ValueError(f"case {cid} requires positive allowed_token_ids")
        if len(set(allowed_ids)) != len(allowed_ids):
            raise ValueError(f"case {cid} allowed token ids must be unique")
        if not set(expected_ids) <= set(allowed_ids):
            raise ValueError(f"case {cid} expected tokens must be allowed")
        if not set(allowed_ids) - set(expected_ids):
            raise ValueError(f"case {cid} requires at least one distractor token")
        cases.append(
            CanaryCase(
                cid=cid,
                prompt=prompt,
                expected=expected,
                expected_token_ids=tuple(expected_ids),
                allowed_token_ids=tuple(allowed_ids),
            )
        )
    ids = [case.cid for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("canary case ids must be unique")
    return model, cases


def _token_ids(value: Any) -> TypeGuard[list[int]]:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0 for token in value
        )
    )


def parse_sse(lines: list[str]) -> StreamEvidence:
    """Extract completion text and token IDs from retained-in-memory SSE lines."""
    text: list[str] = []
    token_ids: list[int] = []
    done = False
    malformed = False
    terminal_error = False
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            row = event_object(line)
            if row is None:
                malformed = True
                continue
            if row.get("error") is not None:
                terminal_error = True
                continue
            choices = event_choices(row)
        except ValueError:
            malformed = True
            continue
        for choice in choices:
            piece = choice.get("text")
            if piece is None:
                piece = (choice.get("delta") or {}).get("content")
            if piece:
                if not isinstance(piece, str):
                    malformed = True
                else:
                    text.append(piece)
            ids = event_token_ids([choice])
            if ids is None:
                malformed = True
            else:
                token_ids.extend(ids)
    return StreamEvidence("".join(text), tuple(token_ids), done, malformed, terminal_error)


def classify(case: CanaryCase, evidence: StreamEvidence) -> str:
    """Classify exact, wrong, truncated, malformed, or terminal output."""
    if evidence.terminal_error:
        return "terminal_error"
    if evidence.malformed:
        return "malformed"
    exact = (
        evidence.text == case.expected
        and evidence.token_ids == case.expected_token_ids
        and evidence.done
    )
    if exact:
        return "correct"
    text_prefix = case.expected.startswith(evidence.text)
    token_prefix = case.expected_token_ids[: len(evidence.token_ids)] == evidence.token_ids
    if (
        text_prefix
        and token_prefix
        and (
            not evidence.done
            or len(evidence.token_ids) < len(case.expected_token_ids)
            or len(evidence.text) < len(case.expected)
        )
    ):
        return "truncated"
    return "wrong"


async def request_case(
    client: httpx.AsyncClient,
    *,
    base: str,
    model: str,
    case: CanaryCase,
    seq: int,
    scheduled_at: float,
    timeout_s: float,
    digest_key: bytes | None,
) -> CanaryOutcome:
    """Send one exact-output canary and retain no prompt or response content."""
    started_at = time.time()
    started = time.monotonic()
    first: float | None = None
    lines: list[str] = []
    http_status: int | None = None
    status = "transport_error"
    observed_tokens = 0
    observed_digest: str | None = None
    body = {
        "model": model,
        "prompt": case.prompt,
        "max_tokens": len(case.expected_token_ids),
        "min_tokens": len(case.expected_token_ids),
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
        "return_token_ids": True,
        "allowed_token_ids": list(case.allowed_token_ids),
    }
    try:
        async with asyncio.timeout(timeout_s):
            async with client.stream("POST", f"{base.rstrip('/')}/v1/completions", json=body) as r:
                http_status = r.status_code
                if r.status_code != 200:
                    status = "http_error"
                    await r.aread()
                else:
                    async for line in r.aiter_lines():
                        lines.append(line)
                        if first is None and _line_has_output(line):
                            first = time.monotonic()
                    evidence = parse_sse(lines)
                    status = classify(case, evidence)
                    observed_tokens = len(evidence.token_ids)
                    if digest_key is not None:
                        observed_digest = _response_digest(digest_key, evidence.text)
    except TimeoutError:
        status = "timeout"
        evidence = parse_sse(lines)
        observed_tokens = len(evidence.token_ids)
        if digest_key is not None:
            observed_digest = _response_digest(digest_key, evidence.text)
    except httpx.HTTPError:
        status = "transport_error"
        evidence = parse_sse(lines)
        observed_tokens = len(evidence.token_ids)
        if digest_key is not None:
            observed_digest = _response_digest(digest_key, evidence.text)
    completed = time.monotonic()
    completed_at = time.time()
    return CanaryOutcome(
        seq=seq,
        canary_id=case.cid,
        scheduled_at=scheduled_at,
        started_at=started_at,
        completed_at=completed_at,
        dispatch_delay_s=max(0.0, started_at - scheduled_at),
        ttft_s=(first - started) if first is not None else None,
        latency_s=completed - started,
        expected_tokens=len(case.expected_token_ids),
        observed_tokens=observed_tokens,
        correct=status == "correct",
        status=status,
        http_status=http_status,
        observed_digest=observed_digest,
    )


def _line_has_output(line: str) -> bool:
    evidence = parse_sse([line])
    return bool(evidence.text or evidence.token_ids)


def _response_digest(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def state_transitions(
    previous: dict[str, Any], current: dict[str, Any], observed_at: float
) -> list[ControlEvent]:
    """Describe role, ejection, and retry-quarantine changes between snapshots."""
    events: list[ControlEvent] = []
    previous_roles = _roles(previous)
    current_roles = _roles(current)
    events.extend(
        ControlEvent("role_change", observed_at, "router_state", iid, current_roles[iid])
        for iid in sorted(previous_roles.keys() & current_roles.keys())
        if previous_roles[iid] != current_roles[iid]
    )
    for field, entered, left in (
        ("ejected", "engine_ejected", "engine_readmitted"),
        ("quarantined", "failure_quarantine", "failure_quarantine_cleared"),
    ):
        before = set(previous.get(field) or [])
        after = set(current.get(field) or [])
        events.extend(
            ControlEvent(entered, observed_at, "router_state", iid)
            for iid in sorted(after - before)
        )
        events.extend(
            ControlEvent(left, observed_at, "router_state", iid) for iid in sorted(before - after)
        )
    return events


def _roles(state: dict[str, Any]) -> dict[str, str]:
    pools = state.get("pools") or {}
    return {str(iid): role for role in ("prefill", "decode") for iid in (pools.get(role) or [])}


async def watch_state(
    client: httpx.AsyncClient,
    base: str,
    interval_s: float,
    stop: asyncio.Event,
    events: list[ControlEvent],
) -> None:
    """Poll router state and timestamp transitions on the client clock."""
    previous: dict[str, Any] | None = None
    while not stop.is_set():
        try:
            response = await client.get(f"{base.rstrip('/')}/narwhal/state", timeout=5.0)
            response.raise_for_status()
            current = response.json()
            if isinstance(current, dict):
                validate_document(current, STATE)
                if previous is not None:
                    events.extend(state_transitions(previous, current, time.time()))
                previous = current
        except (httpx.HTTPError, ValueError):
            pass
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


async def drive(
    *,
    base: str,
    model: str,
    cases: list[CanaryCase],
    rate: float,
    duration_s: float,
    timeout_s: float,
    state_poll_s: float,
    digest: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[CanaryOutcome], list[ControlEvent], float, float]:
    """Run a fixed-rate canary stream and concurrent router-state watcher."""
    started_at = time.time()
    started = time.monotonic()
    stop = asyncio.Event()
    events: list[ControlEvent] = []
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=8)
    timeout = httpx.Timeout(max(timeout_s, 5.0), connect=min(timeout_s, 10.0))
    digest_key = secrets.token_bytes(32) if digest else None
    async with httpx.AsyncClient(transport=transport, limits=limits, timeout=timeout) as client:
        watcher = (
            asyncio.create_task(watch_state(client, base, state_poll_s, stop, events))
            if state_poll_s > 0
            else None
        )
        tasks = []
        interval = 1.0 / rate
        count = math.ceil(duration_s * rate)
        for seq in range(count):
            offset = seq * interval
            delay = offset - (time.monotonic() - started)
            if delay > 0:
                await asyncio.sleep(delay)
            case = cases[seq % len(cases)]
            tasks.append(
                asyncio.create_task(
                    request_case(
                        client,
                        base=base,
                        model=model,
                        case=case,
                        seq=seq,
                        scheduled_at=started_at + offset,
                        timeout_s=timeout_s,
                        digest_key=digest_key,
                    )
                )
            )
        outcomes = list(await asyncio.gather(*tasks))
        stop.set()
        if watcher is not None:
            await watcher
    return outcomes, events, started_at, time.time()


def load_markers(path: Path, started_at: float, completed_at: float) -> list[ControlEvent]:
    """Read bounded operator markers that overlap this canary run."""
    events: list[ControlEvent] = []
    if not path.exists():
        return events
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            at = float(row["at"])
            event = row["event"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, str) or not event or not started_at <= at <= completed_at:
            continue
        iid = row.get("iid")
        events.append(
            ControlEvent(
                event=event,
                at=at,
                source="operator_marker",
                iid=str(iid) if iid is not None else None,
            )
        )
    return events


def percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(
    outcomes: list[CanaryOutcome], events: list[ControlEvent], window_s: float
) -> dict[str, Any]:
    """Score correctness, latency, terminal outcomes, and event windows."""
    ttft = [row.ttft_s for row in outcomes if row.ttft_s is not None]
    statuses = Counter(row.status for row in outcomes)
    terminal = sum(
        statuses[status]
        for status in ("timeout", "http_error", "transport_error", "terminal_error")
    )
    windows = []
    for event in sorted(events, key=lambda row: row.at):
        nearby = [row for row in outcomes if abs(row.started_at - event.at) <= window_s]
        nearby_ttft = [row.ttft_s for row in nearby if row.ttft_s is not None]
        windows.append(
            {
                **asdict(event),
                "window_s": window_s,
                "requests": len(nearby),
                "correct": sum(row.correct for row in nearby),
                "failures": sum(not row.correct for row in nearby),
                "ttft_p50_s": percentile(nearby_ttft, 0.50),
                "ttft_p95_s": percentile(nearby_ttft, 0.95),
            }
        )
    return {
        "kind": "canary_summary",
        "requests": len(outcomes),
        "correct": sum(row.correct for row in outcomes),
        "correctness_failures": sum(not row.correct for row in outcomes),
        "terminal_errors": terminal,
        "statuses": dict(sorted(statuses.items())),
        "ttft_p50_s": percentile(ttft, 0.50),
        "ttft_p95_s": percentile(ttft, 0.95),
        "ttft_p99_s": percentile(ttft, 0.99),
        "event_windows": windows,
    }


def write_artifact(
    path: Path,
    *,
    cases_path: Path,
    case_ids: list[str],
    model: str,
    rate: float,
    duration_s: float,
    timeout_s: float,
    outcomes: list[CanaryOutcome],
    events: list[ControlEvent],
    summary: dict[str, Any],
) -> None:
    """Write content-free canary rows and their scoring envelope."""
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    with path.open("w") as output:
        output.write(stamp_line(contract=CANARY))
        output.write(
            json.dumps(
                {
                    "meta": versioned(
                        CANARY,
                        {
                            "kind": "narwhal-canary",
                            "model": model,
                            "rate": rate,
                            "duration_s": duration_s,
                            "timeout_s": timeout_s,
                            "cases": {"name": cases_path.name, "sha256": digest, "ids": case_ids},
                        },
                    )
                }
            )
            + "\n"
        )
        for row in outcomes:
            output.write(json.dumps(versioned(CANARY, {"kind": "canary", **asdict(row)})) + "\n")
        for event in events:
            output.write(
                json.dumps(versioned(CANARY, {"kind": "control_event", **asdict(event)})) + "\n"
            )
        output.write(json.dumps(versioned(CANARY, summary)) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Run the correctness-canary CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="router base URL")
    parser.add_argument("--model", default="", help="served model; defaults to the case file")
    parser.add_argument("--cases", required=True, help="exact-output case JSON")
    parser.add_argument("--rate", type=float, default=0.1, help="canary requests per second")
    parser.add_argument("--duration", type=float, required=True, help="arrival duration in seconds")
    parser.add_argument("--timeout", type=float, default=30.0, help="whole-request timeout")
    parser.add_argument(
        "--state-poll", type=float, default=1.0, help="router state poll interval; 0 disables"
    )
    parser.add_argument(
        "--event-window", type=float, default=15.0, help="seconds either side of an event"
    )
    parser.add_argument("--markers", default="", help="optional operator marker JSONL")
    parser.add_argument("--out", required=True, help="write content-free JSONL here")
    parser.add_argument(
        "--digest", action="store_true", help="retain run-keyed HMAC-SHA-256 of each completion"
    )
    args = parser.parse_args(argv)
    for name in ("rate", "duration", "timeout", "event_window"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.state_poll < 0:
        parser.error("--state-poll cannot be negative")

    cases_path = Path(args.cases)
    try:
        case_model, cases = load_cases(cases_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"canary setup failed: {exc}", file=sys.stderr)
        return 2
    model = args.model or case_model
    if not model:
        parser.error("--model is required when the case file has no model")

    outcomes, events, started_at, completed_at = asyncio.run(
        drive(
            base=args.base,
            model=model,
            cases=cases,
            rate=args.rate,
            duration_s=args.duration,
            timeout_s=args.timeout,
            state_poll_s=args.state_poll,
            digest=args.digest,
        )
    )
    if args.markers:
        events.extend(load_markers(Path(args.markers), started_at, completed_at))
    summary = summarize(outcomes, events, args.event_window)
    write_artifact(
        Path(args.out),
        cases_path=cases_path,
        case_ids=[case.cid for case in cases],
        model=model,
        rate=args.rate,
        duration_s=args.duration,
        timeout_s=args.timeout,
        outcomes=outcomes,
        events=events,
        summary=summary,
    )
    p95 = summary["ttft_p95_s"]
    shown_p95 = f"{p95:.4f}s" if p95 is not None else "unseen"
    print(
        f"canaries: {summary['correct']}/{summary['requests']} correct, "
        f"{summary['terminal_errors']} terminal errors, "
        f"TTFT p95 {shown_p95}"
    )
    print(f"artifact: {args.out}")
    return 1 if summary["correctness_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
