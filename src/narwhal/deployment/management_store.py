"""Commit operation admission, ownership and recovery records in one private store."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

from narwhal.contracts import ContractVersionError

from .management_access import AccessError, directory
from .management_records import (
    ACTION_CAPABILITIES,
    MAX_RECORD_BYTES,
    MAX_RESOURCES,
    MAX_STAGES,
    TERMINAL,
    TRANSITIONS,
    OperationError,
    budgets,
    canonical,
    encode_record,
    new_operation,
    require,
    summary,
    utc_now,
    uuid_string,
    validate_record,
)
from .management_registry import ManagementRegistry, ManagementTarget

_DATABASE = "operations.sqlite3"
_LOCK = "operations.lock"
MAX_PAGE_BYTES = 220_000
MAX_CLEANUP_PREDECESSORS = 16
_P = ParamSpec("_P")
_R = TypeVar("_R")
_IMMUTABLE = {
    "schema",
    "schema_version",
    "operation_id",
    "target_id",
    "request_id",
    "tool",
    "action",
    "parent_operation_id",
    "plan_id",
    "plan_digest",
    "created_at",
    "preparation_budgets",
    "target_snapshot",
}


def _checked(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def checked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            raise OperationError("invalid_input", "Operation input or record is invalid") from None

    return checked


def _private(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise OperationError(
            "permission_denied", "Operation storage must be a private regular file"
        )


def _open(parent: int, name: str, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(name, flags | (os.O_CREAT if create else 0), 0o600, dir_fd=parent)
    try:
        _private(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _sidecars(parent: int) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            descriptor = _open(parent, _DATABASE + suffix)
        except FileNotFoundError:
            continue
        os.close(descriptor)


def _decode(payload: bytes | str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise OperationError("invalid_input", "Persisted operation data is invalid") from None
    require(isinstance(value, dict), "Persisted operation data is invalid")
    return value


def _plan_budget(plan: dict[str, Any]) -> int:
    payload = plan.get("payload")
    if not isinstance(payload, dict):
        raise OperationError("invalid_input", "Plan payload is invalid")
    stages = payload.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise OperationError("invalid_input", "Plan stages are invalid")
    known = set()
    total = 0
    inputs = payload.get("binding", {}).get("inputs", [])
    require(isinstance(inputs, list))
    input_names = {entry["name"] for entry in inputs}
    for stage in stages:
        require(isinstance(stage, dict))
        name = stage.get("stage_id")
        require(isinstance(name, str) and name not in known)
        dependencies = stage.get("depends_on")
        require(
            isinstance(dependencies, list)
            and all(dependency in known for dependency in dependencies)
        )
        selected = stage.get("input_names")
        require(isinstance(selected, list) and all(item in input_names for item in selected))
        cleanup = stage.get("cleanup")
        require(isinstance(cleanup, dict))
        total += budgets({"timeout_ms": stage.get("timeout_ms"), **cleanup})
        known.add(name)
    return total


class OperationStore:
    """Use one registry authority for transactions shared by workers, MCP and the CLI."""

    def __init__(self, registry: ManagementRegistry):
        self.registry = registry
        self.registry_id = str(registry.registry_id)
        self._targets = {target.id: target for target in registry.targets}

    def _target(self, target_id: str, *, inspect: bool = True) -> ManagementTarget:
        target = self._targets.get(target_id)
        if target is None:
            raise OperationError("target_not_found", "Target is not registered")
        if inspect and "inspect" not in target.capabilities:
            raise OperationError("permission_denied", "Target does not permit inspection")
        return target

    def _authorize(self, target_id: str, action: str) -> ManagementTarget:
        target = self._target(target_id)
        require(action in ACTION_CAPABILITIES, "Operation action is unsupported")
        if action not in target.actions or not ACTION_CAPABILITIES[action] <= set(
            target.capabilities
        ):
            raise OperationError("permission_denied", "Target does not permit this action")
        return target

    def check_registry(self, new_registry: ManagementRegistry) -> None:
        """Reject authority changes or removal of a target with unfinished persisted work."""
        if (
            str(new_registry.registry_id) != self.registry_id
            or new_registry.state_dir != self.registry.state_dir
        ):
            raise OperationError("permission_denied", "Operation store authority cannot change")
        permitted = {target.id for target in new_registry.targets}
        with self.connection() as connection:
            active = connection.execute(
                "SELECT DISTINCT target_id FROM operations "
                "WHERE state NOT IN ('succeeded','failed','cancelled')"
            )
            if any(target_id not in permitted for (target_id,) in active):
                raise OperationError(
                    "resource_busy", "Registry cannot remove targets with unfinished operations"
                )

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open one serialized FULL-durability transaction without following storage links."""
        try:
            with directory(self.registry.state_dir, private=True, create=True) as parent:
                lock = _open(parent, _LOCK, create=True)
                try:
                    deadline = time.monotonic() + 1
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise OperationError(
                                    "resource_busy", "Operation store is busy"
                                ) from None
                            time.sleep(0.005)
                    _sidecars(parent)
                    descriptor = _open(parent, _DATABASE, create=True)
                    try:
                        with contextlib.closing(
                            sqlite3.connect(
                                f"file:/proc/self/fd/{descriptor}?mode=rw",
                                uri=True,
                                isolation_level=None,
                                timeout=1,
                            )
                        ) as connection:
                            # The descriptor pins the verified database; SQLite resolves its path
                            # to place rollback journals beside it in the private directory.
                            info = os.stat(_DATABASE, dir_fd=parent, follow_symlinks=False)
                            opened = os.fstat(descriptor)
                            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                                raise OperationError(
                                    "permission_denied", "Operation database changed while opening"
                                )
                            connection.execute("PRAGMA trusted_schema=OFF")
                            connection.execute("PRAGMA journal_mode=DELETE")
                            connection.execute("PRAGMA synchronous=FULL")
                            connection.execute("PRAGMA foreign_keys=ON")
                            connection.execute("BEGIN IMMEDIATE")
                            try:
                                self._initialize(connection)
                                yield connection
                                _sidecars(parent)
                                connection.commit()
                                os.fsync(parent)
                            except BaseException:
                                connection.rollback()
                                raise
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(lock)
        except AccessError as error:
            raise OperationError(error.code, error.message) from None
        except OSError:
            raise OperationError(
                "permission_denied", "Operation storage is unavailable or unsafe"
            ) from None
        except sqlite3.Error:
            raise OperationError(
                "source_unavailable", "Operation database could not be read or committed"
            ) from None

    def _initialize(self, connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS authority (singleton INTEGER PRIMARY KEY "
            "CHECK(singleton=1), registry_id TEXT NOT NULL, version INTEGER NOT NULL, fence "
            "INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, target_id "
            "TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER "
            "NOT NULL, document BLOB NOT NULL, request BLOB NOT NULL, plan BLOB)",
            "CREATE TABLE IF NOT EXISTS requests (target_id TEXT NOT NULL, request_id TEXT "
            "NOT NULL, digest TEXT NOT NULL, canonical BLOB NOT NULL, operation_id TEXT NOT "
            "NULL, PRIMARY KEY(target_id, request_id))",
            "CREATE TABLE IF NOT EXISTS consumed_plans (plan_id TEXT PRIMARY KEY, target_id "
            "TEXT NOT NULL, tool TEXT NOT NULL, parent_id TEXT, operation_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS reservations (resource_id TEXT PRIMARY KEY, "
            "operation_id TEXT NOT NULL, fence INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tombstones (operation_id TEXT PRIMARY KEY, target_id "
            "TEXT NOT NULL, removed_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pages (cursor TEXT PRIMARY KEY, target_id TEXT NOT "
            "NULL, binding TEXT NOT NULL, page_limit INTEGER NOT NULL, document BLOB NOT NULL)",
        )
        for statement in statements:
            connection.execute(statement)
        row = connection.execute(
            "SELECT registry_id,version FROM authority WHERE singleton=1"
        ).fetchone()
        if row is None:
            connection.execute("INSERT INTO authority VALUES (1, ?, 1, 0)", (self.registry_id,))
        else:
            if row[0] != self.registry_id:
                raise OperationError(
                    "permission_denied", "Operation store belongs to another registry"
                )
            if row[1] != 1:
                raise ContractVersionError("Operation store version is unsupported")

    @staticmethod
    def _fence(connection: sqlite3.Connection) -> int:
        connection.execute("UPDATE authority SET fence=fence+1 WHERE singleton=1")
        return int(
            connection.execute("SELECT fence FROM authority WHERE singleton=1").fetchone()[0]
        )

    @staticmethod
    def _read(connection: sqlite3.Connection, target_id: str, operation_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT target_id,document FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None or row[0] != target_id:
            removed = connection.execute(
                "SELECT 1 FROM tombstones WHERE target_id=? AND operation_id=?",
                (target_id, operation_id),
            ).fetchone()
            if removed:
                raise OperationError("operation_record_removed", "Operation record was removed")
            raise OperationError("object_not_found", "Operation does not belong to this target")
        require(len(row[1]) <= MAX_RECORD_BYTES, "Persisted operation exceeds its byte limit")
        record = _decode(row[1])
        validate_record(record)
        require(record["target_id"] == target_id and record["operation_id"] == operation_id)
        return record

    @staticmethod
    def _write(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        validate_record(record)
        connection.execute(
            "UPDATE operations SET document=?,state=?,revision=? WHERE operation_id=?",
            (encode_record(record), record["state"], record["revision"], record["operation_id"]),
        )
        if record["state"] in TERMINAL:
            connection.execute(
                "DELETE FROM reservations WHERE operation_id=?", (record["operation_id"],)
            )

    def _cleanup_original(
        self,
        connection: sqlite3.Connection,
        target_id: str,
        operation_id: str,
        resources: Sequence[str],
    ) -> None:
        """Require a stopped execution and its exact reservations before transfer."""
        from .management_executor import worker_alive

        original = self._read(connection, target_id, operation_id)
        if original["state"] not in TERMINAL | {"recovery_required"}:
            raise OperationError("resource_busy", "Cleanup cannot replace an active operation")
        self._cleanup_chain(connection, target_id, original, resources)
        if original["state"] == "recovery_required" and worker_alive(original["worker"]):
            raise OperationError("resource_busy", "The original operation worker is still alive")

    def _cleanup_chain(
        self,
        connection: sqlite3.Connection,
        target_id: str,
        original: dict[str, Any],
        resources: Sequence[str],
    ) -> None:
        """Follow immutable cleanup selections to one bounded fleet execution lineage."""
        seen: set[str] = set()
        current = original
        for _ in range(MAX_CLEANUP_PREDECESSORS):
            operation_id = current["operation_id"]
            require(operation_id not in seen, "Cleanup predecessor chain contains a cycle")
            seen.add(operation_id)
            require(
                current["tool"] != "plan_prepare"
                and current["action"]
                in {
                    "fleet_deploy",
                    "fleet_profile",
                    "fleet_preflight",
                    "engine_replace",
                    "monitoring_start",
                    "deployment_cleanup",
                },
                "Cleanup requires a fleet execution operation",
            )
            require(
                bool(resources)
                and sorted(resources) == sorted(row["resource_id"] for row in current["resources"]),
                "Cleanup must reserve every predecessor's exact resources",
            )
            row = connection.execute(
                "SELECT request,plan FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            request, plan = _decode(row[0]), _decode(row[1])
            require(
                all(
                    request[name] == current[name]
                    for name in ("tool", "action", "plan_id", "plan_digest", "parent_operation_id")
                )
                and plan["plan_id"] == current["plan_id"]
                and plan["plan_digest"] == current["plan_digest"]
                and hashlib.sha256(canonical(plan["payload"])).hexdigest() == current["plan_digest"]
                and plan["payload"]["target_id"] == target_id
                and plan["payload"]["action"] == current["action"]
                and plan["payload"]["parameters"] == request["parameters"],
                "Cleanup predecessor admission identity is inconsistent",
            )
            if current["action"] != "deployment_cleanup":
                return
            parameters = request["parameters"]
            predecessor = uuid_string(parameters["operation_id"])
            require(
                parameters == {"operation_id": predecessor},
                "Cleanup predecessor selection is not canonical",
            )
            if len(seen) == MAX_CLEANUP_PREDECESSORS:
                break
            current = self._read(connection, target_id, predecessor)
        raise OperationError("invalid_input", "Cleanup predecessor chain exceeds its limit")

    @staticmethod
    def _cleanup_replay(record: dict[str, Any], resources: Sequence[str]) -> None:
        require(
            sorted(resources) == sorted(row["resource_id"] for row in record["resources"]),
            "Cleanup replay resources differ from the accepted operation",
        )

    @_checked
    def admit(
        self,
        *,
        target_id: str,
        request_id: str,
        tool: str,
        action: str,
        parameters: dict[str, Any],
        plan: dict[str, Any] | None = None,
        resources: Sequence[str] = (),
        parent_operation_id: str | None = None,
        preparation_budgets: dict[str, Any] | None = None,
        cleanup_of: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Commit deduplication, plan consumption and exclusions before acceptance."""
        target = self._authorize(target_id, action)
        request_id = uuid_string(request_id)
        require(tool in {"plan_prepare", "plan_execute", "operation_resume"})
        require(isinstance(parameters, dict))
        require(not isinstance(resources, str) and len(resources) <= MAX_RESOURCES)
        require(
            all(isinstance(resource, str) and 0 < len(resource) <= 1024 for resource in resources)
        )
        require(len(resources) == len(set(resources)))
        if parent_operation_id is not None:
            parent_operation_id = uuid_string(parent_operation_id)
        if action == "deployment_cleanup" and tool != "plan_prepare":
            cleanup_of = uuid_string(cleanup_of)
            require(
                parameters == {"operation_id": cleanup_of},
                "Cleanup selection must match its canonical operation parameter",
            )
        else:
            require(cleanup_of is None, "Only cleanup execution may transfer reservations")
        if tool == "plan_prepare":
            require(plan is None and parent_operation_id is None and not resources)
            preparation_budgets = preparation_budgets or target.preparation.model_dump(mode="json")
            budgets(preparation_budgets)
        else:
            if not isinstance(plan, dict):
                raise OperationError("invalid_input", "Execution requires a plan")
            require(preparation_budgets is None)
            require((parent_operation_id is not None) == (tool == "operation_resume"))
            if (
                plan["schema"] != "narwhal.deployment-plan"
                or type(plan["schema_version"]) is not int
                or plan["schema_version"] != 1
            ):
                raise ContractVersionError("Deployment plan version is unsupported")
            uuid_string(plan["plan_id"])
            require(
                hashlib.sha256(canonical(plan["payload"])).hexdigest() == plan["plan_digest"],
                "Plan digest does not match its payload",
            )
            require(
                plan["payload"]["target_id"] == target_id
                and plan["payload"]["action"] == action
                and plan["payload"]["parameters"] == parameters,
                "Plan scope differs from the request",
            )
            _plan_budget(plan)
        request = {
            "tool": tool,
            "action": action,
            "parameters": parameters,
            "plan_id": plan["plan_id"] if plan else None,
            "plan_digest": plan["plan_digest"] if plan else None,
            "parent_operation_id": parent_operation_id,
        }
        encoded = canonical(request)
        digest = hashlib.sha256(encoded).hexdigest()
        with self.connection() as connection:
            found = connection.execute(
                "SELECT digest,canonical,operation_id FROM requests WHERE target_id=? AND "
                "request_id=?",
                (target_id, request_id),
            ).fetchone()
            if found:
                if found[0] != digest or found[1] != encoded:
                    raise OperationError(
                        "request_id_conflict", "Request ID already identifies different work"
                    )
                record = self._read(connection, target_id, found[2])
                if cleanup_of is not None:
                    self._cleanup_replay(record, resources)
                return record, False
            if plan is not None:
                used = connection.execute(
                    "SELECT target_id,tool,parent_id,operation_id FROM consumed_plans WHERE "
                    "plan_id=?",
                    (plan["plan_id"],),
                ).fetchone()
                if used:
                    if used[:3] != (target_id, tool, parent_operation_id):
                        raise OperationError(
                            "plan_scope_mismatch", "Plan was consumed by another execution scope"
                        )
                    record = self._read(connection, target_id, used[3])
                    if record["plan_digest"] != plan["plan_digest"]:
                        raise OperationError(
                            "plan_scope_mismatch", "Consumed plan identity cannot change"
                        )
                    if cleanup_of is not None:
                        self._cleanup_replay(record, resources)
                    connection.execute(
                        "INSERT INTO requests VALUES (?, ?, ?, ?, ?)",
                        (target_id, request_id, digest, encoded, record["operation_id"]),
                    )
                    return record, False
            if parent_operation_id is not None:
                parent = self._read(connection, target_id, parent_operation_id)
                if parent["state"] == "recovery_required":
                    raise OperationError(
                        "recovery_required", "Operation effects still require reconciliation"
                    )
                if (
                    parent["tool"] == "plan_prepare"
                    or parent["state"] not in {"failed", "cancelled"}
                    or parent["action"] != action
                ):
                    raise OperationError(
                        "operation_not_resumable", "Operation cannot be resumed with this action"
                    )
            if cleanup_of is not None:
                self._cleanup_original(connection, target_id, cleanup_of, resources)
            for resource in sorted(resources):
                reserved = connection.execute(
                    "SELECT operation_id FROM reservations WHERE resource_id=?", (resource,)
                ).fetchone()
                if reserved is not None and reserved[0] != cleanup_of:
                    raise OperationError(
                        "resource_busy", "A required resource is reserved by another operation"
                    )
            record = new_operation(
                target_id=target_id,
                request_id=request_id,
                tool=tool,
                action=action,
                plan=plan,
                parent_operation_id=parent_operation_id,
                preparation_budgets=preparation_budgets,
            )
            record["target_snapshot"] = target.model_dump(mode="json")
            fence = self._fence(connection)
            record["resources"] = [
                {
                    "resource_id": resource,
                    "mode": "exclusive",
                    "operation_id": record["operation_id"],
                    "fence": fence,
                    "acquired_at": record["created_at"],
                }
                for resource in sorted(resources)
            ]
            validate_record(record)
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["operation_id"],
                    target_id,
                    record["created_at"],
                    record["state"],
                    record["revision"],
                    encode_record(record),
                    encoded,
                    canonical(plan) if plan else None,
                ),
            )
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?)",
                (target_id, request_id, digest, encoded, record["operation_id"]),
            )
            if plan:
                connection.execute(
                    "INSERT INTO consumed_plans VALUES (?, ?, ?, ?, ?)",
                    (plan["plan_id"], target_id, tool, parent_operation_id, record["operation_id"]),
                )
            if cleanup_of is not None:
                connection.execute("DELETE FROM reservations WHERE operation_id=?", (cleanup_of,))
            connection.executemany(
                "INSERT INTO reservations VALUES (?, ?, ?)",
                [(resource, record["operation_id"], fence) for resource in sorted(resources)],
            )
            return record, True

    def read(self, target_id: str, operation_id: str) -> dict[str, Any]:
        """Read a committed operation without reconciling or changing its execution."""
        self._target(target_id)
        operation_id = uuid_string(operation_id)
        with self.connection() as connection:
            return self._read(connection, target_id, operation_id)

    def _admission(self, target_id: str, operation_id: str, field: str) -> dict[str, Any] | None:
        self._target(target_id)
        operation_id = uuid_string(operation_id)
        with self.connection() as connection:
            self._read(connection, target_id, operation_id)
            row = connection.execute(
                "SELECT request,plan FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            value = row[0 if field == "request" else 1]
            return _decode(value) if value is not None else None

    def request(self, target_id: str, operation_id: str) -> dict[str, Any]:
        """Return the admitted canonical request for a restarted worker."""
        value = self._admission(target_id, operation_id, "request")
        assert value is not None
        return value

    def lookup_request(self, target_id: str, request_id: str) -> dict[str, Any] | None:
        """Find a prior request before checking inputs that may have changed since admission."""
        self._target(target_id)
        request_id = uuid_string(request_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT canonical,operation_id FROM requests WHERE target_id=? AND request_id=?",
                (target_id, request_id),
            ).fetchone()
            if row is None:
                return None
            self._authorize(target_id, _decode(row[0])["action"])
            self._read(connection, target_id, row[1])
            return {**_decode(row[0]), "operation_id": row[1]}

    def lookup_plan(self, target_id: str, plan_id: str) -> dict[str, Any] | None:
        """Find a plan's existing execution without rereading its original input files."""
        self._target(target_id)
        plan_id = uuid_string(plan_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT consumed_plans.operation_id,requests.canonical FROM consumed_plans "
                "JOIN requests ON requests.operation_id=consumed_plans.operation_id "
                "AND requests.target_id=consumed_plans.target_id "
                "WHERE consumed_plans.target_id=? AND plan_id=? LIMIT 1",
                (target_id, plan_id),
            ).fetchone()
            if row is None:
                return None
            request = _decode(row[1])
            self._authorize(target_id, request["action"])
            self._read(connection, target_id, row[0])
            return {**request, "operation_id": row[0]}

    def plan(self, target_id: str, operation_id: str) -> dict[str, Any] | None:
        """Return the immutable plan captured at admission, or null for preparation."""
        return self._admission(target_id, operation_id, "plan")

    def request_cancel(
        self, target_id: str, operation_id: str, reason: str = "requested"
    ) -> dict[str, Any]:
        """Record cancellation without performing cleanup or inventing its terminal evidence."""
        self._target(target_id)
        operation_id = uuid_string(operation_id)
        require(isinstance(reason, str) and len(reason) <= 1024)
        with self.connection() as connection:
            record = self._read(connection, target_id, operation_id)
            if record["tool"] != "plan_prepare":
                self._authorize(target_id, record["action"])
            if record["state"] in TERMINAL or record["cancellation"]["requested_at"] is not None:
                return record
            record["cancellation"] = {"requested_at": utc_now(), "reason": reason}
            if record["state"] == "running":
                record["state"] = "cancelling"
            record["revision"] += 1
            record["updated_at"] = utc_now()
            self._write(connection, record)
            return record

    @_checked
    def claim(self, target_id: str, operation_id: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Assign a queued operation to one worker and start its recorded deadline."""
        self._target(target_id)
        operation_id = uuid_string(operation_id)
        with self.connection() as connection:
            record = self._read(connection, target_id, operation_id)
            self._authorize(target_id, record["action"])
            if record["state"] != "queued":
                raise OperationError("stale_worker", "Operation is already assigned or terminal")
            worker = deepcopy(identity)
            worker["fence"] = self._fence(connection)
            worker["heartbeat_at"] = utc_now()
            record["worker"] = worker
            record["state"] = "running"
            record["started_at"] = utc_now()
            if record["tool"] == "plan_prepare":
                duration = budgets(record["preparation_budgets"])
                record["current_stage"] = "discovery"
            else:
                raw = connection.execute(
                    "SELECT plan FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
                duration = _plan_budget(_decode(raw))
            record["deadline_at"] = (
                (datetime.fromisoformat(record["started_at"]) + timedelta(milliseconds=duration))
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            for resource in record["resources"]:
                resource["fence"] = worker["fence"]
            connection.execute(
                "UPDATE reservations SET fence=? WHERE operation_id=?",
                (worker["fence"], operation_id),
            )
            record["revision"] += 1
            record["updated_at"] = utc_now()
            self._write(connection, record)
            return record

    @_checked
    def commit(
        self,
        target_id: str,
        operation_id: str,
        record: dict[str, Any],
        expected_revision: int,
        fence: int | None,
    ) -> dict[str, Any]:
        """Commit a checked transition, preserving admission identity and ownership fencing."""
        self._target(target_id, inspect=False)
        operation_id = uuid_string(operation_id)
        require(type(expected_revision) is int and expected_revision > 0)
        with self.connection() as connection:
            current = self._read(connection, target_id, operation_id)
            if current["revision"] != expected_revision:
                raise OperationError(
                    "stale_revision", "Operation changed; read its current revision"
                )
            worker = current["worker"]
            if fence is not None and (
                type(fence) is not int or worker is None or worker["fence"] != fence
            ):
                raise OperationError("stale_worker", "Worker no longer owns this operation")
            updated = deepcopy(record)
            require(
                all(updated.get(name) == current.get(name) for name in _IMMUTABLE),
                "Operation admission identity cannot change",
            )
            require(
                updated.get("resources") == current["resources"],
                "Operation reservations cannot change",
            )
            if current["state"] in TERMINAL:
                raise OperationError("invalid_input", "Terminal operation records are immutable")
            next_state = updated.get("state")
            require(
                next_state == current["state"] or next_state in TRANSITIONS[current["state"]],
                "Operation state transition is invalid",
            )
            require(
                next_state != "running" or current["state"] == "running",
                "Worker ownership must be acquired through claim",
            )
            if (
                worker is not None
                and fence is None
                and current["state"] != "recovery_required"
                and next_state != "recovery_required"
            ):
                raise OperationError("stale_worker", "A worker update requires its current fence")
            if worker is not None:
                changed = updated.get("worker")
                require(
                    isinstance(changed, dict)
                    and all(
                        changed.get(name) == worker[name]
                        for name in (
                            "worker_id",
                            "host_id",
                            "boot_id",
                            "pid",
                            "start_ticks",
                            "fence",
                        )
                    ),
                    "Worker identity cannot change during a commit",
                )
            else:
                require(updated.get("worker") is None)
            require(
                updated.get("started_at") == current["started_at"]
                and updated.get("deadline_at") == current["deadline_at"],
                "Operation execution budget cannot change",
            )
            if current["cancellation"]["requested_at"] is not None:
                require(
                    updated.get("cancellation") == current["cancellation"],
                    "Cancellation cannot be withdrawn",
                )
            require(
                [stage["stage_id"] for stage in updated.get("stages", [])]
                == [stage["stage_id"] for stage in current["stages"]],
                "Operation stages cannot be replaced",
            )
            for old_stage, new_stage in zip(current["stages"], updated["stages"], strict=True):
                if old_stage["state"] in {"succeeded", "reused"}:
                    require(new_stage == old_stage, "Completed stage evidence is immutable")
            updated["revision"] = current["revision"] + 1
            updated["updated_at"] = utc_now()
            self._write(connection, updated)
            return updated

    def list(self, target_id: str, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        """Page frozen summaries, ordered by creation time descending and operation ID."""
        target = self._target(target_id)
        if cursor is not None:
            try:
                cursor = uuid_string(cursor)
            except OperationError:
                raise OperationError(
                    "invalid_cursor", "Operation listing cursor is invalid"
                ) from None
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OperationError(
                "invalid_cursor" if cursor else "invalid_input",
                "Operation listing limit must be between 1 and 100",
            )
        binding = hashlib.sha256(canonical(target.model_dump(mode="json"))).hexdigest()
        with self.connection() as connection:
            if cursor is not None:
                row = connection.execute(
                    "SELECT target_id,binding,page_limit,document FROM pages WHERE cursor=?",
                    (cursor,),
                ).fetchone()
                if row is None or row[:3] != (target_id, binding, limit):
                    raise OperationError(
                        "invalid_cursor", "Operation listing cursor is invalid; restart listing"
                    )
                return _decode(row[3])
            pages: list[list[dict[str, Any]]] = [[]]
            rows = connection.execute(
                "SELECT operation_id FROM operations WHERE target_id=? ORDER BY created_at "
                "DESC,operation_id ASC",
                (target_id,),
            )
            for (operation_id,) in rows:
                item = summary(self._read(connection, target_id, operation_id))
                candidate = {"operations": [*pages[-1], item], "next_cursor": str(uuid4())}
                size = len(json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode())
                if size > MAX_PAGE_BYTES or len(pages[-1]) >= limit:
                    require(bool(pages[-1]), "Operation summary exceeds its page limit")
                    pages.append([])
                pages[-1].append(item)
            tokens = [str(uuid4()) for _ in pages[1:]]
            results = [
                {"operations": page, "next_cursor": tokens[index] if index < len(tokens) else None}
                for index, page in enumerate(pages)
            ]
            for token, result in zip(tokens, results[1:], strict=True):
                connection.execute(
                    "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
                    (token, target_id, binding, limit, encode_record(result)),
                )
            return results[0]

    def tombstone(self, target_id: str, operation_id: str) -> None:
        """Remove a terminal record while preserving every request and consumed-plan claim."""
        self._target(target_id)
        operation_id = uuid_string(operation_id)
        with self.connection() as connection:
            record = self._read(connection, target_id, operation_id)
            if record["state"] not in TERMINAL:
                raise OperationError(
                    "recovery_required", "Active operation records cannot be removed"
                )
            connection.execute(
                "INSERT INTO tombstones VALUES (?, ?, ?)", (operation_id, target_id, utc_now())
            )
            connection.execute("DELETE FROM operations WHERE operation_id=?", (operation_id,))
