"""Authenticate fixed nested commands with a private inherited descriptor."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .management_access import directory, read_input, read_object
from .management_records import OperationError, encode_record, uuid_string
from .management_registry import ManagementRegistry, load_registry

if TYPE_CHECKING:
    from .management_executor import StageContext

CONTEXT_ENV = "NARWHAL_MANAGEMENT_CONTEXT_FD"
_MAX_BYTES = 65536
# Linux UAPI linux/fcntl.h fixes these values even when Python's build headers
# omit their fcntl exports. The kernel still enforces and reports every seal.
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEALS = (
    getattr(fcntl, "F_SEAL_WRITE", 0x0008)
    | getattr(fcntl, "F_SEAL_GROW", 0x0004)
    | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
    | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
)


@dataclass(frozen=True)
class CommandContext:
    """Carry a private credential only to the intended command and its fixed helpers."""

    env: dict[str, str]
    pass_fds: tuple[int, ...]
    owner: dict[str, str]


def _denied() -> OperationError:
    return OperationError("permission_denied", "Managed command context is missing or invalid")


def _private(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > _MAX_BYTES
    ):
        raise _denied()


def _decode(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise _denied()
    return value


def _private_object(root: Path, name: str) -> dict[str, Any]:
    with directory(root, private=True) as parent:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
        )
        try:
            _private(descriptor)
            return _decode(os.read(descriptor, _MAX_BYTES + 1))
        finally:
            os.close(descriptor)


@contextlib.contextmanager
def command_context(
    context: StageContext,
    *,
    command: list[str],
    input_hashes: dict[str, str],
    registry_path: Path | None = None,
) -> Iterator[CommandContext]:
    """Issue a sealed credential for this worker's exact command and input snapshot."""
    context.assert_current()
    record = context.read()
    if (
        record["tool"] not in {"plan_execute", "operation_resume"}
        or record["worker"]["pid"] != os.getpid()
        or record["worker"]["fence"] != context.fence
    ):
        raise _denied()
    selected = registry_path or Path(os.environ.get("NARWHAL_MANAGEMENT_REGISTRY", ""))
    if not selected.is_absolute():
        raise _denied()
    registry = load_registry(selected)
    if (
        registry.registry_id != context.registry.registry_id
        or registry.state_dir != context.registry.state_dir
    ):
        raise _denied()
    identity = str(uuid4())
    nonce = secrets.token_hex(32)
    payload = {
        "grant_id": identity,
        "registry_path": str(selected),
        "registry_id": str(registry.registry_id),
        "target_id": context.target_id,
        "operation_id": context.operation_id,
        "stage_id": context.stage_id,
        "fence": context.fence,
        "action": record["action"],
        "plan_id": record["plan_id"],
        "instance": str(context.target.instance_dir),
        "command": command,
        "expected_inputs": input_hashes,
        "cleanup": context.stage["cleanup"],
        "deadline": context.deadline,
        "hard_deadline": context.hard_deadline,
        "operation_deadline": context.operation_deadline,
        "owner": {
            "operation_id": context.operation_id,
            "stage_id": context.stage_id,
            "launch_token": str(uuid4()),
        },
        "nonce": nonce,
    }
    if record["action"] in {"dev_verify", "dev_down"} and context.target.instance_dir is not None:
        state_path = context.target.instance_dir / "lifecycle.json"
        previous = read_object(state_path) if state_path.exists() else {}
        payload["generation_owner"] = previous.get("management_owner")
        payload["generation_run"] = previous.get("run")
    descriptor = os.memfd_create(
        "narwhal-management-context", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    completion = None
    name = f"command-{identity}.json"
    try:
        completion = os.memfd_create("narwhal-command-completion", os.MFD_CLOEXEC)
        os.fchmod(completion, 0o600)
        info = os.fstat(completion)
        payload["completion"] = {
            "fd": completion,
            "device": info.st_dev,
            "inode": info.st_ino,
        }
        raw = encode_record(payload)
        if len(raw) > _MAX_BYTES:
            raise _denied()
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, raw)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _SEALS)
        with directory(registry.state_dir, private=True, create=True) as parent:
            grant = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(grant, "wb") as stream:
                stream.write(encode_record({"sha256": hashlib.sha256(raw).hexdigest()}))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
            try:
                yield CommandContext(
                    {"NARWHAL_MANAGEMENT_REGISTRY": str(selected), CONTEXT_ENV: str(descriptor)},
                    (descriptor, completion),
                    payload["owner"],
                )
            finally:
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
                raw_completion = os.pread(completion, 257, 0)
                if raw_completion:
                    value = _decode(raw_completion)
                    code = value.get("text_exit_code")
                    if (
                        set(value) != {"text_exit_code"}
                        or type(code) is not int
                        or not 0 <= code <= 255
                    ):
                        raise OperationError(
                            "invalid_input", "Managed command completion is invalid"
                        )
                    receipt = {
                        "operation_id": context.operation_id,
                        "stage_id": context.stage_id,
                        "action": record["action"],
                        "text_exit_code": code,
                    }
                    output = os.open(
                        f"dev-exit-{context.operation_id}.json",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=parent,
                    )
                    with os.fdopen(output, "wb") as stream:
                        stream.write(encode_record(receipt))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(parent)
    finally:
        os.close(descriptor)
        if completion is not None:
            os.close(completion)


