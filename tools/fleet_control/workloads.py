"""Load-job settings: the AIPerf client, the served model and the workload library."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

WorkloadKind = Literal[
    "synthetic", "mixed", "multi_turn", "public_dataset", "timestamped_trace", "prefix_trace"
]
KINDS: tuple[WorkloadKind, ...] = (
    "synthetic",
    "mixed",
    "multi_turn",
    "public_dataset",
    "timestamped_trace",
    "prefix_trace",
)
# AIPerf generates the prompts of these kinds, so only they take prompt-shaping keys.
GENERATED_KINDS = frozenset({"synthetic", "mixed", "multi_turn"})
TRACE_KINDS = frozenset({"timestamped_trace", "prefix_trace"})
PUBLIC_DATASETS = ("sharegpt",)
# AIPerf's --cache-bust targets: where a marker unique to each session defeats prefix reuse.
CACHE_BUST_TARGETS = ("system_prefix", "system_suffix", "first_turn_prefix", "first_turn_suffix")
ENDPOINT_TYPES = ("chat", "completions")
DEFAULT_ENDPOINT_TYPE = "chat"
# AIPerf's arrival pattern for each job `arrival`. `bursty` is a gamma process of this smoothness.
ARRIVALS = {"steady": "constant", "random": "poisson", "bursty": "gamma"}
BURSTY_SMOOTHNESS = 0.5
PARAMS = (
    "workload",
    "rate",
    "arrival",
    "concurrency",
    "ramp_s",
    "duration_s",
    "requests",
    "warmup_requests",
)
LABEL_MAX = 60
DESCRIPTION_MAX = 300

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
_COMMON_KEYS = {"kind", "label", "description", "ignore_eos", "cache_bust"}
_CANCEL_KEYS = {"cancel_percent", "cancel_after_s"}
_SHAPING_KEYS = {"system_prompt_tokens", "prefix_prompts", "prefix_tokens"}
_LENGTH_KEYS = {"isl", "osl", "isl_stddev", "osl_stddev"}
_TURN_KEYS = {"turns", "turns_stddev", "turn_delay_s", "turn_delay_stddev_s"}
_WORKLOAD_KEYS: dict[WorkloadKind, set[str]] = {
    "synthetic": _COMMON_KEYS | _CANCEL_KEYS | _SHAPING_KEYS | _LENGTH_KEYS,
    "mixed": _COMMON_KEYS | _CANCEL_KEYS | _SHAPING_KEYS | {"mix"},
    "multi_turn": _COMMON_KEYS | _CANCEL_KEYS | _SHAPING_KEYS | _LENGTH_KEYS | _TURN_KEYS,
    "public_dataset": _COMMON_KEYS | _CANCEL_KEYS | {"dataset"},
    "timestamped_trace": _COMMON_KEYS | _CANCEL_KEYS | {"file", "block_size"},
    "prefix_trace": _COMMON_KEYS | _CANCEL_KEYS | {"file", "block_size"},
}
_MIX_KEYS = {"isl", "osl", "isl_stddev", "osl_stddev", "percent"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_METRIC_TAG = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class MixEntry:
    """One request size of a `mixed` workload and its share of the requests, in percent."""

    isl: int
    osl: int
    percent: float
    isl_stddev: float = 0.0
    osl_stddev: float = 0.0


@dataclass(frozen=True)
class Workload:
    """One named library entry: generated prompts, a public dataset or a trace file.

    A `timestamped_trace` replays its records at their recorded timestamps. A `prefix_trace`
    reuses prompt prefixes through the `hash_ids` of its records and is paced by the job's
    rate and concurrency instead of any recorded timestamps.
    """

    name: str
    kind: WorkloadKind
    label: str = ""
    description: str = ""
    isl: int | None = None
    osl: int | None = None
    isl_stddev: float = 0.0
    osl_stddev: float = 0.0
    mix: tuple[MixEntry, ...] = ()
    turns: int | None = None
    turns_stddev: float = 0.0
    turn_delay_s: float = 0.0
    turn_delay_stddev_s: float = 0.0
    dataset: str | None = None
    file: Path | None = None
    block_size: int | None = None
    system_prompt_tokens: int | None = None
    prefix_prompts: int | None = None
    prefix_tokens: int | None = None
    cache_bust: str | None = None
    ignore_eos: bool = False
    cancel_percent: float | None = None
    cancel_after_s: float = 0.0

    def document(self) -> dict[str, Any]:
        """Return the library entry as recorded with a job."""
        document: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "label": self.label or self.name,
            "description": self.description,
        }
        if self.kind in ("synthetic", "multi_turn"):
            document.update(
                isl=self.isl, isl_stddev=self.isl_stddev, osl=self.osl, osl_stddev=self.osl_stddev
            )
        if self.kind == "multi_turn":
            document.update(
                turns=self.turns,
                turns_stddev=self.turns_stddev,
                turn_delay_s=self.turn_delay_s,
                turn_delay_stddev_s=self.turn_delay_stddev_s,
            )
        if self.kind == "mixed":
            document["mix"] = [asdict(entry) for entry in self.mix]
        if self.kind == "public_dataset":
            document["dataset"] = self.dataset
        if self.kind in TRACE_KINDS:
            document.update(file=str(self.file), block_size=self.block_size)
        for key in ("system_prompt_tokens", "prefix_prompts", "prefix_tokens", "cache_bust"):
            if getattr(self, key) is not None:
                document[key] = getattr(self, key)
        document["ignore_eos"] = self.ignore_eos
        if self.cancel_percent is not None:
            document.update(cancel_percent=self.cancel_percent, cancel_after_s=self.cancel_after_s)
        return document

    def chat_only(self) -> str:
        """Return why the workload needs the chat endpoint, or an empty string."""
        if self.kind == "multi_turn":
            return "a multi_turn workload"
        if self.kind == "public_dataset":
            return "a public_dataset workload"
        if self.system_prompt_tokens is not None:
            return "system_prompt_tokens"
        if self.cache_bust is not None:
            return "cache_bust"
        return ""


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
        extra = []
    workloads = _read_workloads(problems, raw.get("workloads"))
    for name, workload in workloads.items():
        reason = workload.chat_only()
        if endpoint_type == "completions" and reason:
            problems.append(f"load.workloads.{name}: {reason} needs endpoint_type chat")
    # AIPerf's handling of a repeated --extra-inputs is unverified, so one source must own it.
    if "--extra-inputs" in extra and any(w.ignore_eos for w in workloads.values()):
        problems.append("load.extra_args sets --extra-inputs, so no workload may set ignore_eos")
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
        count = len(problems)
        fields = _read_common(problems, label, spec)
        if kind in GENERATED_KINDS:
            fields.update(_read_shaping(problems, label, spec))
        if kind in ("synthetic", "multi_turn"):
            fields.update(_read_lengths(problems, label, spec))
        if kind == "multi_turn":
            fields.update(_read_turns(problems, label, spec))
        if kind == "mixed":
            fields["mix"] = _read_mix(problems, label, spec.get("mix"))
        if kind == "public_dataset":
            fields["dataset"] = spec.get("dataset")
            if fields["dataset"] not in PUBLIC_DATASETS:
                problems.append(f"{label}.dataset must be one of {', '.join(PUBLIC_DATASETS)}")
        if kind in TRACE_KINDS:
            fields.update(_read_trace(problems, label, kind, spec))
        if len(problems) == count and not unknown:
            workloads[name] = Workload(name, kind, **fields)
    return workloads


def _read_common(problems: list[str], label: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, limit in (("label", LABEL_MAX), ("description", DESCRIPTION_MAX)):
        value = spec.get(key, "")
        if key in spec and not (_text(value) and len(value) <= limit):
            problems.append(f"{label}.{key} must be non-empty text of at most {limit} characters")
        fields[key] = str(value).strip()
    ignore_eos = spec.get("ignore_eos", False)
    if not isinstance(ignore_eos, bool):
        problems.append(f"{label}.ignore_eos must be true or false")
    fields["ignore_eos"] = bool(ignore_eos)
    cache_bust = spec.get("cache_bust")
    if cache_bust is not None and cache_bust not in CACHE_BUST_TARGETS:
        problems.append(f"{label}.cache_bust must be one of {', '.join(CACHE_BUST_TARGETS)}")
    fields["cache_bust"] = cache_bust
    percent = spec.get("cancel_percent")
    if percent is not None and not (_number(percent) and 0 < percent <= 100):
        problems.append(f"{label}.cancel_percent must be a number above 0 and at most 100")
    delay = spec.get("cancel_after_s", 0)
    if not (_number(delay) and delay >= 0):
        problems.append(f"{label}.cancel_after_s must be a non-negative number of seconds")
    elif "cancel_after_s" in spec and percent is None:
        problems.append(f"{label}.cancel_after_s needs cancel_percent")
    fields["cancel_percent"] = None if percent is None else float(percent)
    fields["cancel_after_s"] = float(delay) if _number(delay) else 0.0
    return fields


def _read_shaping(problems: list[str], label: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("system_prompt_tokens", "prefix_prompts", "prefix_tokens"):
        value = spec.get(key)
        if value is not None and not _positive_int(value):
            problems.append(f"{label}.{key} must be a positive integer")
        fields[key] = value
    if ("prefix_prompts" in spec) != ("prefix_tokens" in spec):
        problems.append(f"{label}: prefix_prompts and prefix_tokens are set together")
    if "system_prompt_tokens" in spec and "prefix_prompts" in spec:
        problems.append(f"{label}: system_prompt_tokens and prefix_prompts are exclusive")
    return fields


def _read_lengths(problems: list[str], label: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    problems.extend(
        f"{label}.{key} must be a positive number of tokens"
        for key in ("isl", "osl")
        if not _positive_int(spec.get(key))
    )
    fields: dict[str, Any] = {"isl": spec.get("isl"), "osl": spec.get("osl")}
    for key in ("isl_stddev", "osl_stddev"):
        value = spec.get(key, 0)
        if not (_number(value) and value >= 0):
            problems.append(f"{label}.{key} must be a non-negative number of tokens")
        fields[key] = float(value) if _number(value) else 0.0
    return fields


def _read_turns(problems: list[str], label: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    turns = spec.get("turns")
    if not (_positive_int(turns) and turns >= 2):
        problems.append(f"{label}.turns must be an integer of at least 2")
    fields: dict[str, Any] = {"turns": turns}
    for key, unit in (
        ("turns_stddev", "turns"),
        ("turn_delay_s", "seconds"),
        ("turn_delay_stddev_s", "seconds"),
    ):
        value = spec.get(key, 0)
        if not (_number(value) and value >= 0):
            problems.append(f"{label}.{key} must be a non-negative number of {unit}")
        fields[key] = float(value) if _number(value) else 0.0
    return fields


def _read_mix(problems: list[str], label: str, raw: object) -> tuple[MixEntry, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{label}.mix must be a non-empty list of request sizes")
        return ()
    entries = []
    for index, item in enumerate(raw):
        where = f"{label}.mix[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        problems.extend(f"unknown key {where}.{key}" for key in sorted(set(item) - _MIX_KEYS))
        count = len(problems)
        lengths = _read_lengths(problems, where, item)
        percent = item.get("percent")
        if not (_number(percent) and 0 < percent <= 100):
            problems.append(f"{where}.percent must be a number above 0 and at most 100")
        if len(problems) == count:
            entries.append(MixEntry(percent=float(percent), **lengths))
    if len(entries) == len(raw) and not math.isclose(
        sum(entry.percent for entry in entries), 100, abs_tol=1e-6
    ):
        problems.append(f"{label}.mix percentages must sum to 100")
    return tuple(entries)


def _read_trace(
    problems: list[str], label: str, kind: WorkloadKind, spec: Mapping[str, Any]
) -> dict[str, Any]:
    if not _text(spec.get("file")):
        problems.append(f"{label}.file must name a mooncake_trace JSONL file")
    block_size = spec.get("block_size")
    if kind == "prefix_trace" and block_size is None:
        problems.append(f"{label}.block_size is required: the tokens each hash ID stands for")
    elif block_size is not None and not _positive_int(block_size):
        problems.append(f"{label}.block_size must be a positive number of tokens")
    return {"file": Path(str(spec.get("file"))), "block_size": block_size}


def validate_params(workloads: Mapping[str, Workload], params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a job's normalized parameters, or raise ValueError naming every problem.

    `rate` is requests per second and `arrival` its spacing: `steady`, `random` or `bursty`.
    `concurrency` is the in-flight request limit and `ramp_s` the seconds to reach the rate or
    concurrency. The job stops after `duration_s` seconds or `requests` requests, whichever
    comes first. `warmup_requests` run before measurement. A workload other than a timestamped
    trace needs a rate, a concurrency or both, and a duration, a request count or both. A
    timestamped trace keeps its recorded arrival times, so it takes no rate, arrival, ramp or
    warm-up; `concurrency`, `duration_s` and `requests` optionally cap its replay.
    """
    problems = [f"unknown job parameter {key!r}" for key in sorted(set(params) - set(PARAMS))]
    name = params.get("workload")
    workload = workloads.get(name) if isinstance(name, str) else None
    if workload is None:
        problems.append(f"workload must name a library entry: {', '.join(sorted(workloads))}")
    rate = params.get("rate")
    if rate is not None and not (_number(rate) and rate > 0):
        problems.append("rate must be a positive number of requests per second")
    arrival = params.get("arrival")
    if arrival is not None and arrival not in ARRIVALS:
        problems.append(f"arrival must be one of {', '.join(ARRIVALS)}")
    problems.extend(
        f"{key} must be a positive integer"
        for key in ("concurrency", "requests", "warmup_requests")
        if params.get(key) is not None and not _positive_int(params[key])
    )
    for key in ("duration_s", "ramp_s"):
        value = params.get(key)
        if value is not None and not (_number(value) and value > 0):
            problems.append(f"{key} must be a positive number of seconds")
    if arrival is not None and rate is None:
        problems.append("arrival needs a rate")
    if workload is not None:
        subject = f"{workload.kind} workload {workload.name!r}"
        if workload.kind == "timestamped_trace":
            timed = ("rate", "arrival", "ramp_s", "warmup_requests")
            refused = [key for key in timed if params.get(key) is not None]
            if refused:
                problems.append(
                    f"{subject} replays its recorded timestamps and takes no {', '.join(refused)}"
                )
        else:
            if params.get("duration_s") is None and params.get("requests") is None:
                problems.append(f"{subject} needs duration_s, requests or both")
            if rate is None and params.get("concurrency") is None:
                problems.append(f"{subject} needs a rate, a concurrency or both")
    if problems:
        raise ValueError("; ".join(problems))
    assert workload is not None
    pacing = {key: params.get(key) for key in PARAMS[1:]}
    return {
        "workload": workload.name,
        "kind": workload.kind,
        **{key: value for key, value in pacing.items() if value is not None},
    }
