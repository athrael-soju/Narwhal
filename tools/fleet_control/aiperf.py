"""Run one AIPerf load job per request and reduce its exports to the run record's results."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from tools.measurement.benchmark_evidence import client_row

from .app import API
from .config import ControlConfig
from .hooks import tail, terminate
from .jobs import Job
from .records import owner_only
from .workloads import LoadConfig, Workload, validate_params

ARTIFACTS = "aiperf"
LOG = "aiperf.log"
SUMMARY = "profile_export_aiperf.json"
RECORDS = "profile_export.jsonl"
TRACE_FORMAT = "mooncake_trace"
PERCENTILES = ("avg", "p50", "p90", "p95", "p99")
COMPLETED = "completed"


class AIPerfFailed(RuntimeError):
    """AIPerf could not start, exited non-zero or wrote no summary export."""


def _value(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else repr(float(number))


def command(
    load: LoadConfig, url: str, workload: Workload, params: Mapping[str, Any], artifacts: Path
) -> list[str]:
    """Return the `aiperf profile` argument vector for one job against the router at `url`."""
    argv = [
        load.aiperf,
        "profile",
        "--url",
        url,
        "--model",
        load.model,
        "--tokenizer",
        load.tokenizer,
        "--endpoint-type",
        load.endpoint_type,
    ]
    if load.streaming:
        argv.append("--streaming")
    argv += ["--ui", "none", "--no-gpu-telemetry", "--export-level", "records"]
    argv += ["--artifact-dir", str(artifacts)]
    if workload.kind == "synthetic":
        assert workload.isl is not None and workload.osl is not None
        argv += ["--isl", _value(workload.isl), "--isl-stddev", _value(workload.isl_stddev)]
        argv += ["--osl", _value(workload.osl), "--osl-stddev", _value(workload.osl_stddev)]
    else:
        argv += ["--input-file", str(workload.file), "--custom-dataset-type", TRACE_FORMAT]
        if workload.kind == "timestamped_trace":
            argv += ["--fixed-schedule", "--fixed-schedule-auto-offset"]
        else:
            # Pace the trace by the job's rate and concurrency, not by any record timestamps.
            argv.append("--no-fixed-schedule")
        if workload.block_size is not None:
            argv += ["--isl-block-size", _value(workload.block_size)]
    if "rate" in params:
        argv += ["--request-rate", _value(params["rate"])]
    if "concurrency" in params:
        argv += ["--concurrency", _value(params["concurrency"])]
    if "duration_s" in params:
        argv += ["--benchmark-duration", _value(params["duration_s"])]
        if load.grace_period_s is not None:
            argv += ["--benchmark-grace-period", _value(load.grace_period_s)]
    if load.goodput:
        argv += ["--goodput", " ".join(f"{t}:{_value(v)}" for t, v in load.goodput.items())]
    return argv + list(load.extra_args)


def _metric(summary: Mapping[str, Any], tag: str) -> dict[str, Any] | None:
    raw = summary.get(tag)
    if not isinstance(raw, dict):
        return None
    stats = {key: raw[key] for key in PERCENTILES if isinstance(raw.get(key), int | float)}
    return {"unit": raw.get("unit"), **stats} if stats else None


def _average(summary: Mapping[str, Any], tag: str) -> float | None:
    raw = summary.get(tag)
    value = raw.get("avg") if isinstance(raw, dict) else None
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _count(summary: Mapping[str, Any], tag: str) -> int | None:
    value = _average(summary, tag)
    return None if value is None else round(value)


def _outcome(error: Mapping[str, Any] | None) -> str:
    """Classify an AIPerf error the way the benchmark evidence collector does."""
    return str(client_row({"metadata": {}, "metrics": {}, "error": error})["outcome"])


def _record_outcomes(path: Path) -> tuple[Counter[str], int]:
    outcomes: Counter[str] = Counter()
    unreadable = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = client_row(json.loads(line))
            except (ValueError, TypeError, KeyError, AttributeError):
                unreadable += 1
                continue
            if not row.get("benchmark_warmup"):
                outcomes[str(row.get("outcome"))] += 1
    return outcomes, unreadable


def _requests(summary: Mapping[str, Any], records: Path) -> dict[str, Any]:
    """Count profiling requests by outcome from the records export, else from the summary."""
    if records.is_file():
        outcomes, unreadable = _record_outcomes(records)
        source = RECORDS
    else:
        outcomes, unreadable, source = Counter(), 0, SUMMARY
        completed = _count(summary, "request_count")
        if completed:
            outcomes[COMPLETED] = completed
        for entry in summary.get("error_summary") or []:
            if isinstance(entry, dict) and isinstance(entry.get("count"), int):
                details = entry.get("error_details")
                outcomes[_outcome(details if isinstance(details, dict) else None)] += entry["count"]
    errors = {name: count for name, count in sorted(outcomes.items()) if name != COMPLETED}
    result: dict[str, Any] = {
        "total": sum(outcomes.values()),
        "completed": outcomes[COMPLETED],
        "errors": sum(errors.values()),
        "errors_by_type": errors,
        "source": source,
    }
    if unreadable:
        result["unreadable_records"] = unreadable
    return result


def summarize(artifacts: Path, goodput: Mapping[str, float]) -> dict[str, Any]:
    """Reduce an AIPerf artifact directory to request counts, throughput and latency.

    Raise ValueError when the directory holds no readable summary export. Latency statistics
    keep the unit AIPerf reports.
    """
    path = artifacts / SUMMARY
    if not path.is_file():
        found = sorted(artifacts.rglob(SUMMARY)) if artifacts.is_dir() else []
        if len(found) != 1:
            raise ValueError(f"no {SUMMARY} under {artifacts.name}/")
        path = found[0]
    summary = json.loads(path.read_text())
    if not isinstance(summary, dict):
        raise ValueError(f"{SUMMARY} is not a JSON object")
    result: dict[str, Any] = {
        "summary": str(path.relative_to(artifacts.parent)),
        "aiperf_version": summary.get("aiperf_version"),
        "was_cancelled": summary.get("was_cancelled"),
        "is_complete": summary.get("is_complete"),
        "requests": _requests(summary, path.parent / RECORDS),
        "throughput": {
            "requests_per_s": _average(summary, "request_throughput"),
            "output_tokens_per_s": _average(summary, "output_token_throughput"),
        },
        "latency": {
            "ttft": _metric(summary, "time_to_first_token"),
            "itl": _metric(summary, "inter_token_latency"),
            "request": _metric(summary, "request_latency"),
        },
    }
    if goodput:
        result["goodput"] = {
            "slo": dict(goodput),
            "requests_per_s": _average(summary, "goodput"),
            "good_requests": _count(summary, "good_request_count"),
        }
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _spawned(
    spawn: asyncio.Future[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process | None:
    try:
        return await spawn
    except OSError:
        return None


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Terminate AIPerf's process group, then kill any member that outlived its leader."""
    await terminate(process)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


