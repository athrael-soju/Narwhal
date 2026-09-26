"""Measure crossed-handoff first-token latency without the serving deadline."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from ..config import FleetConfig
from ..engines.client import FIRST_OUTPUT_DETAIL, EngineClient, EngineError
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect
from ..engines.stream import sse_token_bearing
from ..engines.validation import validation_pairs
from .generation import read_generation
from .probe import engine_context_limit, make_prompt

SCHEMA = "narwhal.first-token-calibration"


def _group_key(row: Any) -> tuple[str, str, int] | None:
    if not isinstance(row, dict):
        return None
    source = row.get("producer")
    destination = row.get("consumer")
    target = row.get("target_input_tokens")
    if not isinstance(source, str) or not isinstance(destination, str) or type(target) is not int:
        return None
    return source, destination, target


def candidate_deadline(samples: list[float]) -> tuple[float, float, float]:
    """Return nearest-rank p99, maximum, and the documented guarded candidate."""
    if not samples or any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError("candidate deadline requires finite completed timings")
    ordered = sorted(samples)
    p99 = ordered[math.ceil(0.99 * len(ordered)) - 1]
    maximum = ordered[-1]
    return p99, maximum, max(maximum, 1.2 * p99) + 0.5


def evidence_problems(cfg: FleetConfig, document: dict[str, Any]) -> list[str]:
    """Check a completed calibration against this fleet's current configuration."""
    problems: list[str] = []
    if document.get("schema") != SCHEMA or document.get("schema_version") != 1:
        return ["first-token calibration has an unknown schema or version"]
    if document.get("status") != "complete":
        problems.append("first-token calibration has failed, expired, or insufficient probes")
    if document.get("model") != cfg.model:
        problems.append("first-token calibration model differs from the fleet")
    contract = cfg.engine_contract.fingerprint() if cfg.engine_contract else None
    if document.get("contract_fingerprint") != contract:
        problems.append("first-token calibration engine contract differs from the fleet")
    expected = {spec.iid: spec.url for spec in cfg.engines}
    if document.get("engine_urls") != expected:
        problems.append("first-token calibration engine addresses differ from the fleet")
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
    for attempt in attempts:
        key = _group_key(attempt)
        if key is None or attempt.get("status") != "completed":
            continue
        elapsed = attempt.get("first_token_seconds")
        if (
            not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
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
        if timings.get(key):
            p99, maximum, calculated = candidate_deadline(timings[key])
            for field, expected_value in (
                ("p99_seconds", p99),
                ("maximum_seconds", maximum),
                ("candidate_deadline_s", calculated),
            ):
                observed = row.get(field)
                if not isinstance(observed, (int, float)) or not math.isclose(
                    observed, expected_value, rel_tol=1e-9, abs_tol=1e-9
                ):
                    problems.append(f"first-token calibration group {key} has incorrect {field}")
        value = row.get("candidate_deadline_s")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        ):
            group_candidates.append(value)
        else:
            problems.append(f"first-token calibration group {key} has no candidate deadline")
    if actual_groups != expected_groups or len(groups) != len(expected_groups):
        problems.append(
            "first-token calibration does not cover every directed pair and input length"
        )
    candidate = document.get("candidate_deadline_s")
    if (
        not isinstance(candidate, (int, float))
        or isinstance(candidate, bool)
        or not math.isfinite(candidate)
        or candidate <= 0
    ):
        problems.append("first-token calibration has no valid candidate deadline")
    elif cfg.first_token_timeout_s <= candidate:
        problems.append(
            f"engine.first_token_timeout_s {cfg.first_token_timeout_s:g}s is not above "
            f"measured calibration candidate {candidate:g}s"
        )
    if group_candidates and candidate != max(group_candidates):
        problems.append("first-token calibration candidate differs from its measured groups")
    return problems


async def verify_calibration(cfg: FleetConfig) -> list[str]:
    """Check saved timings and the engine generations they measured."""
    path = cfg.first_token_calibration_path
    if path is None:
        return [
            "engine.first_token_calibration_path is unset; run narwhal-check "
            "--calibrate-first-token before qualifying this fleet"
        ]
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [f"first-token calibration {path} is unreadable: {exc}"]
    if not isinstance(document, dict):
        return [f"first-token calibration {path} must be a JSON object"]
    problems = evidence_problems(cfg, document)
    saved = document.get("generations")
    if not isinstance(saved, dict):
        return [*problems, "first-token calibration has no process-generation evidence"]
    for spec in cfg.engines:
        try:
            current = await read_generation(
                spec,
                cfg.engine_contract,
                timeout_s=cfg.health_timeout_s,
                headers=cfg.engine_headers(),
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"{spec.iid} calibration generation unreadable: {exc}")
            continue
        if saved.get(spec.iid) != current.digest:
            problems.append(f"{spec.iid} process differs from first-token calibration")
    return problems


