"""Run a stage graph in a run directory, skipping each stage whose recorded result still holds.

A stage instance is skipped when its last attempt passed, its stage definition is unchanged,
the digests of the facts that rerun it match, every output still matches its SHA-256, and
every live fact it produces is still there. A changed fact reruns the stages its rule names.
A changed output reruns its stage, and the record marks every stage after it `invalidated`
before anything reruns, so a stop before those stages run does not lose them.

Each attempt writes its outputs to its own directory, `stages/<stage>/[<instance>/]attempt-<n>`,
so a rerun never meets an earlier attempt's files. SIGINT and SIGTERM end the current stage:
the stage's own cleanup runs, the record keeps the attempt as cancelled, and the run stops.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from narwhal import __version__
from narwhal.contracts import canonical_digest

from . import run_record, stages
from .graph import INPUT, LIVE, PRODUCT, Fact, Rerun, Stage, StageGraph
from .launch_engine.runtime import regular_file_digest
from .run_record import (
    CANCELLED,
    FAILED,
    INSTANCE,
    INVALIDATED,
    PASSED,
    RUNNING,
    TARGETS,
    TIMEOUT,
    RunLocked,
    key,
)

__all__ = [
    "Command",
    "Decision",
    "HelpersSurvived",
    "Input",
    "LiveCheck",
    "Outcome",
    "Refreshed",
    "RunCancelled",
    "RunLocked",
    "RunResult",
    "Runner",
    "StageContext",
    "StageFailed",
    "StaticTopology",
    "Topology",
]

# A temporary record that a crash left before its rename.
_TEMPORARY = re.compile(r"\." + re.escape(run_record.RECORD) + r"-[0-9a-f]{32}")
# Where a stage that left the graph sends the operator.
_GRAPH_CHANGES = "Deploy.md#rerunning-after-a-change"


@dataclass(frozen=True)
class Input:
    """One run input: its digest and where the value came from. Secrets enter only as digests."""

    digest: str
    source: str

    @classmethod
    def value(cls, value: Any, source: str) -> Input:
        return cls(canonical_digest(value), source)

    @classmethod
    def file(cls, path: Path) -> Input:
        return cls("sha256:" + regular_file_digest(path), str(path))


@dataclass(frozen=True)
class Outcome:
    """What a passed stage returns: one value per fact it produces, and the processes it started.

    `live` names the files and directories in the attempt directory that those processes keep
    writing, such as their logs. The run record lists them without a digest, so their growth
    never reruns the stage; the processes' own live checks decide whether it still holds.
    """

    products: Mapping[str, Any] = field(default_factory=dict)
    processes: tuple[Mapping[str, Any], ...] = ()
    live: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Command:
    """A stage that runs one command under `stages.run` in its attempt directory.

    `argv` and `env` are built per instance. Credentials belong in `env`, because the run
    record keeps `argv`. `outcome` reads the products from the attempt's files.
    """

    argv: Callable[[StageContext], Sequence[str]]
    env: Callable[[StageContext], Mapping[str, str]] | None = None
    timeout: float | None = None
    default_timeout: float = 300
    outcome: Callable[[StageContext], Outcome] | None = None


@dataclass(frozen=True)
class Refreshed:
    """What a probe returns for a supervised process that restarted and passed its check again.

    `value` is the process's new identity. The record keeps it in place of the old one, for the
    stage that started the process too, so that stage holds and only the stages after it that
    the fact reruns run again.
    """

    value: Any


Action = Callable[["StageContext"], "Outcome | None"] | Command
# A probe reads a live fact again from the value its stage recorded. It returns the current
# value, `Refreshed` for a restarted process that passed its check, or None when the process
# or route the value names is gone.
Probe = Callable[[str, Any], Any]


@dataclass(frozen=True)
class LiveCheck:
    """What `Runner.inspect` found for one live fact a passed stage recorded."""

    key: str
    stage: str
    state: str
    detail: str = ""


class Topology(Protocol):
    """The instances of each scope, their capabilities and the named relations between them.

    The runner asks for a scope's instances when it first reaches a stage of that scope, so
    a topology can read them from the outputs of earlier stages.
    """

    def instances(self, scope: str) -> Sequence[str]: ...

    def capable(self, capability: str, instance: str) -> bool: ...

    def related(self, relation: str, instance: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class StaticTopology:
    """A topology known before the run starts."""

    scopes: Mapping[str, Sequence[str]]
    relations: Mapping[str, Mapping[str, Sequence[str]]] = field(default_factory=dict)
    capabilities: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def instances(self, scope: str) -> Sequence[str]:
        if scope not in self.scopes:
            raise ValueError(f"topology has no instances for scope {scope}")
        return tuple(self.scopes[scope])

    def capable(self, capability: str, instance: str) -> bool:
        if capability not in self.capabilities:
            raise ValueError(f"topology does not describe capability {capability}")
        return instance in self.capabilities[capability]

    def related(self, relation: str, instance: str) -> Sequence[str]:
        if instance not in self.relations.get(relation, {}):
            raise ValueError(f"topology relation {relation} has no entry for {instance}")
        return tuple(self.relations[relation][instance])


@dataclass(frozen=True)
class Decision:
    """Whether one stage instance ran, and why."""

    key: str
    ran: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RunResult:
    run_id: str
    decisions: tuple[Decision, ...]
    stale_lock: Mapping[str, Any] | None


class StageFailed(RuntimeError):
    """A stage failed or timed out; `record` is its stage record with the failure and recovery.

    `stale_lock`, as on every error the run raises after taking its lock, names the owner of a
    stale lock this run found; the record's `stale_locks` keeps it too.
    """

    def __init__(self, key: str, record: Mapping[str, Any]) -> None:
        self.key = key
        self.record = record
        self.stale_lock: Mapping[str, Any] | None = None
        failure = record["failure"]
        detail = f"{failure['message']}; see {failure['recovery']}"
        super().__init__(f"{key} {record['status']}: {detail}")


class RunCancelled(KeyboardInterrupt):
    """SIGINT or SIGTERM ended the run during a stage, after its cleanup."""

    def __init__(self, key: str, record: Mapping[str, Any]) -> None:
        self.key = key
        self.record = record
        self.stale_lock: Mapping[str, Any] | None = None
        super().__init__(f"{key} cancelled; inspect {record['directory']}")


class HelpersSurvived(RuntimeError):
    """Helpers of a stage an earlier runner left running survived their cleanup."""

    def __init__(self, key: str, survivors: Mapping[str, Any], holder: str) -> None:
        self.key = key
        self.survivors = survivors
        self.stale_lock: Mapping[str, Any] | None = None
        super().__init__(
            f"{key}: helper processes {dict(survivors)} of {holder} survived cleanup; "
            "stop them and run again"
        )


class StageContext:
    """What a stage action sees: its instance, its attempt directory and the upstream results.

    `previous` is this instance's record from before the attempt, or None. A stage that starts
    long-lived processes reads the `processes` of an interrupted attempt there, so it can stop
    or adopt them instead of starting them twice.
    """

    def __init__(
        self,
        run: _Run,
        stage: str,
        instance: str,
        attempt: int,
        directory: Path,
        previous: Mapping[str, Any] | None,
    ) -> None:
        self._run = run
        self.stage = stage
        self.instance = instance
        self.attempt = attempt
        self.directory = directory
        self.run_directory = run.directory
        self.run_id: str = run.record["run_id"]
        self.previous = previous

    def product(self, fact: str, instance: str = "") -> Any:
        """The current value of a fact a stage before this one produced."""
        producer = self._run.graph.producer(fact)
        if producer not in self._run.graph.upstream(self.stage):
            raise ValueError(f"{self.stage} does not run after {producer}, which produces {fact}")
        return self._run.values[key(fact, instance)]

    def outputs(self, stage: str, instance: str = "") -> Path:
        """The attempt directory of a passed stage before this one."""
        if stage not in self._run.graph.upstream(self.stage):
            raise ValueError(f"{self.stage} does not run after {stage}")
        return self._run.directory / self._run.record["stages"][key(stage, instance)]["directory"]


class Runner:
    """Runs one stage graph for one target in one run directory."""

    def __init__(
        self,
        graph: StageGraph,
        directory: Path,
        *,
        target: str,
        revision: str,
        actions: Mapping[str, Action],
        topology: Topology,
        probes: Mapping[str, Probe],
    ) -> None:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {', '.join(TARGETS)}")
        if unknown := sorted(set(actions) - set(graph.stages)):
            raise ValueError(f"actions name unknown stages: {', '.join(unknown)}")
        live = {name for name, fact in graph.facts.items() if fact.kind == LIVE}
        if set(probes) != live:
            raise ValueError(f"probes must cover exactly the live facts: {', '.join(sorted(live))}")
        self.graph = graph
        self.directory = directory
        self.target = target
        self.revision = revision
        self.actions = dict(actions)
        self.topology = topology
        self.probes = dict(probes)

    def run(self, inputs: Mapping[str, Input], *, until: str | None = None) -> RunResult:
        """Run every stage up to and including `until`, or the whole graph."""
        order = self._order(until)
        self._check_inputs(inputs)
        with contextlib.suppress(FileExistsError):
            self.directory.mkdir(mode=0o700)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"{self.directory} must be a directory this user owns")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(
                f"{self.directory} has mode {stat.S_IMODE(info.st_mode):04o}, which lets other "
                "users in; run chmod 700 on it"
            )
        with run_record.locked(self.directory), stages.cancellation():
            return _Run(self, inputs, until, order).execute()

    def plan(self, inputs: Mapping[str, Input], *, until: str | None = None) -> RunResult:
        """Decide every stage up to `until` as `run` would, without running or recording one.

        Each live fact is checked again, read-only. Once a stage would run, every stage after
        it is reported as running after it, and its instances are not looked up.
        """
        order = self._order(until)
        self._check_inputs(inputs)
        return _Run(self, inputs, until, order, dry=True).execute()

    def inspect(self, *, refresh: bool) -> tuple[dict[str, Any], tuple[LiveCheck, ...]]:
        """Check the live facts of every passed stage; return the record and what was found.

        With `refresh`, a restarted process that passed its check again replaces its recorded
        identity in the record, under the run's lock.
        """
        if not (self.directory / run_record.RECORD).exists():
            raise ValueError(f"{self.directory} holds no run record; run narwhal deploy run")
        guard = run_record.locked(self.directory) if refresh else contextlib.nullcontext()
        with guard:
            record = run_record.read(self.directory)
            checks = []
            changed = False
            for name, entry in record["stages"].items():
                spec = self.graph.stages.get(entry["stage"])
                if spec is None or entry["status"] != PASSED:
                    continue
                for fact in spec.produces:
                    if self.graph.facts[fact].kind != LIVE:
                        continue
                    fact_key = key(fact, entry["instance"])
                    try:
                        current = self.probes[fact](entry["instance"], entry["products"][fact])
                    except Exception as error:  # a probe's own failure is what status reports
                        checks.append(LiveCheck(fact_key, name, "unreadable", str(error)))
                        continue
                    if current is None:
                        checks.append(LiveCheck(fact_key, name, "gone"))
                    elif isinstance(current, Refreshed):
                        checks.append(LiveCheck(fact_key, name, "restarted and checked"))
                        if refresh:
                            _refresh(entry, fact_key, fact, current.value)
                            changed = True
                    elif canonical_digest(current) != canonical_digest(entry["products"][fact]):
                        checks.append(LiveCheck(fact_key, name, "changed"))
                    else:
                        checks.append(LiveCheck(fact_key, name, "holds"))
            if changed:
                record["updated_at"] = time.time()
                with _held():
                    run_record.write(self.directory, record)
        return record, tuple(checks)

    def _order(self, until: str | None) -> tuple[str, ...]:
        order = self.graph.order()
        if until is not None:
            if until not in self.graph.stages:
                raise ValueError(f"unknown stage {until}")
            order = order[: order.index(until) + 1]
        if missing := [name for name in order if name not in self.actions]:
            raise ValueError(f"no action for stages: {', '.join(missing)}")
        return order

    def _check_inputs(self, inputs: Mapping[str, Input]) -> None:
        expected = {name for name, fact in self.graph.facts.items() if fact.kind == INPUT}
        if set(inputs) != expected:
            absent = ", ".join(sorted(expected - set(inputs))) or "none"
            extra = ", ".join(sorted(set(inputs) - expected)) or "none"
            raise ValueError(f"inputs differ from the graph: missing {absent}; unknown {extra}")


def _refresh(entry: dict[str, Any], fact_key: str, fact: str, value: Any) -> None:
    """Record a restarted process's checked identity in place of the identity its stage started."""
    value = json.loads(json.dumps(value))
    entry["products"][fact] = value
    if fact_key in entry["input_digests"]:
        entry["input_digests"][fact_key] = canonical_digest(value)


