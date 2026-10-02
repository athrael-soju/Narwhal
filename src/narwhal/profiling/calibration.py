"""Measure crossed-handoff first-token latency without the serving deadline."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import Counter
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeGuard, TypeVar
from uuid import uuid4

import httpx

from ..config import FleetConfig
from ..engines.client import EngineClient, EngineError, first_output_timeout
from ..engines.connector import PrefillResult
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import EngineDialect
from ..engines.dialect import lookup as lookup_dialect
from ..engines.stream import sse_token_bearing
from ..engines.validation import validation_pairs
from .engine_io import engine_context_limit, make_prompt
from .generation import read_generation
from .live import device_key

SCHEMA = "narwhal.first-token-calibration"
RECALIBRATE = "recalibrate with narwhal-check --calibrate-first-token"
_ATTEMPT_ERRORS = (EngineError, httpx.HTTPError, TimeoutError, ValueError, RuntimeError)

T = TypeVar("T")
Pair = tuple[str, str]
EngineLabel = Literal["measured", "reused"]


def _group_key(row: Any) -> tuple[str, str, int] | None:
    if not isinstance(row, dict):
        return None
    source = row.get("producer")
    destination = row.get("consumer")
    target = row.get("target_input_tokens")
    if not isinstance(source, str) or not isinstance(destination, str) or type(target) is not int:
        return None
    return source, destination, target


def _finite(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def candidate_deadline(samples: list[float]) -> tuple[float, float, float]:
    """Return nearest-rank p99, maximum, and the documented guarded candidate."""
    if not samples or any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError("candidate deadline requires finite completed timings")
    ordered = sorted(samples)
    p99 = ordered[math.ceil(0.99 * len(ordered)) - 1]
    maximum = ordered[-1]
    return p99, maximum, max(maximum, 1.2 * p99) + 0.5


def calibration_rounds(pairs: list[Pair], slots: Mapping[str, str]) -> list[list[Pair]]:
    """Split pairs into rounds in which each device slot produces and consumes at most once.

    `slots` maps each engine to its device slot. The round count equals the largest number
    of pairs one slot produces or consumes.
    """
    produced: dict[str, dict[int, Pair]] = {}
    consumed: dict[str, dict[int, Pair]] = {}
    colour: dict[Pair, int] = {}
    for pair in pairs:
        at_producer = produced.setdefault(slots[pair[0]], {})
        at_consumer = consumed.setdefault(slots[pair[1]], {})
        free = min(set(range(len(at_producer) + 1)) - at_producer.keys())
        if free in at_consumer:
            other = min(set(range(len(at_consumer) + 1)) - at_consumer.keys())
            # Swap `free` and `other` along the alternating path from the consumer slot.
            path: list[Pair] = []
            consumer_side, slot, wanted = True, slots[pair[1]], free
            while (edge := (consumed if consumer_side else produced)[slot].get(wanted)) is not None:
                path.append(edge)
                slot = slots[edge[0]] if consumer_side else slots[edge[1]]
                consumer_side = not consumer_side
                wanted = other if wanted == free else free
            for edge in path:
                del produced[slots[edge[0]]][colour[edge]]
                del consumed[slots[edge[1]]][colour[edge]]
            for edge in path:
                colour[edge] = other if colour[edge] == free else free
                produced[slots[edge[0]]][colour[edge]] = edge
                consumed[slots[edge[1]]][colour[edge]] = edge
        colour[pair] = free
        at_producer[free] = pair
        at_consumer[free] = pair
    rounds: dict[int, list[Pair]] = {}
    for pair in pairs:
        rounds.setdefault(colour[pair], []).append(pair)
    return [rounds[index] for index in sorted(rounds)]


def evidence_problems(cfg: FleetConfig, document: dict[str, Any]) -> list[str]:
    """Return mismatches between a calibration document and this fleet's configuration."""
    problems: list[str] = []
    if document.get("schema") != SCHEMA or document.get("schema_version") != 1:
        return ["first-token calibration has an unknown schema or version"]
    if document.get("status") != "complete":
        problems.append("first-token calibration has failed, expired, or insufficient probes")
    problems.extend(
        f"first-token calibration {name} must be an empty list"
        for name in ("changed_generations", "generation_errors")
        if document.get(name) != []
    )
    if document.get("model") != cfg.model:
        problems.append("first-token calibration model differs from the fleet")
    contract = cfg.engine_contract.fingerprint() if cfg.engine_contract else None
    if document.get("contract_fingerprint") != contract:
        problems.append("first-token calibration engine contract differs from the fleet")
    expected = {spec.iid: spec.url for spec in cfg.engines}
    if document.get("engine_urls") != expected:
        problems.append("first-token calibration engine addresses differ from the fleet")
    captured = document.get("captured_at_unix")
    if not _finite(captured) or captured <= 0:
        problems.append("first-token calibration has no valid capture time")
    starts = document.get("process_starts")
    if (
        not isinstance(starts, dict)
        or starts.keys() != expected.keys()
        or not all(_finite(start) and start > 0 for start in starts.values())
    ):
        problems.append(
            f"first-token calibration lacks a process start for each engine; {RECALIBRATE}"
        )
    duration = document.get("duration_s")
    if not _finite(duration) or duration < 0:
        problems.append(f"first-token calibration lacks its run duration; {RECALIBRATE}")
    lengths = document.get("input_tokens")
    repeats = document.get("samples_per_group")
    if (
        not isinstance(lengths, list)
        or not lengths
        or any(type(value) is not int or value < 1 for value in lengths)
        or len(set(lengths)) != len(lengths)
        or type(repeats) is not int
        or repeats < 100
    ):
        problems.append("first-token calibration lacks at least 100 samples per input length")
        return problems
    expected_groups = {
        (src, dst, length)
        for src, dst in validation_pairs(cfg.engines, mesh=True)
        for length in lengths
    }
    groups = document.get("groups")
    attempts = document.get("attempts")
    if not isinstance(groups, list) or not isinstance(attempts, list):
        problems.append("first-token calibration has no group and raw-attempt records")
        return problems
    if any(
        isinstance(row, dict)
        and (
            "round" not in row
            or (row["round"] is not None and (type(row["round"]) is not int or row["round"] < 1))
        )
        for row in attempts
    ):
        problems.append(f"first-token calibration attempts lack round records; {RECALIBRATE}")
    completed = Counter(
        _group_key(row)
        for row in attempts
        if isinstance(row, dict) and row.get("status") == "completed"
    )
    attempted = Counter(_group_key(row) for row in attempts)
    if None in attempted:
        problems.append("first-token calibration has malformed raw attempts")
    if set(attempted) - {None} != expected_groups:
        problems.append("first-token calibration raw attempts do not cover the expected groups")
    timings: dict[tuple[str, str, int], list[float]] = {}
    attempt_indices: dict[tuple[str, str, int], set[int]] = {}
    for attempt in attempts:
        key = _group_key(attempt)
        if key is None or attempt.get("status") != "completed":
            continue
        index = attempt.get("attempt")
        if type(index) is not int or not 1 <= index <= repeats:
            problems.append(f"first-token calibration group {key} has an invalid attempt index")
        else:
            attempt_indices.setdefault(key, set()).add(index)
        elapsed = attempt.get("first_token_seconds")
        if not _finite(elapsed) or elapsed < 0:
            problems.append(f"first-token calibration group {key} has invalid raw timing")
            continue
        timings.setdefault(key, []).append(elapsed)
    actual_groups = set()
    group_candidates: list[float] = []
    for row in groups:
        if not isinstance(row, dict):
            problems.append("first-token calibration has a malformed group")
            continue
        key = _group_key(row)
        if key is None:
            problems.append("first-token calibration has a malformed group key")
            continue
        actual_groups.add(key)
        if (
            row.get("failed") != 0
            or row.get("completed") != repeats
            or completed[key] != repeats
            or attempted[key] != repeats
        ):
            problems.append(f"first-token calibration group {key} has incomplete attempts")
        if len(attempt_indices.get(key, set())) != repeats:
            problems.append(
                f"first-token calibration group {key} lacks distinct attempt indices 1..{repeats}"
            )
        if timings.get(key):
            p99, maximum, calculated = candidate_deadline(timings[key])
            for name, expected_value in (
                ("p99_seconds", p99),
                ("maximum_seconds", maximum),
                ("candidate_deadline_s", calculated),
            ):
                observed = row.get(name)
                if not isinstance(observed, (int, float)) or not math.isclose(
                    observed, expected_value, rel_tol=1e-9, abs_tol=1e-9
                ):
                    problems.append(f"first-token calibration group {key} has incorrect {name}")
        value = row.get("candidate_deadline_s")
        if _finite(value) and value > 0:
            group_candidates.append(value)
        else:
            problems.append(f"first-token calibration group {key} has no candidate deadline")
    if actual_groups != expected_groups or len(groups) != len(expected_groups):
        problems.append(
            "first-token calibration does not cover every directed pair and input length"
        )
    candidate = document.get("candidate_deadline_s")
    if not _finite(candidate) or candidate <= 0:
        problems.append("first-token calibration has no valid candidate deadline")
    elif cfg.first_token_timeout_s <= candidate:
        problems.append(
            f"engine.first_token_timeout_s {cfg.first_token_timeout_s:g}s is not above "
            f"measured calibration candidate {candidate:g}s"
        )
    if group_candidates and candidate != max(group_candidates):
        problems.append("first-token calibration candidate differs from its measured groups")
    return problems