def command_exit_reporter() -> Callable[[int], None] | None:
    """Return a private completion sink when the issuer supplied an authenticated one.

    Recording an outcome does not authorize command execution. The normal guard
    still checks current grants and bindings before the command can run.
    """
    selected = os.environ.get(CONTEXT_ENV)
    if selected is None:
        return None
    try:
        if not selected.isdecimal() or int(selected) < 3:
            raise _denied()
        descriptor = int(selected)
        _private(descriptor)
        if fcntl.fcntl(descriptor, _F_GET_SEALS) & _SEALS != _SEALS:
            raise _denied()
        raw = os.pread(descriptor, _MAX_BYTES + 1, 0)
        payload = _decode(raw)
        registry = load_registry(Path(payload["registry_path"]))
        if (
            str(registry.registry_id) != payload["registry_id"]
            or os.environ.get("NARWHAL_MANAGEMENT_REGISTRY") != payload["registry_path"]
        ):
            raise _denied()
        grant = _private_object(
            registry.state_dir, f"command-{uuid_string(payload['grant_id'])}.json"
        )
        if not secrets.compare_digest(grant["sha256"], hashlib.sha256(raw).hexdigest()):
            raise _denied()
        sink = payload["completion"]
        completion = sink["fd"]
        if type(completion) is not int or completion < 3:
            raise _denied()
        _private(completion)
        info = os.fstat(completion)
        if (info.st_dev, info.st_ino) != (sink["device"], sink["inode"]):
            raise _denied()
    except (OSError, ValueError, KeyError, TypeError):
        return None

    def report(code: int) -> None:
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError("Command exit code is invalid")
        value = encode_record({"text_exit_code": code})
        os.pwrite(completion, value, 0)
        os.ftruncate(completion, len(value))

    return report


def completed_command_exit(registry: ManagementRegistry, record: dict[str, Any]) -> int:
    """Read the private text outcome for this operation's completed dev command."""
    operation_id = uuid_string(record["operation_id"])
    try:
        receipt = _private_object(registry.state_dir, f"dev-exit-{operation_id}.json")
    except (OSError, ValueError):
        raise OperationError("invalid_input", "Managed command completion is unavailable") from None
    code = receipt.get("text_exit_code")
    if (
        set(receipt) != {"operation_id", "stage_id", "action", "text_exit_code"}
        or receipt["operation_id"] != operation_id
        or receipt["action"] != record["action"]
        or not any(row["stage_id"] == receipt["stage_id"] for row in record["stages"])
        or type(code) is not int
        or not 0 <= code <= 255
    ):
        raise OperationError(
            "invalid_input", "Managed command completion does not match its operation"
        )
    return code


def _descendant(worker: dict[str, Any]) -> bool:
    from . import stages
    from .management_executor import worker_alive

    if not worker_alive(worker):
        return False
    processes = stages._processes()
    pid = os.getpid()
    for _ in range(256):
        if pid == worker["pid"]:
            return True
        row = processes.get(pid)
        if row is None or row[1] in {0, pid}:
            return False
        pid = row[1]
    return False