class _Run:
    """One pass over the graph, holding the run record and the current value of every fact."""

    def __init__(
        self,
        runner: Runner,
        inputs: Mapping[str, Input],
        until: str | None,
        order: Sequence[str],
        *,
        dry: bool = False,
    ) -> None:
        self.runner = runner
        # A dry pass decides each stage without running one or writing the record.
        self.dry = dry
        # In a dry pass, the first instance of each stage that would run, and the facts of
        # stages that would run, whose values only that run decides.
        self.pending: dict[str, str] = {}
        self.unknown: list[str] = []
        self.graph = runner.graph
        self.directory = runner.directory
        self.inputs = inputs
        self.until = until
        self.order = order
        self.values: dict[str, Any] = {}
        self.decisions: list[Decision] = []
        self.scopes: dict[str, tuple[str, ...]] = {}
        self.stage_instances: dict[str, tuple[str, ...]] = {}
        # Stage instances that passed in this run, with this run's inputs.
        self.passed: set[str] = set()
        self.record: dict[str, Any] = {}
        self.opened = False

    def execute(self) -> RunResult:
        if self.dry:
            return self._plan()
        stale = None
        try:
            stale = self._open()
            self._recover(stale)
            for name in self.order:
                self._sweep(self.graph.stages[name])
        except (StageFailed, RunCancelled, HelpersSurvived) as error:
            error.stale_lock = stale
            if stale is not None:
                error.add_note(f"this run found a stale lock of {run_record.describe(stale)}")
            raise
        finally:
            # Once the record names this runner, every way out clears it again.
            if self.opened:
                with _held():
                    self.record.update(lock=None, updated_at=time.time())
                    run_record.write(self.directory, self.record)
        return RunResult(self.record["run_id"], tuple(self.decisions), stale)

    def _plan(self) -> RunResult:
        if (self.directory / run_record.RECORD).exists():
            self.record = run_record.read(self.directory)
            if self.record["target"] != self.runner.target:
                raise ValueError(f"{self.directory} holds a {self.record['target']} run")
        else:
            self.record = {"run_id": "0" * 32, "stages": {}}
        # Stages whose changed outputs mark every stage after them invalidated.
        changed: dict[str, str] = {}
        for name in self.order:
            spec = self.graph.stages[name]
            pending = [stage for stage in self.pending if stage in self.graph.upstream(name)]
            try:
                instances = self._stage_instances(spec)
            except (ValueError, KeyError, OSError):
                # The topology reads this scope from a stage that has not passed yet.
                if not pending:
                    raise
                self.pending[name] = name
                self.decisions.append(
                    Decision(name, True, (f"runs after {self.pending[pending[-1]]}",))
                )
                continue
            invalidated = next((changed[stage] for stage in changed if stage in pending), None)
            for instance in instances:
                stage_key = key(spec.name, instance)
                previous = self.record["stages"].get(stage_key)
                self.unknown = []
                reasons = self._reasons(spec, instance, previous)
                if invalidated is not None and not reasons:
                    reasons = [f"runs after {invalidated}, whose output changed"]
                elif self.unknown and not reasons:
                    fact_key = self.unknown[0]
                    producer = self.graph.producer(fact_key.split("[", 1)[0])
                    reasons = [f"runs after {producer} if {fact_key} changes"]
                self.decisions.append(Decision(stage_key, bool(reasons), tuple(reasons)))
                if reasons:
                    self.pending.setdefault(name, stage_key)
                    if any(reason.startswith("output ") for reason in reasons):
                        changed.setdefault(name, stage_key)
                else:
                    assert previous is not None
                    for fact in spec.produces:
                        if self.graph.facts[fact].kind != LIVE:
                            self.values[key(fact, instance)] = previous["products"][fact]
        return RunResult(self.record["run_id"], tuple(self.decisions), None)

    def _open(self) -> dict[str, Any] | None:
        now = time.time()
        stale = None
        # Under the lock no other writer exists, so a temporary record is a crash's leftover.
        for path in self.directory.iterdir():
            if _TEMPORARY.fullmatch(path.name):
                path.unlink()
        if (self.directory / run_record.RECORD).exists():
            record = run_record.read(self.directory)
            if record["target"] != self.runner.target:
                raise ValueError(f"{self.directory} holds a {record['target']} run")
            if record["lock"] is not None:
                stale = record["lock"]["owner"]
                if run_record.owner_running(stale):
                    raise RunLocked(
                        self.directory, stale, "the recorded owner runs without holding run.lock"
                    )
                record["stale_locks"].append({**record["lock"], "found_at": now})
        else:
            names = (path.name for path in self.directory.iterdir())
            if others := sorted(name for name in names if name != run_record.LOCK):
                raise ValueError(f"{self.directory} holds {', '.join(others)} but no run record")
            record = {
                "run_id": uuid.uuid4().hex,
                "created_at": now,
                "stale_locks": [],
                "stages": {},
            }
        record.update(
            target=self.runner.target,
            narwhal_version=__version__,
            revision=self.runner.revision,
            graph_sha256=self.graph.digest(),
            updated_at=now,
            lock={"owner": run_record.owner(), "acquired_at": now},
            inputs={
                name: {"digest": value.digest, "source": value.source}
                for name, value in sorted(self.inputs.items())
            },
            until=self.until,
        )
        self.record = record
        with _held():
            run_record.write(self.directory, record)
            self.opened = True
        return stale

    def _recover(self, stale: Mapping[str, Any] | None) -> None:
        """Stop the helpers of a stage an earlier runner left running, and record it as failed."""
        holder = run_record.describe(stale) if stale else "an earlier runner"
        for name, entry in self.record["stages"].items():
            if entry["status"] != RUNNING:
                continue
            cleanups = {}
            survivors = {}
            for evidence in sorted((self.directory / entry["directory"]).rglob("*.stage.json")):
                helper = json.loads(evidence.read_text())
                if helper.get("status") not in {"running", "recovery_required"}:
                    continue
                relative = str(evidence.relative_to(self.directory))
                cleanups[relative] = stages.recover(evidence)
                if cleanups[relative]["surviving_processes"]:
                    survivors[relative] = cleanups[relative]["surviving_processes"]
            if survivors:
                raise HelpersSurvived(name, survivors, holder)
            spec = self.graph.stages.get(entry["stage"])
            message = f"{holder} ended during this stage"
            if spec is None:
                message += ", which the current stage graph no longer has"
            entry.update(
                status=FAILED,
                ended_at=time.time(),
                failure={
                    "message": message,
                    "recovery": spec.page if spec else _GRAPH_CHANGES,
                    "context": {"owner": dict(stale) if stale else None, "helpers": cleanups},
                },
            )
        with _held():
            run_record.write(self.directory, self.record)

    def _instances(self, scope: str) -> tuple[str, ...]:
        if self.graph.scopes[scope].single:
            return ("",)
        if scope not in self.scopes:
            instances = tuple(self.runner.topology.instances(scope))
            for instance in instances:
                if not INSTANCE.fullmatch(instance):
                    raise ValueError(f"{scope} instance {instance!r} must match {INSTANCE.pattern}")
            if len(set(instances)) != len(instances):
                raise ValueError(f"{scope} instances repeat a name")
            self.scopes[scope] = instances
        return self.scopes[scope]

    def _stage_instances(self, spec: Stage) -> tuple[str, ...]:
        """The instances a stage runs for: its scope's, narrowed to those with its capability."""
        if spec.name not in self.stage_instances:
            instances = self._instances(spec.scope)
            capability = spec.capability
            if capability is not None:
                instances = tuple(
                    instance
                    for instance in instances
                    if self.runner.topology.capable(capability, instance)
                )
            self.stage_instances[spec.name] = instances
        return self.stage_instances[spec.name]

    def _fact_instances(self, fact: Fact) -> tuple[str, ...]:
        if self.graph.scopes[fact.scope].single:
            return ("",)
        return self._stage_instances(self.graph.stages[self.graph.producer(fact.name)])

    def _sweep(self, spec: Stage) -> None:
        instances = self._stage_instances(spec)
        for instance in instances:
            self._stage(spec, instance, first=True)
        if self._reruns_itself(spec):
            # A changed product of this stage invalidated the instances that had not passed in
            # this run, some of them before the change; they rerun once here. Every instance
            # has then passed in this run, so no later change can invalidate one again.
            for instance in instances:
                self._stage(spec, instance)

    def _reruns_itself(self, spec: Stage) -> bool:
        return any(
            fact.kind == PRODUCT and self.graph.producer(fact.name) == spec.name
            for fact, _ in self.graph.reruns_of(spec.name)
        )

    def _stage(self, spec: Stage, instance: str, first: bool = False) -> bool:
        name = key(spec.name, instance)
        previous = self.record["stages"].get(name)
        reasons = self._reasons(spec, instance, previous)
        if first or reasons:
            self.decisions.append(Decision(name, bool(reasons), tuple(reasons)))
        if not reasons:
            assert previous is not None
            for fact in spec.produces:
                if self.graph.facts[fact].kind != LIVE:
                    self.values[key(fact, instance)] = previous["products"][fact]
            return False
        self._attempt(spec, instance, previous)
        return True

    def _reasons(self, spec: Stage, instance: str, previous: Mapping[str, Any] | None) -> list[str]:
        if previous is None:
            return ["no attempt yet"]
        if previous["status"] == INVALIDATED:
            return [previous["invalidation"]]
        if previous["status"] != PASSED:
            return [f"attempt {previous['attempt']} {previous['status']}"]
        changed = [output["path"] for output in previous["outputs"] if not self._matches(output)]
        if changed and not self.dry:
            self._invalidate_later(spec, instance)
        reasons = [f"output {path} changed" for path in changed]
        if previous["definition"] != self.graph.definition(spec.name):
            return [*reasons, "the stage definition changed"]
        for fact in spec.produces:
            if self.graph.facts[fact].kind == LIVE:
                fact_key = key(fact, instance)
                try:
                    current = self.runner.probes[fact](instance, previous["products"][fact])
                except Exception as error:
                    error.add_note(f"live check of {fact_key} for {key(spec.name, instance)}")
                    raise
                if isinstance(current, Refreshed):
                    # The process restarted and passed its check; the record keeps its new
                    # identity, and the stages after it that the fact names run again.
                    current = json.loads(json.dumps(current.value))
                    if not self.dry:
                        with _held():
                            entry = self.record["stages"][key(spec.name, instance)]
                            _refresh(entry, fact_key, fact, current)
                            run_record.write(self.directory, self.record)
                self.values[fact_key] = current
                if current is None:
                    reasons.append(f"{fact_key} is gone")
        current_digests = self._digests(spec, instance)
        recorded = previous["input_digests"]
        reasons += [
            f"{name} changed"
            for name in sorted((set(current_digests) | set(recorded)) - set(self.unknown))
            if current_digests.get(name) != recorded.get(name)
        ]
        return reasons

    def _invalidate_later(self, spec: Stage, instance: str) -> None:
        """Mark every passed stage after a changed output invalidated, in the record."""
        reason = f"output of {key(spec.name, instance)} changed"
        later = set(self.graph.downstream(spec.name))
        for entry in self.record["stages"].values():
            if (
                entry["stage"] in later
                and entry["status"] == PASSED
                and (
                    self.graph.stages[entry["stage"]].scope != spec.scope
                    or entry["instance"] == instance
                )
            ):
                entry.update(status=INVALIDATED, invalidation=reason)
        with _held():
            run_record.write(self.directory, self.record)

    def _invalidate_others(self, spec: Stage, instance: str, previous: Mapping[str, Any]) -> None:
        """Mark the stage's other instances invalidated when a product that reruns them changed."""
        for fact, _ in self.graph.reruns_of(spec.name):
            if fact.kind != PRODUCT or self.graph.producer(fact.name) != spec.name:
                continue
            fact_key = key(fact.name, instance)
            if fact.name not in previous["products"] or canonical_digest(
                previous["products"][fact.name]
            ) == canonical_digest(self.values[fact_key]):
                continue
            for other in self._stage_instances(spec):
                name = key(spec.name, other)
                entry = self.record["stages"].get(name)
                # An instance that passed in this run already holds this run's result.
                if (
                    other != instance
                    and name not in self.passed
                    and entry is not None
                    and entry["status"] == PASSED
                ):
                    entry.update(status=INVALIDATED, invalidation=f"{fact_key} changed")

    def _digests(self, spec: Stage, instance: str) -> dict[str, str]:
        """The digest of every fact instance whose change reruns this stage instance."""
        digests = {}
        for fact, rerun in self.graph.reruns_of(spec.name):
            for source in self._sources(fact, rerun, spec, instance):
                fact_key = key(fact.name, source)
                if self.dry and fact.kind != INPUT and fact_key not in self.values:
                    self.unknown.append(fact_key)
                    continue
                digests[fact_key] = (
                    self.inputs[fact.name].digest
                    if fact.kind == INPUT
                    else canonical_digest(self.values[fact_key])
                )
        return digests

    def _sources(self, fact: Fact, rerun: Rerun, spec: Stage, instance: str) -> tuple[str, ...]:
        """The fact instances whose change reruns `spec` for `instance` under one rule."""
        if fact.kind == INPUT:
            return ("",)
        if fact.kind == PRODUCT and self.graph.producer(fact.name) == spec.name:
            # A stage's own product reruns its other instances through `_invalidate_others`.
            return ()
        sources = self._fact_instances(fact)
        if rerun.via is not None:
            target = self.graph.relations[rerun.via].target
            matched = []
            for source in sources:
                related = tuple(self.runner.topology.related(rerun.via, source))
                if unknown := set(related) - set(self._instances(target)):
                    raise ValueError(f"relation {rerun.via} names unknown {target} {unknown}")
                if instance in related:
                    matched.append(source)
            return tuple(matched)
        if rerun.every or fact.scope != spec.scope:
            return sources
        return (instance,) if instance in sources else ()

    def _matches(self, output: Mapping[str, Any]) -> bool:
        path = self.directory / output["path"]
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_size == output["size"]
            and f"{stat.S_IMODE(info.st_mode):04o}" == output["mode"]
            and regular_file_digest(path) == output["sha256"]
        )

    def _attempt(self, spec: Stage, instance: str, previous: Mapping[str, Any] | None) -> None:
        name = key(spec.name, instance)
        attempt = previous["attempt"] + 1 if previous else 1
        parts = (spec.name, instance) if instance else (spec.name,)
        relative = Path("stages", *parts, f"attempt-{attempt}")
        entry: dict[str, Any] = {
            "stage": spec.name,
            "scope": spec.scope,
            "instance": instance,
            "status": RUNNING,
            "attempt": attempt,
            "directory": str(relative),
            "definition": self.graph.definition(spec.name),
            "input_digests": {},
            "outputs": [],
            "live_outputs": [],
            # Until this attempt returns, the record keeps the products and processes of the
            # earlier attempt, so an interruption loses neither.
            "products": dict(previous["products"]) if previous else {},
            "processes": list(previous["processes"]) if previous else [],
            "command": None,
            "started_at": time.time(),
            "ended_at": None,
            "failure": None,
            "invalidation": None,
        }
        directory = self.directory / relative
        try:
            # The record names the attempt before its directory exists, so a crash between the
            # two leaves a record the next runner recovers, never an unrecorded directory. A
            # signal held during the write arrives inside this block and cancels the attempt.
            with _held():
                self.record["stages"][name] = entry
                run_record.write(self.directory, self.record)
            directory.parent.mkdir(parents=True, exist_ok=True)
            directory.mkdir(mode=0o700)
            context = StageContext(self, spec.name, instance, attempt, directory, previous)
            outcome = self._execute(self.runner.actions[spec.name], context, entry) or Outcome()
            # The started processes are recorded before anything else can fail or be cancelled.
            entry["processes"] = json.loads(json.dumps([dict(p) for p in outcome.processes]))
            if set(outcome.products) != set(spec.produces):
                raise ValueError(
                    f"{spec.name} must return products {', '.join(spec.produces) or 'none'}"
                )
            products = json.loads(json.dumps(dict(outcome.products)))
            live = self._live(directory, outcome.live)
            outputs = self._outputs(directory, live)
        except KeyboardInterrupt as error:
            self._finish(spec, entry, CANCELLED, error)
            raise RunCancelled(name, entry) from error
        except stages.StageTimeout as error:
            self._finish(spec, entry, TIMEOUT, error)
            raise StageFailed(name, entry) from error
        except Exception as error:
            self._finish(spec, entry, FAILED, error)
            raise StageFailed(name, entry) from error
        except BaseException as error:
            self._finish(spec, entry, FAILED, error)
            raise
        for fact, value in products.items():
            self.values[key(fact, instance)] = value
        with _held():
            entry.update(
                status=PASSED,
                outputs=outputs,
                live_outputs=sorted(
                    os.path.relpath(path, os.path.abspath(self.directory)) for path in live
                ),
                products=products,
                input_digests=self._digests(spec, instance),
                ended_at=time.time(),
            )
            if previous is not None:
                self._invalidate_others(spec, instance, previous)
            run_record.write(self.directory, self.record)
        self.passed.add(name)

    def _execute(self, action: Action, context: StageContext, entry: dict) -> Outcome | None:
        if not isinstance(action, Command):
            return action(context)
        argv = [str(part) for part in action.argv(context)]
        log = context.directory / "command.log"
        entry["command"] = {
            "argv": argv,
            "cwd": entry["directory"],
            "exit_code": None,
            "log": str(log.relative_to(self.directory)),
        }
        result = stages.run(
            argv,
            stage=context.stage,
            log=log,
            cwd=context.directory,
            env=dict(action.env(context)) if action.env is not None else None,
            timeout=action.timeout,
            default_timeout=action.default_timeout,
        )
        entry["command"]["exit_code"] = result.returncode
        if result.returncode:
            raise RuntimeError(f"{argv[0]} exited {result.returncode}; inspect {log}")
        return action.outcome(context) if action.outcome is not None else None

    @staticmethod
    def _live(directory: Path, live: Sequence[Path]) -> set[Path]:
        """The absolute paths of `live`, each inside the attempt directory."""
        attempt = Path(os.path.abspath(directory))
        paths = set()
        for path in live:
            absolute = Path(os.path.abspath(path))
            if absolute == attempt or not absolute.is_relative_to(attempt):
                raise ValueError(f"live output {path} is not inside the attempt directory")
            paths.add(absolute)
        return paths

    def _outputs(
        self, directory: Path, live: AbstractSet[Path] = frozenset()
    ) -> list[dict[str, Any]]:
        outputs = []
        for root, directories, files in os.walk(directory):
            # A live directory's files belong to the processes that write them.
            here = Path(os.path.abspath(root))
            directories[:] = [name for name in directories if here / name not in live]
            for name in sorted(directories + files):
                path = Path(root) / name
                if here / name in live:
                    continue
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"stage output {path} is not a regular file")
                outputs.append(
                    {
                        "path": str(path.relative_to(self.directory)),
                        "sha256": regular_file_digest(path),
                        "size": info.st_size,
                        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                    }
                )
        return sorted(outputs, key=lambda output: output["path"])

    def _finish(self, spec: Stage, entry: dict, status: str, error: BaseException) -> None:
        context = getattr(error, "context", {})
        with _held():
            entry.update(
                status=status,
                ended_at=time.time(),
                failure={
                    "message": str(error) or type(error).__name__,
                    "recovery": spec.page,
                    "context": json.loads(json.dumps(context if isinstance(context, dict) else {})),
                },
            )
            run_record.write(self.directory, self.record)


@contextlib.contextmanager
def _held() -> Iterator[None]:
    """Hold SIGINT and SIGTERM until the record write completes; they arrive right after it."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
