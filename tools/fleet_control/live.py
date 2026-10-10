"""Live load-job results from AIPerf's per-request records."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from tools.measurement.benchmark_evidence import client_row

from .app import API
from .service import ControlService

ARTIFACTS = "aiperf"
RECORDS = "profile_export.jsonl"
COMPLETED = "completed"
# Result name and AIPerf metric tag of each latency, in milliseconds.
LATENCIES = {
    "ttft": "time_to_first_token",
    "itl": "inter_token_latency",
    "request": "request_latency",
}
# Smallest time-series bucket, in seconds, and the most points kept.
BUCKET_S = 5
MAX_POINTS = 120


@dataclass(frozen=True)
class Sample:
    """One profiling request: its timing, outcome, output tokens and latencies."""

    start_ns: int
    end_ns: int
    outcome: str
    output_tokens: float
    good: bool | None
    latency: Mapping[str, float]


def _value(metrics: Mapping[str, Any], tag: str) -> float | None:
    raw = metrics.get(tag)
    value = raw.get("value") if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def sample(row: Mapping[str, Any]) -> Sample | None:
    """Return the request a record describes, or None for a warm-up request."""
    metadata = row["metadata"]
    if metadata.get("benchmark_phase") == "warmup":
        return None
    metrics = row.get("metrics") or {}
    start = int(metadata["request_start_ns"])
    good = _value(metrics, "good_request_count")
    latency = {name: _value(metrics, tag) for name, tag in LATENCIES.items()}
    return Sample(
        start_ns=start,
        end_ns=int(metadata.get("request_end_ns") or start),
        outcome=str(client_row(dict(row))["outcome"]),
        output_tokens=_value(metrics, "output_token_count") or 0.0,
        good=None if good is None else good > 0,
        latency={name: value for name, value in latency.items() if value is not None},
    )


def percentile(values: list[float], fraction: float) -> float:
    """Return the linearly interpolated percentile of sorted `values`."""
    position = (len(values) - 1) * fraction
    low = math.floor(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "p50": percentile(ordered, 0.5),
        "p95": percentile(ordered, 0.95),
        "p99": percentile(ordered, 0.99),
    }


class LiveRecords:
    """Follow one AIPerf record export, keeping one sample per profiling request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._identity: tuple[int, int] | None = None
        self._reset()

    def _reset(self) -> None:
        self.samples: list[Sample] = []
        self.warmup = 0
        self.unreadable = 0
        self._offset = 0

    def refresh(self) -> None:
        """Parse the complete lines appended since the last refresh."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return
        identity = (stat.st_dev, stat.st_ino)
        if identity != self._identity or stat.st_size < self._offset:
            self._reset()
            self._identity = identity
        with self.path.open("rb") as stream:
            stream.seek(self._offset)
            data = stream.read()
        # Read up to the last complete line.
        complete = data.rfind(b"\n") + 1
        self._offset += complete
        for line in data[:complete].splitlines():
            if not line.strip():
                continue
            try:
                found = sample(json.loads(line))
            except (ValueError, TypeError, KeyError, AttributeError):
                self.unreadable += 1
                continue
            if found is None:
                self.warmup += 1
            else:
                self.samples.append(found)

    def results(self, slo: Mapping[str, float]) -> dict[str, Any]:
        """Return the counts, rates, latency percentiles and time series so far."""
        samples = self.samples
        completed = [s for s in samples if s.outcome == COMPLETED]
        errors: dict[str, int] = {}
        for s in samples:
            if s.outcome != COMPLETED:
                errors[s.outcome] = errors.get(s.outcome, 0) + 1
        result: dict[str, Any] = {
            "requests": {
                "total": len(samples),
                "completed": len(completed),
                "errors": len(samples) - len(completed),
                "errors_by_type": dict(sorted(errors.items())),
                "warmup": self.warmup,
            },
            "elapsed_s": 0.0,
            "throughput": {"requests_per_s": None, "output_tokens_per_s": None},
            "latency": {
                name: _stats([s.latency[name] for s in completed if name in s.latency])
                for name in LATENCIES
            },
            "slo": {name: slo[tag] for name, tag in LATENCIES.items() if tag in slo},
            "series": {"bucket_s": BUCKET_S, "points": []},
        }
        if self.unreadable:
            result["requests"]["unreadable"] = self.unreadable
        if not samples:
            return result
        start = min(s.start_ns for s in samples)
        elapsed = max(s.end_ns for s in samples) - start
        elapsed_s = elapsed / 1e9
        result["elapsed_s"] = round(elapsed_s, 3)
        if elapsed_s > 0:
            result["throughput"] = {
                "requests_per_s": len(completed) / elapsed_s,
                "output_tokens_per_s": sum(s.output_tokens for s in completed) / elapsed_s,
            }
            graded = [s.good for s in completed if s.good is not None]
            if graded:
                result["goodput"] = {
                    "good_requests": sum(graded),
                    "requests_per_s": sum(graded) / elapsed_s,
                }
        result["series"] = _series(samples, start, elapsed_s)
        return result


def _series(samples: list[Sample], start: int, elapsed_s: float) -> dict[str, Any]:
    """Bucket the requests by finish time into completion rate, errors and latency p95."""
    bucket = max(BUCKET_S, BUCKET_S * math.ceil(elapsed_s / MAX_POINTS / BUCKET_S))
    count = max(1, math.ceil(elapsed_s / bucket))
    buckets: list[list[Sample]] = [[] for _ in range(count)]
    for s in samples:
        index = min(int((s.end_ns - start) / 1e9 // bucket), count - 1)
        buckets[index].append(s)
    points = []
    for index, members in enumerate(buckets):
        done = [s for s in members if s.outcome == COMPLETED]
        # The last bucket covers only the time elapsed so far.
        width = min(bucket, elapsed_s - index * bucket) if index == count - 1 else bucket
        point: dict[str, Any] = {
            "t": (index + 1) * bucket,
            "requests_per_s": len(done) / width if width > 0 else 0.0,
            "errors": len(members) - len(done),
        }
        for name in ("ttft", "itl"):
            stats = _stats([s.latency[name] for s in done if name in s.latency])
            point[f"{name}_p95"] = None if stats is None else stats["p95"]
        points.append(point)
    return {"bucket_s": bucket, "points": points}


def live_routes(service: ControlService, slo: Mapping[str, float]) -> APIRouter:
    """Return the route that reports the current load job's results from its records so far."""
    routes = APIRouter(prefix=API)
    followed: dict[str, LiveRecords] = {}
    # Overlapping polls refresh one at a time.
    reading = asyncio.Lock()

    @routes.get("/jobs/current/live")
    async def live() -> JSONResponse:
        job = None if service.jobs is None else service.jobs.job
        if job is None:
            return JSONResponse({"detail": "no load job has run"}, status_code=404)
        # The job directory identifies the job.
        key = str(job.directory)
        records = followed.get(key)
        if records is None:
            followed.clear()
            records = followed[key] = LiveRecords(job.directory / ARTIFACTS / RECORDS)
        async with reading:
            await asyncio.to_thread(records.refresh)
            results = records.results(slo)
        return JSONResponse({"job": job.id, "state": job.state, **results})

    return routes
