"""Load-job settings: the AIPerf client, the served model and the workload library."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

WorkloadKind = Literal["synthetic", "timestamped_trace", "prefix_trace"]
KINDS: tuple[WorkloadKind, ...] = ("synthetic", "timestamped_trace", "prefix_trace")
ENDPOINT_TYPES = ("chat", "completions")
DEFAULT_ENDPOINT_TYPE = "chat"
PARAMS = ("workload", "rate", "concurrency", "duration_s")

_LOAD_KEYS = {
    "aiperf",
    "model",
    "tokenizer",
    "endpoint_type",
    "streaming",
    "goodput",
    "grace_period_s",
    "extra_args",
    "workloads",
}
_WORKLOAD_KEYS: dict[WorkloadKind, set[str]] = {
    "synthetic": {"kind", "isl", "osl", "isl_stddev", "osl_stddev"},
    "timestamped_trace": {"kind", "file", "block_size"},
    "prefix_trace": {"kind", "file", "block_size"},
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_METRIC_TAG = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Workload:
    """One named library entry: synthetic length distributions or a trace file.

    A `timestamped_trace` replays its records at their recorded timestamps. A `prefix_trace`
    reuses prompt prefixes through the `hash_ids` of its records and is paced by the job's
    rate and concurrency instead of any recorded timestamps.
    """

    name: str
    kind: WorkloadKind
    isl: int | None = None
    osl: int | None = None
    isl_stddev: float = 0.0
    osl_stddev: float = 0.0
    file: Path | None = None
    block_size: int | None = None

    def document(self) -> dict[str, Any]:
        """Return the library entry as recorded with a job."""
        if self.kind == "synthetic":
            return {
                "name": self.name,
                "kind": self.kind,
                "isl": self.isl,
                "isl_stddev": self.isl_stddev,
                "osl": self.osl,
                "osl_stddev": self.osl_stddev,
            }
        return {
            "name": self.name,
            "kind": self.kind,
            "file": str(self.file),
            "block_size": self.block_size,
        }


@dataclass(frozen=True)
class LoadConfig:
    """The AIPerf executable, the model it requests and the workload library.

    AIPerf sends its requests to the router named by the control configuration's `router.url`.
    """

    aiperf: str
    model: str
    tokenizer: str
    workloads: Mapping[str, Workload]
    endpoint_type: str = DEFAULT_ENDPOINT_TYPE
    streaming: bool = True
    goodput: Mapping[str, float] = field(default_factory=dict)
    grace_period_s: float | None = None
    extra_args: tuple[str, ...] = ()


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def read_load(problems: list[str], raw: object) -> LoadConfig | None:
    """Validate the `load` section, appending each problem to `problems`."""
    if not isinstance(raw, dict):
        problems.append("load must be an object")
        return None
    count = len(problems)
    problems.extend(f"unknown key load.{key}" for key in sorted(set(raw) - _LOAD_KEYS))
    problems.extend(
        f"load.{key} must be a non-empty string"
        for key in ("aiperf", "model", "tokenizer")
        if not _text(raw.get(key))
    )
    endpoint_type = raw.get("endpoint_type", DEFAULT_ENDPOINT_TYPE)
    if endpoint_type not in ENDPOINT_TYPES:
        problems.append(f"load.endpoint_type must be one of {', '.join(ENDPOINT_TYPES)}")
    streaming = raw.get("streaming", True)
    if not isinstance(streaming, bool):
        problems.append("load.streaming must be true or false")
    goodput = raw.get("goodput", {})
    if not isinstance(goodput, dict) or not all(
        isinstance(tag, str) and _METRIC_TAG.match(tag) and _number(value) and value > 0
        for tag, value in goodput.items()
    ):
        problems.append("load.goodput must map AIPerf metric tags to positive numbers")
    grace = raw.get("grace_period_s")
    if grace is not None and not (_number(grace) and grace >= 0):
        problems.append("load.grace_period_s must be a non-negative number of seconds")
    extra = raw.get("extra_args", [])
    if not isinstance(extra, list) or not all(_text(part) for part in extra):
        problems.append("load.extra_args must be a list of non-empty strings")
    workloads = _read_workloads(problems, raw.get("workloads"))
    if len(problems) > count:
        return None
    return LoadConfig(
        aiperf=raw["aiperf"],
        model=raw["model"],
        tokenizer=raw["tokenizer"],
        workloads=workloads,
        endpoint_type=str(endpoint_type),
        streaming=bool(streaming),
        goodput={str(tag): float(value) for tag, value in goodput.items()},
        grace_period_s=None if grace is None else float(grace),
        extra_args=tuple(extra),
    )


def _read_workloads(problems: list[str], raw: object) -> dict[str, Workload]:
    if not isinstance(raw, dict) or not raw:
        problems.append("load.workloads must be an object naming at least one workload")
        return {}
    workloads = {}
    for name, spec in raw.items():
        label = f"load.workloads.{name}"
        if not _NAME.match(name):
            problems.append(f"{label}: names use letters, digits, '.', '_' and '-'")
            continue
        if not isinstance(spec, dict) or spec.get("kind") not in KINDS:
            problems.append(f"{label}.kind must be one of {', '.join(KINDS)}")
            continue
        kind: WorkloadKind = spec["kind"]
        unknown = sorted(set(spec) - _WORKLOAD_KEYS[kind])
        problems.extend(f"unknown key {label}.{key}" for key in unknown)
        workload = (
            _read_synthetic(problems, label, name, spec)
            if kind == "synthetic"
            else _read_trace(problems, label, name, kind, spec)
        )
        if workload is not None and not unknown:
            workloads[name] = workload
    return workloads


def _read_synthetic(
    problems: list[str], label: str, name: str, spec: Mapping[str, Any]
) -> Workload | None:
    count = len(problems)
    problems.extend(
        f"{label}.{key} must be a positive number of tokens"
        for key in ("isl", "osl")
        if not _positive_int(spec.get(key))
    )
    for key in ("isl_stddev", "osl_stddev"):
        value = spec.get(key, 0)
        if not (_number(value) and value >= 0):
            problems.append(f"{label}.{key} must be a non-negative number of tokens")
    if len(problems) > count:
        return None
    return Workload(
        name,
        "synthetic",
        isl=spec["isl"],
        osl=spec["osl"],
        isl_stddev=float(spec.get("isl_stddev", 0)),
        osl_stddev=float(spec.get("osl_stddev", 0)),
    )


def _read_trace(
    problems: list[str], label: str, name: str, kind: WorkloadKind, spec: Mapping[str, Any]
) -> Workload | None:
    count = len(problems)
    if not _text(spec.get("file")):
        problems.append(f"{label}.file must name a mooncake_trace JSONL file")
    block_size = spec.get("block_size")
    if kind == "prefix_trace" and block_size is None:
        problems.append(f"{label}.block_size is required: the tokens each hash ID stands for")
    elif block_size is not None and not _positive_int(block_size):
        problems.append(f"{label}.block_size must be a positive number of tokens")
    if len(problems) > count:
        return None
    return Workload(name, kind, file=Path(spec["file"]), block_size=block_size)


def validate_params(workloads: Mapping[str, Workload], params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a job's normalized parameters, or raise ValueError naming every problem.

    `rate` is requests per second, `concurrency` the in-flight request limit and `duration_s`
    the load duration. A synthetic or reused-prefix workload needs `duration_s` and a rate,
    a concurrency or both. A timestamped trace keeps its recorded arrival times, so it takes
    no rate; `concurrency` and `duration_s` optionally cap and shorten its replay.
    """
    problems = [f"unknown job parameter {key!r}" for key in sorted(set(params) - set(PARAMS))]
    name = params.get("workload")
    workload = workloads.get(name) if isinstance(name, str) else None
    if workload is None:
        problems.append(f"workload must name a library entry: {', '.join(sorted(workloads))}")
    rate = params.get("rate")
    if rate is not None and not (_number(rate) and rate > 0):
        problems.append("rate must be a positive number of requests per second")
    concurrency = params.get("concurrency")
    if concurrency is not None and not _positive_int(concurrency):
        problems.append("concurrency must be a positive integer")
    duration = params.get("duration_s")
    if duration is not None and not (_number(duration) and duration > 0):
        problems.append("duration_s must be a positive number of seconds")
    if workload is not None:
        subject = f"{workload.kind} workload {workload.name!r}"
        if workload.kind == "timestamped_trace":
            if rate is not None:
                problems.append(f"{subject} replays its recorded timestamps and takes no rate")
        else:
            if duration is None:
                problems.append(f"{subject} needs duration_s")
            if rate is None and concurrency is None:
                problems.append(f"{subject} needs a rate, a concurrency or both")
    if problems:
        raise ValueError("; ".join(problems))
    assert workload is not None
    pacing = {"rate": rate, "concurrency": concurrency, "duration_s": duration}
    return {
        "workload": workload.name,
        "kind": workload.kind,
        **{key: value for key, value in pacing.items() if value is not None},
    }