async def calibrate(
    cfg: FleetConfig,
    *,
    input_tokens: tuple[int, ...],
    samples_per_group: int,
    observation_timeout_s: float,
    out: Path,
) -> int:
    """Write fresh process-bound samples and return zero only for complete evidence."""
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
    by_id = {spec.iid: spec for spec in cfg.engines}
    dialect = lookup_dialect(cfg.dialect)
    if dialect.tokenize_path is None:
        raise ValueError("first-token calibration requires an exact-count tokenizer route")
    generations = {
        spec.iid: (
            await read_generation(
                spec,
                cfg.engine_contract,
                timeout_s=observation_timeout_s,
                headers=cfg.engine_headers(),
            )
        ).digest
        for spec in cfg.engines
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
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            timeout=observation_timeout_s, headers=cfg.engine_headers()
        ) as sizing:
            context_limits: dict[str, int] = {}
            for source in sorted({src for src, _ in pairs}):
                spec = by_id[source]
                async with asyncio.timeout(observation_timeout_s):
                    context_limit = await engine_context_limit(
                        sizing, spec.url, cfg.model, dialect, observation_timeout_s
                    )
                for target in input_tokens:
                    if target + 4 > context_limit:
                        raise ValueError(
                            f"{source}: requested {target} input tokens plus four output tokens "
                            f"exceed live context limit {context_limit}"
                        )
                context_limits[source] = context_limit
            for source, context_limit in context_limits.items():
                spec = by_id[source]
                for target in input_tokens:
                    for src, dst in pairs:
                        if src != source:
                            continue
                        timings: list[float] = []
                        actual_lengths: list[int] = []
                        failures = 0
                        for attempt in range(samples_per_group):
                            row: dict[str, Any] = {
                                "producer": src,
                                "consumer": dst,
                                "target_input_tokens": target,
                                "attempt": attempt + 1,
                            }
                            phase = "sizing"
                            request_deadline = asyncio.timeout(cfg.request_timeout_s)
                            try:
                                async with request_deadline:
                                    async with asyncio.timeout(observation_timeout_s):
                                        prompt, actual = await make_prompt(
                                            sizing,
                                            spec.url,
                                            cfg.model,
                                            target,
                                            dialect,
                                            timeout_s=observation_timeout_s,
                                            prefix=uuid4().hex + " ",
                                        )
                                    row["actual_input_tokens"] = actual
                                    if actual + 4 > context_limit:
                                        raise ValueError(
                                            f"sized input {actual} plus four output tokens "
                                            f"exceed live context limit {context_limit}"
                                        )
                                    body = {
                                        "model": cfg.model,
                                        "prompt": prompt,
                                        "max_tokens": 4,
                                        "temperature": 0.0,
                                    }
                                    phase = "prefill"
                                    began = time.monotonic()
                                    handoff = await client.prefill(
                                        by_id[src].url, "/v1/completions", body, {}
                                    )
                                    row["prefill_seconds"] = time.monotonic() - began
                                    phase = "decode"
                                    began = time.monotonic()
                                    first: float | None = None
                                    async for line in client.decode(
                                        by_id[dst].url,
                                        "/v1/completions",
                                        body,
                                        {},
                                        handoff,
                                        first_token_timeout_s=observation_timeout_s,
                                    ):
                                        if first is None and sse_token_bearing(line, dialect):
                                            first = time.monotonic() - began
                                            row["first_token_seconds"] = first
                                    if first is None:
                                        raise ValueError(
                                            "decode completed without a generated token"
                                        )
                                    row.update(status="completed", first_token_seconds=first)
                                    timings.append(first)
                                    actual_lengths.append(actual)
                            except (
                                EngineError,
                                httpx.HTTPError,
                                TimeoutError,
                                ValueError,
                                RuntimeError,
                            ) as exc:
                                failures += 1
                                expired = (
                                    isinstance(exc, EngineError)
                                    and exc.status == 504
                                    and exc.detail.startswith(FIRST_OUTPUT_DETAIL)
                                )
                                row.update(
                                    status=(
                                        "request_expired"
                                        if request_deadline.expired()
                                        else "observation_expired"
                                        if expired
                                        else "failed_sizing"
                                        if phase == "sizing"
                                        else "failed_prefill"
                                        if phase == "prefill"
                                        else "failed_transfer"
                                    ),
                                    error=f"{type(exc).__name__}: {exc}",
                                )
                            rows.append(row)
                        summary: dict[str, Any] = {
                            "producer": src,
                            "consumer": dst,
                            "target_input_tokens": target,
                            "actual_input_tokens_min": min(actual_lengths)
                            if actual_lengths
                            else None,
                            "actual_input_tokens_max": max(actual_lengths)
                            if actual_lengths
                            else None,
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
                        print(
                            f"{src} -> {dst}, target {target} tokens: "
                            f"{len(timings)}/{samples_per_group} completed, {failures} failed"
                        )
    finally:
        await client.aclose()
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
            if current.digest != generations[spec.iid]:
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
    document = {
        "schema": SCHEMA,
        "schema_version": 1,
        "captured_at_unix": time.time(),
        "status": "complete" if complete else "incomplete",
        "model": cfg.model,
        "contract_fingerprint": cfg.engine_contract.fingerprint() if cfg.engine_contract else None,
        "engine_urls": {spec.iid: spec.url for spec in cfg.engines},
        "generations": generations,
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
    return 0 if complete else 1
