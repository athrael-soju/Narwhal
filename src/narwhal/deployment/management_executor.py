"""Run accepted work independently of clients and reconcile recorded effects."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from narwhal.diagnostics.management_artifacts import ArtifactStore

from . import stages
from .management_access import AccessError, InspectionAccess, clean_command, directory
from .management_exports import public_value
from .management_records import OperationError, utc_now, uuid_string
from .management_registry import ManagementRegistry
from .management_store import OperationStore

_TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass
class StageOutcome:
    """Evidence and outcome from one trusted adapter operation."""

    status: str = "success"
    data: dict[str, Any] = field(default_factory=dict)
    command_result: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReconcileOutcome:
    """Read-only findings; unknown effects or helpers prevent terminal completion."""

    helpers_stopped: bool = False
    complete: bool = False
    effects: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


class ExecutionAdapter(Protocol):
    """Trusted implementations use context checks before every external effect.

    Calls run in the detached worker's main thread. They must honor the context
    deadline, use its owned helper runner and not mask cancellation or deadlines.
    Reconciliation may inspect state but must not stop resources or repeat work.
    """

    def execute_stage(
        self, context: StageContext, stage: dict[str, Any], plan: dict[str, Any]
    ) -> StageOutcome:
        """Run one fixed stage and return its receipt and evidence."""
        ...

    def reconcile(self, context: StageContext, operation: dict[str, Any]) -> ReconcileOutcome:
        """Inspect intentions and receipts without changing their resources."""
        ...


class _Expired(TimeoutError):
    pass


class _OperationExpired(TimeoutError):
    pass


class _Cancelled(KeyboardInterrupt):
    pass


def worker_identity() -> dict[str, Any]:
    """Bind this worker to the current boot and kernel process start counter."""
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
    return {
        "worker_id": str(uuid4()),
        "host_id": "management",
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": os.getpid(),
        "start_ticks": int(fields[19]),
        "heartbeat_at": utc_now(),
    }


def worker_alive(identity: dict[str, Any] | None) -> bool:
    """Check identity only; an expired heartbeat never authorizes replacement."""
    if not identity:
        return False
    try:
        return bool(
            stages.active(
                {
                    "boot_id": identity["boot_id"],
                    "processes": {str(identity["pid"]): identity["start_ticks"]},
                }
            )
        )
    except (OSError, KeyError, TypeError, ValueError):
        # Inability to inspect a recorded worker is not evidence that it died.
        raise OperationError(
            "recovery_required", "Worker identity could not be inspected"
        ) from None


@contextlib.contextmanager
def _bounded(seconds: float) -> Iterator[None]:
    """Interrupt trusted synchronous callbacks at their recorded action deadline."""
    if seconds <= 0:
        raise _Expired("Operation action deadline expired")
    if threading.current_thread() is not threading.main_thread():
        raise OperationError("invalid_input", "Operation workers require their main thread")
    previous = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(signum: int, frame: object) -> None:
        raise _Expired("Operation action deadline expired")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, *timer)


class StageContext:
    """Persist intentions and helper identities before allowing adapter effects."""

    def __init__(
        self,
        registry: ManagementRegistry,
        target_id: str,
        operation_id: str,
        *,
        fence: int | None,
        stage: dict[str, Any] | None = None,
        deadline: float | None = None,
        read_only: bool = False,
    ) -> None:
        self.registry = registry
        self.target_id = target_id
        self.operation_id = operation_id
        self.fence = fence
        self.stage = stage or {}
        self.stage_id = self.stage.get("stage_id")
        self.deadline = deadline if deadline is not None else time.monotonic() + 30
        self.hard_deadline = self.deadline
        self.operation_deadline = self.deadline
        self.read_only = read_only
        self.store = OperationStore(registry)
        self.access = InspectionAccess(registry)
        self.target = self.access.target(target_id)
        self.redactor = self.access.redactor(self.target)
        self.output_dir = self.target.artifact_root / f"operation-{operation_id}"
        self._lock = threading.RLock()
        self._last_heartbeat = 0.0

    def read(self) -> dict[str, Any]:
        """Read the latest committed operation, including cancellation requests."""
        return self.store.read(self.target_id, self.operation_id)

    def update(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Rebase worker updates on a concurrent committed cancellation request."""
        with self._lock:
            for attempt in range(5):
                record = self.read()
                revision = record["revision"]
                change(record)
                try:
                    return self.store.commit(
                        self.target_id,
                        self.operation_id,
                        record,
                        expected_revision=revision,
                        fence=self.fence,
                    )
                except OperationError as error:
                    if error.code != "stale_revision" or attempt == 4:
                        raise
        raise AssertionError("unreachable")

    def assert_current(self) -> None:
        """Check the current fence, cancellation and budget before an effect."""
        if self.read_only:
            raise OperationError("permission_denied", "Reconciliation cannot perform effects")
        record = self.read()
        if (
            record["state"] not in {"running", "cancelling"}
            or record["worker"]["fence"] != self.fence
        ):
            raise OperationError("recovery_required", "Operation worker ownership changed")
        if record["cancellation"]["requested_at"] is not None:
            raise _Cancelled()
        if time.monotonic() >= self.operation_deadline:
            raise _OperationExpired("Overall operation deadline expired")
        if time.monotonic() >= self.deadline:
            raise _Expired("Operation action deadline expired")

    def record_intent(self, receipt: dict[str, Any]) -> None:
        """Retain an unknown effect before an adapter attempts it."""
        self.assert_current()
        if receipt.get("effect") != "unknown":
            raise OperationError("invalid_input", "Intended effects must initially be unknown")
        self.record_effect(receipt)

    def record_effect(self, receipt: dict[str, Any]) -> None:
        """Commit a receipt after an effect, retaining the operation's cancellation."""
        if self.read_only:
            raise OperationError("permission_denied", "Reconciliation cannot write effect receipts")
        safe = self.redactor.value(receipt)
        safe["owner"] = dict(receipt["owner"])

        def change(record: dict[str, Any]) -> None:
            row = next(row for row in record["stages"] if row["stage_id"] == self.stage_id)
            row["effects"] = [
                item for item in row["effects"] if item["resource_id"] != safe["resource_id"]
            ] + [safe]

        self.update(change)

    def run_command(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
        retain_on_success: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a trusted fixed command with persisted, gated helper ownership."""
        self.assert_current()
        cleanup = self.stage["cleanup"]
        launch_token = str(uuid4())
        receipt: dict[str, Any] = {
            "resource_id": f"helper:{launch_token}",
            "kind": "measurement_helper",
            "host_id": "management",
            "owner": {
                "operation_id": self.operation_id,
                "stage_id": self.stage_id,
                "launch_token": launch_token,
            },
            "identity": None,
            "effect": "unknown",
            "observed_at": utc_now(),
        }
        self.record_intent(receipt)
        transferred: dict[int, int] = {}
        with directory(self.target.artifact_root, private=True, create=True):
            pass
        with directory(self.output_dir, private=True, create=True):
            pass

        def observed(evidence: dict[str, Any]) -> None:
            released = evidence.get("supervisor_returncode") == 0 and "cleanup" not in evidence
            receipt["identity"] = {
                "boot_id": evidence["boot_id"],
                "pid": evidence["pid"],
                "start_ticks": evidence["processes"].get(evidence["pid"], -1),
                "processes": {
                    str(pid): ticks
                    for pid, ticks in evidence["processes"].items()
                    if not released or transferred.get(int(pid)) != ticks
                },
            }
            receipt["effect"] = (
                "absent"
                if (
                    "cleanup" in evidence
                    and not evidence["cleanup"].get("surviving_processes")
                    and "error" not in evidence["cleanup"]
                )
                or (released and not stages.active(receipt["identity"]))
                else "unknown"
            )
            receipt["observed_at"] = utc_now()
            self.record_effect(receipt)
            if "cleanup" in evidence:
                self.deadline = min(
                    self.deadline
                    + min(
                        evidence["cleanup"].get("elapsed_seconds", 0),
                        (cleanup["term_grace_ms"] + cleanup["kill_grace_ms"]) / 1000,
                    ),
                    self.hard_deadline - cleanup["reconcile_ms"] / 1000,
                )

        def before_start(evidence: dict[str, Any]) -> None:
            observed(evidence)
            self.assert_current()

        def transfer_services(evidence: dict[str, Any]) -> None:
            self.assert_current()
            assert retain_on_success is not None
            effects = retain_on_success(evidence)
            live = stages.active(evidence)
            covered: dict[int, int] = {}
            resource_ids: set[str] = set()
            for effect in effects:
                identity = effect.get("identity") or {}
                owner = effect.get("owner") or {}
                if (
                    effect.get("effect") != "confirmed"
                    or effect.get("kind") not in self.stage.get("retain_on_success", [])
                    or effect.get("host_id") != "management"
                    or owner.get("operation_id") != self.operation_id
                    or owner.get("stage_id") != self.stage_id
                    or identity.get("boot_id") != evidence["boot_id"]
                    or effect.get("resource_id") in resource_ids
                ):
                    raise OperationError(
                        "ownership_conflict", "Retained service receipt is invalid"
                    )
                resource_ids.add(effect["resource_id"])
                processes = identity.get("processes", {})
                if not processes:
                    raise OperationError(
                        "ownership_conflict", "Retained service has no process identity"
                    )
                for pid_text, ticks in processes.items():
                    pid = int(pid_text)
                    if pid == evidence["pid"] or pid in covered or live.get(pid) != ticks:
                        raise OperationError(
                            "ownership_conflict", "Retained service identity changed"
                        )
                    covered[pid] = ticks
            if {pid: ticks for pid, ticks in live.items() if pid != evidence["pid"]} != covered:
                raise OperationError(
                    "ownership_conflict", "Helper has an unrecorded surviving process"
                )
            self.assert_current()
            safe_effects = [self.redactor.value(effect) for effect in effects]
            for safe, effect in zip(safe_effects, effects, strict=True):
                safe["owner"] = dict(effect["owner"])

            def transfer(record: dict[str, Any]) -> None:
                row = next(row for row in record["stages"] if row["stage_id"] == self.stage_id)
                row["effects"] = [
                    item for item in row["effects"] if item["resource_id"] not in resource_ids
                ] + safe_effects

            self.update(transfer)
            transferred.update(covered)

        def before_release(evidence: dict[str, Any]) -> None:
            with _bounded(min(self.deadline, self.operation_deadline) - time.monotonic()):
                transfer_services(evidence)

        def cancelled() -> bool:
            if time.monotonic() - self._last_heartbeat > 0.5:
                self.update(lambda record: record["worker"].update(heartbeat_at=utc_now()))
                self._last_heartbeat = time.monotonic()
            return self.read()["cancellation"]["requested_at"] is not None

        # stages.run owns action and cleanup timers independently. Its cleanup must
        # not be interrupted by the adapter action alarm after that action expires.
        timer = signal.getitimer(signal.ITIMER_REAL)
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            result = stages.run(
                command,
                stage=str(self.stage_id),
                log=self.output_dir / f"{launch_token}.log",
                cwd=cwd,
                env=env,
                pass_fds=pass_fds,
                retain_descendants=retain_on_success is not None,
                before_release=before_release if retain_on_success is not None else None,
                timeout=max(
                    0.001,
                    min(
                        self.deadline,
                        self.hard_deadline
                        - sum(
                            cleanup[name]
                            for name in ("term_grace_ms", "kill_grace_ms", "reconcile_ms")
                        )
                        / 1000,
                    )
                    - time.monotonic(),
                ),
                cleanup_grace=cleanup["term_grace_ms"] / 1000,
                kill_grace=cleanup["kill_grace_ms"] / 1000,
                before_start=before_start,
                observe=observed,
                cancelled=cancelled,
                redact=self.redactor.body,
            )
        finally:
            remaining = self.deadline - time.monotonic()
            if timer[0] and remaining > 0:
                signal.setitimer(signal.ITIMER_REAL, remaining)
        return subprocess.CompletedProcess(
            command,
            result.returncode,
            self.redactor.body(result.stdout.encode()).decode(),
            self.redactor.body(result.stderr.encode()).decode(),
        )


def _stage_record(stage_id: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "state": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "command_result": None,
        "artifacts": [],
        "effects": [],
        "reused_from": None,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _finish(
    context: StageContext,
    state: str,
    status: str,
    *,
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    record = context.read()
    errors = context.redactor.value(errors or [])
    store = ArtifactStore(str(context.registry.registry_id), context.target)
    reference = store.export(
        (
            json.dumps(
                public_value(
                    context.access,
                    context.target,
                    {
                        "operation_id": context.operation_id,
                        "status": status,
                        "data": data or {},
                        "stages": record["stages"],
                        "resources": record["resources"],
                        "errors": errors,
                    },
                ),
                ensure_ascii=False,
            )
            + "\n"
        ).encode(),
        "operation_summary",
    )
    result_data = (
        data
        if record["tool"] == "plan_prepare" and status == "success"
        else {
            "summary_artifact_id": reference["artifact_id"],
        }
    )

    def change(value: dict[str, Any]) -> None:
        if record["state"] == "queued" and value["state"] != "queued":
            raise OperationError("stale_worker", "Operation was assigned during cancellation")
        interrupted = (
            value["state"] == "cancelling" and value["cancellation"]["requested_at"] is not None
        )
        value.update(
            state="cancelled" if interrupted else state, finished_at=utc_now(), current_stage=None
        )
        value["artifacts"].append(reference)
        last = next(
            (
                row["command_result"]
                for row in reversed(value["stages"])
                if row["command_result"] is not None
            ),
            None,
        )
        value["result"] = {
            "status": "interrupted" if interrupted else status,
            "data": result_data,
            "errors": errors,
            "artifacts": value["artifacts"],
            "command_result": last,
        }

    return context.update(change)


def cancel_queued_operation(
    registry: ManagementRegistry,
    target_id: str,
    operation_id: str,
) -> dict[str, Any]:
    """Complete recorded cancellation before ownership, preserving a concurrent claim."""
    context = StageContext(registry, target_id, operation_id, fence=None, read_only=True)
    record = context.read()
    if record["state"] != "queued" or record["cancellation"]["requested_at"] is None:
        return record
    try:
        return _finish(
            context,
            "cancelled",
            "interrupted",
            errors=[
                _error("stage_cancelled", "Operation was cancelled before execution"),
            ],
        )
    except OperationError as error:
        if error.code != "stale_worker":
            raise
        return context.read()


def _recovery(context: StageContext, reason: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    def change(record: dict[str, Any]) -> None:
        record.update(state="recovery_required", result=None)
        record["recovery"].update(
            reason=reason,
            observed_at=utc_now(),
            errors=context.redactor.value(errors),
            required_actions=["Reconcile recorded effects before another execution"],
        )

    return context.update(change)


def _known_helpers_stopped(record: dict[str, Any]) -> bool:
    for row in record["stages"]:
        for receipt in row["effects"]:
            if receipt["kind"] != "measurement_helper":
                continue
            identity = receipt.get("identity")
            if identity is None:
                if receipt["effect"] != "absent":
                    return False
            elif stages.active(identity):
                return False
    return True


def _owned_local_helper(receipt: dict[str, Any], operation_id: str, stage_id: str) -> bool:
    """Recognize the ownership receipt written by StageContext.run_command."""
    try:
        token = uuid_string(receipt["owner"]["launch_token"])
        identity = receipt["identity"]
        if (
            receipt["kind"] != "measurement_helper"
            or receipt["host_id"] != "management"
            or receipt["resource_id"] != f"helper:{token}"
            or receipt["owner"]["operation_id"] != operation_id
            or receipt["owner"]["stage_id"] != stage_id
            or not isinstance(identity, dict)
        ):
            return False
        uuid_string(identity["boot_id"])
        pid, ticks = identity["pid"], identity["start_ticks"]
        processes = identity["processes"]
        return (
            type(pid) is int
            and pid > 1
            and type(ticks) is int
            and ticks > 0
            and isinstance(processes, dict)
            and processes.get(str(pid)) == ticks
            and all(
                isinstance(key, str)
                and key.isdecimal()
                and int(key) > 1
                and type(value) is int
                and value > 0
                for key, value in processes.items()
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def cancel_recovery_operation(
    registry: ManagementRegistry,
    target_id: str,
    operation_id: str,
) -> dict[str, Any]:
    """Apply an authorized cancellation to recorded local helpers after worker loss.

    The caller must authorize cancellation and persist its request first. Inspection
    never calls this function. Remote effects and unidentifiable helpers retain
    their reservations until an adapter establishes their outcomes.
    """
    context = StageContext(registry, target_id, operation_id, fence=None, read_only=True)
    # Read validates the UUID and target binding before constructing a lock name.
    record = context.read()
    if record["state"] in _TERMINAL | {"queued"} or not record["cancellation"]["requested_at"]:
        return record
    with _recovery_lock(context) as acquired:
        if not acquired:
            return context.read()
        return _cancel_recovery_locked(context)


@contextlib.contextmanager
def _recovery_lock(context: StageContext) -> Iterator[bool]:
    """Serialize explicit cleanup; process death releases ownership automatically."""
    try:
        with directory(context.registry.state_dir, private=True) as parent:
            name = f".cancel-{uuid_string(context.operation_id)}.lock"
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                info = os.fstat(descriptor)
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    raise OperationError(
                        "permission_denied", "Cleanup lock is unavailable or unsafe"
                    )
                os.fsync(parent)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    yield False
                else:
                    try:
                        yield True
                    finally:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    except (OSError, AccessError):
        raise OperationError("permission_denied", "Cleanup lock is unavailable or unsafe") from None


def _cancel_recovery_locked(context: StageContext) -> dict[str, Any]:
    record = context.read()
    if (
        record["state"] in _TERMINAL | {"queued"}
        or record["cancellation"]["requested_at"] is None
        or worker_alive(record["worker"])
    ):
        return record
    record = _recovery(context, "Cancellation is inspecting recorded local helpers", [])
    for row in record["stages"]:
        cleanup = row.get("cleanup_budgets")
        if cleanup is None or row["state"] in {"succeeded", "reused"}:
            continue
        grace = cleanup["term_grace_ms"] / 1000
        kill = cleanup["kill_grace_ms"] / 1000
        deadline = time.monotonic() + grace + kill
        for receipt in row["effects"]:
            if not _owned_local_helper(receipt, context.operation_id, row["stage_id"]):
                continue
            identity = receipt["identity"]
            owned = {int(pid): ticks for pid, ticks in identity["processes"].items()}
            attempt = {
                "stage_id": row["stage_id"],
                "resource_id": receipt["resource_id"],
                "requested_at": utc_now(),
                "term_grace_ms": cleanup["term_grace_ms"],
                "kill_grace_ms": cleanup["kill_grace_ms"],
                "finished_at": None,
            }

            # Commit the cleanup intention before sending a signal. Its process
            # identities remain in the receipt if this cancelling process dies.
            def intended(value: dict[str, Any], attempt: dict[str, Any] = attempt) -> None:
                value["recovery"].update(cleanup_attempt=attempt)

            context.update(intended)
            try:
                if stages.active(identity):
                    remaining = max(0.0, deadline - time.monotonic())
                    term_seconds = min(grace, max(0.0, remaining - kill))
                    result = stages._cleanup(
                        None,
                        owned,
                        term_seconds,
                        min(kill, remaining),
                        leader=identity["pid"],
                        hold_leader=True,
                    )
                else:
                    result = {"elapsed_seconds": 0.0, "surviving_processes": {}}
                updated = dict(receipt)
                updated["identity"] = {
                    **identity,
                    "processes": {str(pid): ticks for pid, ticks in owned.items()},
                }
                updated["observed_at"] = utc_now()
                updated["effect"] = "unknown" if result["surviving_processes"] else "absent"
            except OSError:
                return _recovery(
                    context,
                    "Local helper cleanup could not establish its outcome",
                    [_error("recovery_required", "Owned helper cleanup requires inspection")],
                )

            def observed(
                value: dict[str, Any],
                stage_id: str = row["stage_id"],
                updated: dict[str, Any] = updated,
                result: dict[str, Any] = result,
                attempt: dict[str, Any] = attempt,
            ) -> None:
                stage = next(stage for stage in value["stages"] if stage["stage_id"] == stage_id)
                stage["effects"] = [
                    updated if item["resource_id"] == updated["resource_id"] else item
                    for item in stage["effects"]
                ]
                value["recovery"]["cleanup_attempt"] = {
                    **attempt,
                    "finished_at": utc_now(),
                    "result": result,
                }

            context.update(observed)
    record = context.read()
    if any(
        item["effect"] == "unknown" for row in record["stages"] for item in row["effects"]
    ) or not _known_helpers_stopped(record):
        return _recovery(context, "Cancellation left effects requiring reconciliation", [])
    return _finish(context, "cancelled", "interrupted")


def run_operation(
    registry: ManagementRegistry,
    target_id: str,
    operation_id: str,
    *,
    adapter: ExecutionAdapter | None,
    plan: dict[str, Any] | None = None,
    prepare: Callable[[StageContext, dict[str, Any]], dict[str, Any]] | None = None,
    before_start: Callable[[StageContext], None] | None = None,
    before_stage: Callable[[StageContext], None] | None = None,
) -> dict[str, Any]:
    """Claim one accepted operation and execute its fixed stages in this worker."""
    store = OperationStore(registry)
    initial = store.read(target_id, operation_id)
    if initial["state"] != "queued":
        return initial
    context = StageContext(registry, target_id, operation_id, fence=None, read_only=True)
    if initial["cancellation"]["requested_at"] is not None:
        return _finish(
            context,
            "cancelled",
            "interrupted",
            errors=[_error("stage_cancelled", "Operation was cancelled before execution")],
        )
    if before_start is not None:
        try:
            with _bounded(30):
                before_start(context)
        except Exception as error:
            code = error.code if isinstance(error, OperationError) else "prerequisite_failed"
            status = (
                "failed_gate"
                if code
                in {
                    "stale_plan",
                    "adapter_unavailable",
                    "plan_evidence_missing",
                    "plan_evidence_changed",
                }
                else "invalid_input"
                if code in {"permission_denied", "invalid_input", "prerequisite_failed"}
                else "error"
            )
            return _finish(
                context,
                "failed",
                status,
                errors=[
                    _error(
                        code,
                        "Operation preconditions failed before execution",
                    )
                ],
            )
    try:
        record = store.claim(target_id, operation_id, worker_identity())
    except OperationError as error:
        if error.code != "stale_worker":
            raise
        return store.read(target_id, operation_id)
    context.fence = record["worker"]["fence"]
    context.read_only = False
    if record["tool"] == "plan_prepare":
        budgets = record["preparation_budgets"]
        selected = [
            {
                "stage_id": "discovery",
                "timeout_ms": budgets["timeout_ms"],
                "cleanup": {
                    "policy": "temporary_only",
                    **{
                        key: budgets[key]
                        for key in ("term_grace_ms", "kill_grace_ms", "reconcile_ms")
                    },
                },
            }
        ]
    elif plan is not None:
        selected = plan["payload"]["stages"]
    else:
        return _finish(
            context,
            "failed",
            "invalid_input",
            errors=[_error("plan_missing", "Operation plan is unavailable")],
        )
    operation_deadline = time.monotonic() + max(
        0.0,
        (
            datetime.fromisoformat(record["deadline_at"].replace("Z", "+00:00"))
            - datetime.now().astimezone()
        ).total_seconds(),
    )
    context.operation_deadline = operation_deadline
    data: dict[str, Any] = {}
    try:
        with stages.cancellation():
            for index, stage in enumerate(selected):
                context.stage = stage
                context.stage_id = stage["stage_id"]
                cleanup_seconds = (
                    sum(
                        stage["cleanup"][name]
                        for name in ("term_grace_ms", "kill_grace_ms", "reconcile_ms")
                    )
                    / 1000
                )
                action_seconds = stage["timeout_ms"] / 1000
                remaining = operation_deadline - time.monotonic()
                if remaining <= cleanup_seconds or (
                    index and action_seconds + cleanup_seconds > remaining
                ):
                    raise _OperationExpired("Operation cannot fit the next stage and cleanup")
                context.deadline = min(
                    time.monotonic() + action_seconds, operation_deadline - cleanup_seconds
                )
                context.hard_deadline = min(
                    time.monotonic() + action_seconds + cleanup_seconds,
                    operation_deadline,
                )

                def started(value: dict[str, Any], stage: dict[str, Any] = stage) -> None:
                    value["current_stage"] = stage["stage_id"]
                    row = next(
                        row for row in value["stages"] if row["stage_id"] == stage["stage_id"]
                    )
                    row.update(_stage_record(stage["stage_id"]))
                    row["cleanup_budgets"] = stage["cleanup"]

                context.update(started)
                with _bounded(context.deadline - time.monotonic()):
                    context.assert_current()
                    if before_stage is not None:
                        before_stage(context)
                    context.assert_current()
                    if record["tool"] == "plan_prepare":
                        if prepare is None:
                            raise OperationError(
                                "unsupported_action", "Plan preparation adapter is unavailable"
                            )
                        outcome = StageOutcome(data=prepare(context, context.read()))
                    else:
                        assert plan is not None
                        if adapter is None:
                            raise OperationError(
                                "adapter_unavailable", "Execution adapter is unavailable"
                            )
                        outcome = adapter.execute_stage(context, stage, plan)
                if outcome.status not in {
                    "success",
                    "degraded",
                    "failed_gate",
                    "invalid_input",
                    "error",
                    "interrupted",
                }:
                    raise OperationError("invalid_input", "Adapter returned an invalid outcome")
                for receipt in outcome.effects:
                    context.record_effect(receipt)
                data = context.redactor.value(outcome.data)

                def completed(value: dict[str, Any], outcome: StageOutcome = outcome) -> None:
                    if outcome.status == "interrupted" and value["state"] == "running":
                        value["state"] = "cancelling"
                        value["cancellation"] = {
                            "requested_at": utc_now(),
                            "reason": "Installed command reported interruption",
                        }
                    row = next(
                        row for row in value["stages"] if row["stage_id"] == context.stage_id
                    )
                    row.update(
                        artifacts=outcome.artifacts,
                        command_result=clean_command(outcome.command_result, context.redactor)
                        if outcome.command_result
                        else None,
                    )
                    value["artifacts"].extend(outcome.artifacts)

                current = context.update(completed)
                if any(
                    item["effect"] == "unknown"
                    for row in current["stages"]
                    for item in row["effects"]
                ) or not _known_helpers_stopped(current):
                    return _recovery(
                        context, "Stage effects or helpers require reconciliation", outcome.errors
                    )

                def settled(value: dict[str, Any], outcome: StageOutcome = outcome) -> None:
                    row = next(
                        row for row in value["stages"] if row["stage_id"] == context.stage_id
                    )
                    row.update(
                        state="cancelled"
                        if outcome.status == "interrupted"
                        else "succeeded"
                        if outcome.status == "success"
                        else "failed",
                        finished_at=utc_now(),
                    )

                current = context.update(settled)
                if current["cancellation"]["requested_at"] is not None:
                    return _finish(
                        context, "cancelled", "interrupted", data=data, errors=outcome.errors
                    )
                context.assert_current()
                if outcome.status != "success":
                    return _finish(
                        context,
                        "failed",
                        "failed_gate" if outcome.status == "degraded" else outcome.status,
                        data=data,
                        errors=outcome.errors,
                    )
        current = context.read()
        if any(
            item["effect"] == "unknown" for row in current["stages"] for item in row["effects"]
        ) or not _known_helpers_stopped(current):
            return _recovery(
                context,
                "Successful action left effects or helpers unresolved",
                [
                    _error("recovery_required", "Action effects require reconciliation"),
                ],
            )
        return _finish(context, "succeeded", "success", data=data)
    except KeyboardInterrupt:
        context.update(
            lambda value: value.update(
                state="cancelling",
                cancellation=value["cancellation"]
                if value["cancellation"]["requested_at"] is not None
                else {
                    "requested_at": value["cancellation"]["requested_at"] or utc_now(),
                    "reason": "Operation interrupted",
                },
            )
        )
        code, message, cancelled = "stage_cancelled", "Operation execution was interrupted", True
    except _OperationExpired:
        code, message, cancelled = "operation_timeout", "Overall operation budget expired", False
    except stages.StageOutputLimit:
        code, message, cancelled = (
            "source_truncated",
            "Helper output exceeded its byte limit",
            False,
        )
    except (TimeoutError, stages.StageTimeout):
        code, message, cancelled = "stage_timeout", "Operation action deadline expired", False
    except Exception as error:
        code = error.code if isinstance(error, OperationError) else "adapter_failed"
        message = error.message if isinstance(error, OperationError) else "Operation adapter failed"
        cancelled = False
    errors = [_error(code, message)]

    def failed(value: dict[str, Any]) -> None:
        row = next((row for row in value["stages"] if row["stage_id"] == context.stage_id), None)
        if row is not None and row["state"] == "running":
            row.update(state="cancelled" if cancelled else "failed", finished_at=utc_now())

    context.update(failed)
    current = context.read()
    if any(
        item["effect"] == "unknown" for row in current["stages"] for item in row["effects"]
    ) or not _known_helpers_stopped(current):
        return _recovery(context, message, errors)
    return _finish(
        context,
        "cancelled" if cancelled else "failed",
        "interrupted" if cancelled else "error",
        errors=errors,
    )


def reconcile_operation(
    registry: ManagementRegistry,
    target_id: str,
    operation_id: str,
    *,
    adapter: ExecutionAdapter | None = None,
    timeout_s: float = 30,
) -> dict[str, Any]:
    """Inspect a lost worker and effects without restarting commands or cleanup."""
    context = StageContext(registry, target_id, operation_id, fence=None, read_only=True)
    record = context.read()
    if record["state"] in _TERMINAL | {"queued"} or worker_alive(record["worker"]):
        return record
    with _recovery_lock(context) as acquired:
        if not acquired:
            return context.read()
        return _reconcile_operation(
            registry, target_id, operation_id, adapter=adapter, timeout_s=timeout_s
        )


def _reconcile_operation(
    registry: ManagementRegistry,
    target_id: str,
    operation_id: str,
    *,
    adapter: ExecutionAdapter | None,
    timeout_s: float,
) -> dict[str, Any]:
    """Reconcile while excluding another reconciliation or explicit cleanup."""
    context = StageContext(
        registry,
        target_id,
        operation_id,
        fence=None,
        read_only=True,
        deadline=time.monotonic() + timeout_s,
    )
    record = context.read()
    if record["state"] in _TERMINAL or record["state"] == "queued":
        return record
    if worker_alive(record["worker"]):
        return record
    record = _recovery(context, "Worker identity is no longer active", [])
    if adapter is None:
        return record
    cleanup = next(
        (
            row.get("cleanup_budgets")
            for row in reversed(record["stages"])
            if row.get("cleanup_budgets")
        ),
        None,
    )
    if cleanup is None:
        cleanup = record["preparation_budgets"]
    if cleanup is not None:
        timeout_s = min(timeout_s, cleanup["reconcile_ms"] / 1000)
    context.deadline = time.monotonic() + timeout_s
    try:
        with _bounded(timeout_s):
            outcome = adapter.reconcile(context, record)
    except Exception:
        return _recovery(
            context,
            "Reconciliation did not establish effects",
            [_error("recovery_required", "Reconciliation could not complete")],
        )
    observed = {row["resource_id"]: row for row in outcome.effects}

    def findings(value: dict[str, Any]) -> None:
        for stage in value["stages"]:
            if stage["state"] in {"succeeded", "reused"}:
                continue
            for index, row in enumerate(stage["effects"]):
                receipt = observed.get(row["resource_id"], row)
                safe = context.redactor.value(receipt)
                safe["owner"] = dict(receipt["owner"])
                stage["effects"][index] = safe
        value["recovery"].update(
            observed_at=utc_now(),
            errors=context.redactor.value(outcome.errors),
            artifacts=outcome.artifacts,
        )
        value["artifacts"].extend(outcome.artifacts)

    record = context.update(findings)
    if (
        not outcome.helpers_stopped
        or not _known_helpers_stopped(record)
        or any(item["effect"] == "unknown" for row in record["stages"] for item in row["effects"])
    ):
        return record
    if record["cancellation"]["requested_at"] is not None:
        return _finish(context, "cancelled", "interrupted", errors=outcome.errors)
    if (
        record["tool"] != "plan_prepare"
        and outcome.complete
        and all(row["state"] in {"succeeded", "reused"} for row in record["stages"])
    ):
        return _finish(context, "succeeded", "success", errors=outcome.errors)
    return _finish(
        context,
        "failed",
        "error",
        errors=outcome.errors
        or [_error("operation_interrupted", "Worker stopped before required work completed")],
    )
