"""Score trace replays against TTFT and TPOT SLOs and find each trace's highest passing rate."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tools.measurement.benchmark_evidence import client_row
from tools.measurement.trace_convert import mean_rate, read_arrivals

DEFAULT_SHARE = 0.9
DEFAULT_RECORDS = "{point_dir}/aiperf/profile_export.jsonl"
# Seconds in one unit of an AIPerf record metric.
UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}


def trace_rate(path: Path) -> dict[str, Any]:
    """Return the request count, replay span and mean offered rate of a `mooncake_trace` file."""
    stamps = [arrival.at_s for arrival in read_arrivals(path, "mooncake")]
    if not stamps:
        raise ValueError(f"{path} holds no request")
    span = max(stamps) - min(stamps)
    return {
        "requests": len(stamps),
        "replay_span_s": span,
        "offered_rps": mean_rate(len(stamps), span),
    }


def _seconds(metrics: Mapping[str, Any], tag: str) -> float | None:
    raw = metrics.get(tag)
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    unit = raw.get("unit")
    if unit not in UNITS:
        raise ValueError(f"{tag} has unit {unit!r}, not one of {', '.join(UNITS)}")
    return float(value) * UNITS[unit]


def _tokens(metrics: Mapping[str, Any]) -> float | None:
    for tag in ("output_sequence_length", "output_token_count"):
        raw = metrics.get(tag)
        value = raw.get("value") if isinstance(raw, dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def within_slo(row: Mapping[str, Any], ttft_s: float, tpot_s: float) -> bool:
    """Return whether an AIPerf record completed within the TTFT and TPOT limits.

    A request with one output token has no TPOT and passes on TTFT alone.
    """
    if client_row(dict(row))["outcome"] != "completed":
        return False
    metrics = row.get("metrics") or {}
    ttft = _seconds(metrics, "time_to_first_token")
    if ttft is None or ttft > ttft_s:
        return False
    tpot = _seconds(metrics, "inter_token_latency")
    if tpot is not None:
        return tpot <= tpot_s
    tokens = _tokens(metrics)
    return tokens is not None and tokens < 2


def score(path: Path, offered: int, ttft_s: float, tpot_s: float, share: float) -> dict[str, Any]:
    """Score the AIPerf records of one replay of a trace with `offered` requests.

    The attainment rate counts requests within both SLOs over every offered request,
    so a request without a record counts as a miss.
    """
    recorded = passed = 0
    with path.open(encoding="utf-8") as stream:
        for line, text in enumerate(stream, 1):
            if not text.strip():
                continue
            try:
                row = json.loads(text)
                if not isinstance(row, dict) or not isinstance(row.get("metadata"), dict):
                    raise TypeError("not an AIPerf record")
                if row["metadata"].get("benchmark_phase") == "warmup":
                    continue
                recorded += 1
                passed += within_slo(row, ttft_s, tpot_s)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
    if recorded > offered:
        raise ValueError(f"{path} holds {recorded} records for a trace of {offered} requests")
    attainment = passed / offered
    return {
        "offered": offered,
        "recorded": recorded,
        "within_slo": passed,
        "attainment": attainment,
        "met": attainment >= share,
    }


def highest_rates(points: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return, for each trace, the scored points by rate and the highest rate that met the share.

    Each point holds `id`, `trace` and the `attainment` that `score` returned with the
    trace's `offered_rps`.
    """
    traces: dict[str, list[Mapping[str, Any]]] = {}
    for point in points:
        traces.setdefault(point["trace"], []).append(point)
    report = {}
    for name, scored in traces.items():
        ordered = sorted(scored, key=lambda point: point["attainment"]["offered_rps"] or 0)
        passing = [point for point in ordered if point["attainment"]["met"]]
        best = passing[-1] if passing else None
        report[name] = {
            "highest_rps": None if best is None else best["attainment"]["offered_rps"],
            "highest_point": None if best is None else best["id"],
            "points": [
                {
                    "id": point["id"],
                    "offered_rps": point["attainment"]["offered_rps"],
                    "attainment": point["attainment"]["attainment"],
                    "met": point["attainment"]["met"],
                }
                for point in ordered
            ],
        }
    return report