def inherited_context(instance: Path | None = None) -> dict[str, Any] | None:
    """Verify the inherited grant against current permissions, worker identity and fence."""
    selected = os.environ.get(CONTEXT_ENV)
    if selected is None:
        return None
    try:
        if not selected.isdecimal() or int(selected) < 3:
            raise _denied()
        descriptor = int(selected)
        _private(descriptor)
        if fcntl.fcntl(descriptor, _F_GET_SEALS) & _SEALS != _SEALS:
            raise _denied()
        raw = os.pread(descriptor, _MAX_BYTES + 1, 0)
        payload = _decode(raw)
        identity = uuid_string(payload["grant_id"])
        registry_path = Path(payload["registry_path"])
        if os.environ.get("NARWHAL_MANAGEMENT_REGISTRY") != str(registry_path):
            raise _denied()
        registry = load_registry(registry_path)
        if str(registry.registry_id) != payload["registry_id"]:
            raise _denied()
        with directory(registry.state_dir, private=True) as parent:
            grant = os.open(
                f"command-{identity}.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
            try:
                _private(grant)
                expected = _decode(os.read(grant, _MAX_BYTES + 1))
            finally:
                os.close(grant)
        if not secrets.compare_digest(expected["sha256"], hashlib.sha256(raw).hexdigest()):
            raise _denied()
        from .management_coordinator import OperationCoordinator
        from .management_plans import PlanStore

        coordinator = OperationCoordinator(registry, registry_path, entry_point="cli")
        target = coordinator.authorize_action(
            payload["target_id"],
            payload["action"],
            operation={**payload, "current_stage": payload["stage_id"]},
        )
        record = coordinator.store.read(target.id, payload["operation_id"])
        worker = record["worker"]
        if (
            record["state"] not in {"running", "cancelling"}
            or not isinstance(worker, dict)
            or worker["fence"] != payload["fence"]
            or record["action"] != payload["action"]
            or record["plan_id"] != payload["plan_id"]
            or record["current_stage"] != payload["stage_id"]
            or str(target.instance_dir) != payload["instance"]
            or not _descendant(worker)
        ):
            raise _denied()
        if record["cancellation"]["requested_at"] is not None:
            raise OperationError("stage_cancelled", "Managed operation cancellation was requested")
        if instance is not None and instance.resolve() != target.instance_dir:
            raise _denied()
        if payload["plan_id"] is not None:
            plan = PlanStore(registry, target.id).read(payload["plan_id"])
            coordinator._check_local_binding(target, plan)
        # Callers inside the lifecycle lock check these before changing instance state.
        if instance is not None:
            for path, digest in payload["expected_inputs"].items():
                if hashlib.sha256(read_input(Path(path))).hexdigest() != digest:
                    raise OperationError(
                        "stale_plan", "Registered dev input changed before execution"
                    )
            if payload["plan_id"] is not None:
                from .management_executor import StageContext

                remaining = payload["deadline"] - time.monotonic()
                if remaining <= 0:
                    raise OperationError(
                        "stage_timeout", "Dev input check exceeded its stage budget"
                    )
                stage = next(
                    row
                    for row in plan["payload"]["stages"]
                    if row["stage_id"] == payload["stage_id"]
                )
                inspection = StageContext(
                    registry,
                    target.id,
                    record["operation_id"],
                    fence=payload["fence"],
                    stage=stage,
                    deadline=payload["deadline"],
                    read_only=False,
                )
                inspection.hard_deadline = payload["hard_deadline"]
                inspection.operation_deadline = payload["operation_deadline"]
                coordinator.adapter(target).check(inspection, target, plan)
        return payload
    except OperationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise _denied() from None


def inherited_fds() -> tuple[int, ...]:
    """Forward the authenticated descriptor only to trusted finite helper commands."""
    return (int(os.environ[CONTEXT_ENV]),) if inherited_context() is not None else ()


def authorize_command(
    command: str,
    *,
    action: str | None,
    instance: Path | None,
    fleet: Path | None,
    arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Allow only the admitted dev command or its bounded installed helper calls."""
    context = inherited_context()
    if context is None:
        return False
    root = Path(context["instance"])
    parent_action = context["action"]
    if command == "narwhal" and action == parent_action and instance == root:
        if arguments is None or "argv" not in arguments:
            raise _denied()

        def normalized(values: list[str]) -> list[str]:
            selected = values[values.index("dev") :]
            return [
                value
                for index, value in enumerate(selected)
                if value != "--format" and (index == 0 or selected[index - 1] != "--format")
            ]

        if normalized(list(arguments["argv"])) == normalized(context["command"]):
            return True
        raise _denied()
    try:
        lifecycle = _decode(read_input(root / "lifecycle.json"))
        run = Path(lifecycle["run"])
        owner = context["owner"] if parent_action == "dev_up" else context.get("generation_owner")
        if (
            run.parent != root
            or lifecycle.get("management_owner") != owner
            or (parent_action != "dev_up" and str(run) != context.get("generation_run"))
            or (owner is not None and _decode(read_input(run / "management-owner.json")) != owner)
        ):
            raise _denied()
    except (OSError, ValueError, KeyError, TypeError):
        raise _denied() from None
    if fleet is not None:
        selected = fleet.resolve()
        inside = selected == run / "fleet.json"
        if inside and (
            (
                command == "narwhal-profile"
                and parent_action == "dev_up"
                and action == "fleet_profile"
            )
            or (
                command == "narwhal-check"
                and parent_action == "dev_verify"
                and action == "fleet_preflight"
            )
        ):
            return True
    if command == "narwhal-engine" and parent_action == "dev_up" and arguments is not None:
        selected_action = arguments.get("action", arguments.get("command"))
        paths = arguments.get("run")
        values = paths if isinstance(paths, list) else [paths]
        if (
            selected_action in {"check", "start-shared"}
            and values
            and all(
                isinstance(path, Path)
                and path.resolve().parent == run
                and path.name.startswith("engine-")
                for path in values
            )
        ):
            return True
    raise _denied()