class AIPerfRunner:
    """Run each load job as one `aiperf profile` process in its own process group.

    The job directory receives AIPerf's output log and artifact directory. Stopping the job
    terminates the whole process group. The job's result holds the command and exit status
    from the moment AIPerf starts, so stopped and failed jobs keep them in the run record.
    """

    def __init__(self, load: LoadConfig, url: str, env: Mapping[str, str]) -> None:
        self.load = load
        self.url = url
        self._env = dict(env)

    def validate(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return normalized job parameters, or raise ValueError for an invalid request."""
        return validate_params(self.load.workloads, params)

    async def run(self, job: Job) -> Mapping[str, Any]:
        """Run AIPerf for `job` and return its summarized client results."""
        workload = self.load.workloads[job.params["workload"]]
        artifacts = job.directory.resolve() / ARTIFACTS
        argv = command(self.load, self.url, workload, job.params, artifacts)
        result: dict[str, Any] = {
            "workload": workload.document(),
            "command": argv,
            "log": LOG,
            "artifacts": ARTIFACTS,
            "exit_code": None,
        }
        job.result = result
        if workload.file is not None:
            try:
                result["workload"]["sha256"] = await asyncio.to_thread(_sha256, workload.file)
            except OSError as exc:
                raise AIPerfFailed(f"trace file is unreadable: {exc}") from exc
        artifacts.mkdir(mode=0o700)
        log = job.directory / LOG
        started = time.monotonic()
        with open(log, "x", opener=owner_only) as output:
            spawn = asyncio.ensure_future(
                asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=output,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._env,
                    start_new_session=True,
                )
            )
            try:
                process = await asyncio.shield(spawn)
                result["pid"] = process.pid
                await process.wait()
            except asyncio.CancelledError:
                # A stop can arrive while the process is still being created.
                started_process = await _spawned(spawn)
                if started_process is not None:
                    await asyncio.shield(_stop(started_process))
                    self._finish(result, started_process, started, log, artifacts)
                raise
            except OSError as exc:
                raise AIPerfFailed(f"aiperf could not start: {exc}") from exc
        self._finish(result, process, started, log, artifacts)
        if process.returncode != 0:
            raise AIPerfFailed(f"aiperf exited {process.returncode}")
        if "summary_error" in result:
            raise AIPerfFailed(f"aiperf wrote no readable summary: {result['summary_error']}")
        return result

    def _finish(
        self,
        result: dict[str, Any],
        process: asyncio.subprocess.Process,
        started: float,
        log: Path,
        artifacts: Path,
    ) -> None:
        result["exit_code"] = process.returncode
        result["duration_s"] = round(time.monotonic() - started, 3)
        try:
            result.update(summarize(artifacts, self.load.goodput))
        except (OSError, ValueError) as exc:
            result["summary_error"] = str(exc)
        if process.returncode != 0:
            result["tail"] = tail(log)


def runner_for(config: ControlConfig, env: Mapping[str, str]) -> AIPerfRunner | None:
    """Return the configured AIPerf runner; AIPerf never receives the control token."""
    if config.load is None:
        return None
    env = {key: value for key, value in env.items() if key != config.token_env}
    return AIPerfRunner(config.load, config.router.url, env)


def workload_routes(load: LoadConfig) -> APIRouter:
    """Return the route that lists the workload library."""
    routes = APIRouter(prefix=API)

    @routes.get("/workloads")
    async def workloads() -> dict[str, Any]:
        # Trace file paths stay in the private configuration.
        documents = [entry.document() for entry in load.workloads.values()]
        return {"workloads": [{k: v for k, v in d.items() if k != "file"} for d in documents]}

    return routes