def _labels(calibrated: Mapping[str, float], live: Mapping[str, float]) -> dict[str, EngineLabel]:
    return {
        iid: "measured" if start == calibrated[iid] else "reused" for iid, start in live.items()
    }


def _status(engines: Mapping[str, EngineLabel]) -> EngineLabel:
    return "reused" if "reused" in engines.values() else "measured"


@dataclass(frozen=True)
class CalibrationCheck:
    """The configured first-token calibration checked against the live engines.

    An engine is `measured` while it runs the process the calibration timed and `reused`
    after a relaunch with an unchanged generation digest.
    """

    status: Literal["uncalibrated", "rejected", "measured", "reused"]
    problems: tuple[str, ...] = ()
    path: Path | None = None
    captured_at_unix: float | None = None
    candidate_deadline_s: float | None = None
    engines: dict[str, EngineLabel] = field(default_factory=dict)
    process_starts: dict[str, float] = field(default_factory=dict)

    def at_starts(self, starts: Mapping[str, float]) -> CalibrationCheck:
        """Return the check with each engine in `starts` labelled by that process start."""
        if not self.engines:
            return self
        engines = {**self.engines, **_labels(self.process_starts, starts)}
        return replace(self, status=_status(engines), engines=engines)

    def view(self) -> dict[str, Any]:
        """Return the status, capture time, candidate deadline and engine labels."""
        return {
            "status": self.status,
            "captured_at_unix": self.captured_at_unix,
            "candidate_deadline_s": self.candidate_deadline_s,
            "engines": dict(self.engines),
        }

    def summary(self, deadline_s: float) -> str:
        """Describe a measured or reused calibration against the configured deadline."""
        limits = f"candidate {self.candidate_deadline_s:.3f}s, deadline {deadline_s:g}s"
        reused = [iid for iid, label in self.engines.items() if label == "reused"]
        if reused:
            return (
                f"first-token calibration reused for {', '.join(reused)}: "
                f"launch unchanged since capture; {limits}"
            )
        return f"first-token calibration measured on the running engines: {limits}"


async def verify_calibration(
    cfg: FleetConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> CalibrationCheck:
    """Check saved timings against the live engine generations and label each engine."""
    path = cfg.first_token_calibration_path
    if path is None:
        return CalibrationCheck("uncalibrated")
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return CalibrationCheck(
            "rejected", (f"first-token calibration {path} is unreadable: {exc}",), path
        )
    if not isinstance(document, dict):
        return CalibrationCheck(
            "rejected", (f"first-token calibration {path} must be a JSON object",), path
        )
    problems = evidence_problems(cfg, document)
    saved = document.get("generations")
    if not isinstance(saved, dict):
        problems.append("first-token calibration has no process-generation evidence")
        return CalibrationCheck("rejected", tuple(problems), path)
    live: dict[str, float] = {}
    for spec in cfg.engines:
        try:
            current = await read_generation(
                spec,
                cfg.engine_contract,
                timeout_s=cfg.health_timeout_s,
                headers=cfg.engine_headers(),
                transport=transport,
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"{spec.iid} calibration generation unreadable: {exc}")
            continue
        if saved.get(spec.iid) != current.digest:
            problems.append(f"{spec.iid} process differs from first-token calibration")
        live[spec.iid] = current.process_start_time_seconds
    if problems:
        return CalibrationCheck("rejected", tuple(problems), path)
    engines = _labels(document["process_starts"], live)
    return CalibrationCheck(
        _status(engines),
        path=path,
        captured_at_unix=document["captured_at_unix"],
        candidate_deadline_s=document["candidate_deadline_s"],
        engines=engines,
        process_starts=dict(document["process_starts"]),
    )


async def _together(coroutines: list[Coroutine[Any, Any, T]]) -> list[T]:
    """Await `coroutines` concurrently; cancel the rest and re-raise when one raises."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass
class _Attempt:
    """One crossed handoff and its raw row."""

    src: str
    dst: str
    target: int
    output_tokens: int
    context_limit: int
    row: dict[str, Any]
    phase: str = "sizing"
    elapsed: float = 0.0
    body: dict[str, Any] = field(default_factory=dict)
    handoff: PrefillResult | None = None

    def fail(self, exc: Exception, deadline: asyncio.Timeout) -> None:
        """Record the failure status of the current phase."""
        expired = first_output_timeout(exc) and exc.status == 504
        self.row.update(
            status=(
                "request_expired"
                if deadline.expired()
                else "observation_expired"
                if expired
                else "failed_sizing"
                if self.phase == "sizing"
                else "failed_prefill"
                if self.phase == "prefill"
                else "failed_transfer"
            ),
            error=f"{type(exc).__name__}: {exc}",
        )


class _Lockstep:
    """Run calibration steps: every prefill, a barrier, every decode, a barrier."""

    def __init__(
        self,
        cfg: FleetConfig,
        client: EngineClient,
        sizing: httpx.AsyncClient,
        dialect: EngineDialect,
        observation_timeout_s: float,
        context_limits: dict[str, int],
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.sizing = sizing
        self.dialect = dialect
        self.observation_timeout_s = observation_timeout_s
        self.context_limits = context_limits
        self.urls = {spec.iid: spec.url for spec in cfg.engines}
        self.rows: dict[tuple[str, str, int, int], dict[str, Any]] = {}

    async def step(
        self, pairs: list[Pair], target: int, index: int, round_number: int | None
    ) -> None:
        """Measure attempt `index` of each pair at `target` input tokens."""
        attempts = []
        for src, dst in pairs:
            context_limit = min(self.context_limits[src], self.context_limits[dst])
            output_tokens = min(4, context_limit - target)
            row: dict[str, Any] = {
                "producer": src,
                "consumer": dst,
                "target_input_tokens": target,
                "requested_output_tokens": output_tokens,
                "attempt": index,
                "round": round_number,
            }
            self.rows[(src, dst, target, index)] = row
            attempts.append(_Attempt(src, dst, target, output_tokens, context_limit, row))
        prefilled = await _together([self._prefill(attempt) for attempt in attempts])
        await _together(
            [
                self._decode(attempt)
                for attempt, ready in zip(attempts, prefilled, strict=True)
                if ready
            ]
        )

    async def _prefill(self, attempt: _Attempt) -> bool:
        """Size a fresh prompt and prefill it on the producer; return whether it handed off."""
        began = time.monotonic()
        deadline = asyncio.timeout(self.cfg.request_timeout_s)
        try:
            async with deadline:
                async with asyncio.timeout(self.observation_timeout_s):
                    prompt, actual = await make_prompt(
                        self.sizing,
                        self.urls[attempt.src],
                        self.cfg.model,
                        attempt.target,
                        self.dialect,
                        timeout_s=self.observation_timeout_s,
                        prefix=uuid4().hex + " ",
                        max_input_tokens=attempt.target,
                    )
                attempt.row["actual_input_tokens"] = actual
                if actual + attempt.output_tokens > attempt.context_limit:
                    raise ValueError(
                        f"sized input {actual} plus {attempt.output_tokens} output tokens "
                        f"exceed live context limit {attempt.context_limit}"
                    )
                attempt.body = {
                    "model": self.cfg.model,
                    "prompt": prompt,
                    "max_tokens": attempt.output_tokens,
                    "temperature": 0.0,
                    **self.dialect.decode_probe_extras(attempt.output_tokens),
                }
                attempt.phase = "prefill"
                started = time.monotonic()
                attempt.handoff = await self.client.prefill(
                    self.urls[attempt.src], "/v1/completions", attempt.body, {}
                )
                attempt.row["prefill_seconds"] = time.monotonic() - started
        except _ATTEMPT_ERRORS as exc:
            attempt.fail(exc, deadline)
            return False
        attempt.elapsed = time.monotonic() - began
        return True

    async def _decode(self, attempt: _Attempt) -> None:
        """Time the first generated token on the consumer within the attempt's remaining budget."""
        attempt.phase = "decode"
        deadline = asyncio.timeout(self.cfg.request_timeout_s - attempt.elapsed)
        try:
            async with deadline:
                began = time.monotonic()
                first: float | None = None
                async for line in self.client.decode(
                    self.urls[attempt.dst],
                    "/v1/completions",
                    attempt.body,
                    {},
                    attempt.handoff,
                    first_token_timeout_s=self.observation_timeout_s,
                ):
                    if first is None and sse_token_bearing(line, self.dialect):
                        first = time.monotonic() - began
                        attempt.row["first_token_seconds"] = first
                if first is None:
                    raise ValueError("decode completed without a generated token")
                attempt.row.update(status="completed", first_token_seconds=first)
        except _ATTEMPT_ERRORS as exc:
            attempt.fail(exc, deadline)


async def calibrate(
    cfg: FleetConfig,
    *,
    input_tokens: tuple[int, ...],
    samples_per_group: int,
    observation_timeout_s: float,
    out: Path,
) -> int:
    """Write fresh process-bound samples and return zero only for complete evidence.

    Sweep 1 measures each group alone. Later sweeps run each round as one lockstep step.
    """
    if not input_tokens or any(value < 1 for value in input_tokens):
        raise ValueError("--input-tokens requires positive token counts")
    if len(set(input_tokens)) != len(input_tokens):
        raise ValueError("--input-tokens contains duplicate counts")
    if samples_per_group < 1:
        raise ValueError("--samples must be positive")
    if not cfg.first_token_timeout_s < observation_timeout_s <= cfg.request_timeout_s:
        raise ValueError(
            "--observation-timeout-s must exceed engine.first_token_timeout_s "
            "and stay within serving.request_timeout_s"
        )
    if out.exists():
        raise ValueError(f"calibration output already exists: {out}")
    pairs = validation_pairs(cfg.engines, mesh=True)
    if not pairs:
        raise ValueError("first-token calibration requires a role-permitted engine pair")
    rounds = calibration_rounds(pairs, {spec.iid: device_key(spec) for spec in cfg.engines})
    groups = [
        (src, dst, target)
        for source in sorted({src for src, _ in pairs})
        for target in input_tokens
        for src, dst in pairs
        if src == source
    ]
    by_id = {spec.iid: spec for spec in cfg.engines}
    dialect = lookup_dialect(cfg.dialect)
    if dialect.tokenize_path is None:
        raise ValueError("first-token calibration requires an exact-count tokenizer route")
    began = time.monotonic()
    started = {
        spec.iid: await read_generation(
            spec,
            cfg.engine_contract,
            timeout_s=observation_timeout_s,
            headers=cfg.engine_headers(),
        )
        for spec in cfg.engines
    }
    generations = {iid: generation.digest for iid, generation in started.items()}
    process_starts = {
        iid: generation.process_start_time_seconds for iid, generation in started.items()
    }
    client = EngineClient(
        timeout_s=cfg.request_timeout_s,
        prefill_timeout_s=cfg.prefill_timeout_s,
        read_timeout_s=cfg.decode_read_timeout_s,
        max_connections=cfg.max_connections,
        control_connections=cfg.resolved_control_connections(),
        pool_timeout_s=cfg.pool_timeout_s,
        connect_timeout_s=cfg.connect_timeout_s,
        health_timeout_s=cfg.health_timeout_s,
        kv=lookup_connector(cfg.connector),
        dialect=dialect,
        model=cfg.model,
        engine_api_key=cfg.resolve_engine_key(),
    )
    try:
        async with httpx.AsyncClient(
            timeout=observation_timeout_s, headers=cfg.engine_headers()
        ) as sizing:
            context_limits: dict[str, int] = {}
            for iid in sorted({iid for pair in pairs for iid in pair}):
                spec = by_id[iid]
                async with asyncio.timeout(observation_timeout_s):
                    context_limit = await engine_context_limit(
                        sizing, spec.url, cfg.model, dialect, observation_timeout_s
                    )
                for target in input_tokens:
                    if target + 1 > context_limit:
                        raise ValueError(
                            f"{iid}: requested {target} input tokens plus one output token "
                            f"exceed live context limit {context_limit}"
                        )
                context_limits[iid] = context_limit
            lockstep = _Lockstep(
                cfg, client, sizing, dialect, observation_timeout_s, context_limits
            )
            print(
                f"calibration groups: {len(groups)}; concurrent rounds per input length: "
                f"{len(rounds)}; sweep 1 runs each group alone"
            )
            for src, dst, target in groups:
                await lockstep.step([(src, dst)], target, 1, None)
            for index in range(2, samples_per_group + 1):
                for target in input_tokens:
                    for number, members in enumerate(rounds, 1):
                        await lockstep.step(members, target, index, number)
    finally:
        await client.aclose()
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for src, dst, target in groups:
        group_rows = [
            lockstep.rows[(src, dst, target, index)] for index in range(1, samples_per_group + 1)
        ]
        completed = [row for row in group_rows if row["status"] == "completed"]
        timings = [row["first_token_seconds"] for row in completed]
        actual_lengths = [row["actual_input_tokens"] for row in completed]
        failures = len(group_rows) - len(completed)
        summary: dict[str, Any] = {
            "producer": src,
            "consumer": dst,
            "target_input_tokens": target,
            "actual_input_tokens_min": min(actual_lengths) if actual_lengths else None,
            "actual_input_tokens_max": max(actual_lengths) if actual_lengths else None,
            "completed": len(timings),
            "failed": failures,
        }
        if timings:
            p99, maximum, candidate = candidate_deadline(timings)
            summary.update(
                p99_seconds=p99,
                maximum_seconds=maximum,
                candidate_deadline_s=candidate,
            )
        summaries.append(summary)
        rows.extend(group_rows)
        print(
            f"{src} -> {dst}, target {target} tokens: "
            f"{len(timings)}/{samples_per_group} completed, {failures} failed"
        )
    changed: list[str] = []
    generation_errors: list[str] = []
    for spec in cfg.engines:
        try:
            current = await read_generation(
                spec,
                cfg.engine_contract,
                timeout_s=observation_timeout_s,
                headers=cfg.engine_headers(),
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            generation_errors.append(f"{spec.iid}: {type(exc).__name__}: {exc}")
        else:
            if current.process_digest != started[spec.iid].process_digest:
                changed.append(spec.iid)
    complete = (
        samples_per_group >= 100
        and not changed
        and not generation_errors
        and all(row["failed"] == 0 and row["completed"] == samples_per_group for row in summaries)
    )
    candidates = [row["candidate_deadline_s"] for row in summaries if "candidate_deadline_s" in row]
    if not candidates or max(candidates) >= cfg.request_timeout_s:
        complete = False
    duration_s = time.monotonic() - began
    document = {
        "schema": SCHEMA,
        "schema_version": 1,
        "captured_at_unix": time.time(),
        "duration_s": duration_s,
        "status": "complete" if complete else "incomplete",
        "model": cfg.model,
        "contract_fingerprint": cfg.engine_contract.fingerprint() if cfg.engine_contract else None,
        "engine_urls": {spec.iid: spec.url for spec in cfg.engines},
        "generations": generations,
        "process_starts": process_starts,
        "changed_generations": changed,
        "generation_errors": generation_errors,
        "input_tokens": list(input_tokens),
        "samples_per_group": samples_per_group,
        "observation_timeout_s": observation_timeout_s,
        "configured_deadline_s": cfg.first_token_timeout_s,
        "candidate_deadline_s": max(candidates) if candidates else None,
        "groups": summaries,
        "attempts": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(document, output, indent=2)
        output.write("\n")
    print(f"first-token calibration: {out}")
    if candidates:
        print(f"candidate deadline: above {max(candidates):.3f}s")
    if changed:
        print(f"process generation changed during calibration: {', '.join(changed)}")
    for error in generation_errors:
        print(f"process generation check failed: {error}")
    print(f"duration {duration_s:.0f}s")
    return 0 if complete else 1
