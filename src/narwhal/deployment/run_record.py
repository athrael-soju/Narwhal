"""The run record (`run.json`) of a stage run, and the lock that gives one runner its directory."""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import os
import re
import socket
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from narwhal.contracts import DEPLOYMENT_RUN, ContractVersionError, read_json, versioned

from . import stages

RECORD = "run.json"
LOCK = "run.lock"
TARGETS = ("dev", "ssh", "kubernetes")

RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
CANCELLED = "cancelled"
TIMEOUT = "timeout"
INVALIDATED = "invalidated"
STATUSES = (RUNNING, PASSED, FAILED, CANCELLED, TIMEOUT, INVALIDATED)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[0-9a-f]{32}")
_MODE = re.compile(r"0[0-7]{3}")
INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class RunLocked(RuntimeError):
    """Another runner owns the run directory, or the recorded owner still runs."""

    def __init__(self, directory: Path, owner: Mapping[str, Any] | None, detail: str) -> None:
        self.directory = directory
        self.owner = owner
        holder = describe(owner) if owner else "a runner that has not recorded its owner yet"
        super().__init__(f"{directory}: {detail}: {holder}")


def key(stage: str, instance: str) -> str:
    """The record key of one stage instance; a single-scope stage has the empty instance."""
    return f"{stage}[{instance}]" if instance else stage


def owner() -> dict[str, Any]:
    """The identity of this process, as a run record names the runner that holds its lock."""
    pid = os.getpid()
    return {
        "pid": pid,
        "boot_id": _boot_id(),
        "start_ticks": _start_ticks(pid),
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "argv": list(sys.argv),
    }


def owner_running(recorded: Mapping[str, Any]) -> bool:
    """Whether the recorded owner is a live process on this host, by boot and start ticks."""
    if recorded["hostname"] != socket.gethostname() or recorded["boot_id"] != _boot_id():
        return False
    try:
        fields = Path(f"/proc/{recorded['pid']}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[0] not in {"Z", "X"} and int(fields[19]) == recorded["start_ticks"]


def describe(recorded: Mapping[str, Any]) -> str:
    return (
        f"pid {recorded['pid']} of {recorded['user']} on {recorded['hostname']} "
        f"({' '.join(recorded['argv'])})"
    )


@contextlib.contextmanager
def locked(directory: Path) -> Iterator[None]:
    """Hold the run directory's lock; a second holder is refused with the recorded owner."""
    descriptor = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _holder(directory)
            raise RunLocked(directory, holder, "another runner holds this run") from None
        yield
    finally:
        os.close(descriptor)


def _holder(directory: Path) -> Mapping[str, Any] | None:
    # The holder names itself in the record just after it takes the lock, so until then the
    # record can still name an earlier owner that has ended; only a live owner is reported.
    try:
        lock = read(directory)["lock"] if (directory / RECORD).exists() else None
    except (OSError, ContractVersionError):
        return None
    if lock is None or not owner_running(lock["owner"]):
        return None
    owner: Mapping[str, Any] = lock["owner"]
    return owner


def read(directory: Path) -> dict[str, Any]:
    """Read and validate the run record; the result carries no schema fields."""
    path = directory / RECORD
    document = read_json(path, DEPLOYMENT_RUN)
    body = {name: value for name, value in document.items() if name not in _SCHEMA_FIELDS}
    validate(body, path)
    return body


def write(directory: Path, body: Mapping[str, Any]) -> None:
    """Replace the run record atomically at mode 0600."""
    validate(body, directory / RECORD)
    stages.write_evidence(directory / RECORD, versioned(DEPLOYMENT_RUN, body))


_SCHEMA_FIELDS = ("schema", "schema_version")
_TOP = (
    "run_id",
    "target",
    "narwhal_version",
    "revision",
    "graph_sha256",
    "created_at",
    "updated_at",
    "lock",
    "stale_locks",
    "inputs",
    "until",
    "stages",
)
_STAGE = (
    "stage",
    "scope",
    "instance",
    "status",
    "attempt",
    "directory",
    "definition",
    "input_digests",
    "outputs",
    "products",
    "processes",
    "command",
    "started_at",
    "ended_at",
    "failure",
    "invalidation",
)
_OWNER = ("pid", "boot_id", "start_ticks", "hostname", "user", "argv")


def validate(body: Mapping[str, Any], path: Path) -> None:
    """Refuse a run record whose fields differ from the `narwhal.deployment-run` contract."""

    def check(condition: bool, message: str) -> None:
        if not condition:
            raise ContractVersionError(f"{path}: {message}")

    check(set(body) == set(_TOP), f"fields must be {', '.join(_TOP)}")
    check(_text(body["run_id"]) and bool(_RUN_ID.fullmatch(body["run_id"])), "run_id")
    check(body["target"] in TARGETS, f"target must be one of {', '.join(TARGETS)}")
    for name in ("narwhal_version", "revision"):
        check(_text(body[name]), f"{name} must be a nonempty string")
    check(_digest(body["graph_sha256"]), "graph_sha256 must be a sha256: digest")
    for name in ("created_at", "updated_at"):
        check(_number(body[name]), f"{name} must be a number")
    lock = body["lock"]
    check(lock is None or _lock(lock, {"owner", "acquired_at"}), "lock must be null or a lock")
    stale = body["stale_locks"]
    check(
        isinstance(stale, list)
        and all(_lock(entry, {"owner", "acquired_at", "found_at"}) for entry in stale),
        "stale_locks must list each stale lock with its owner, acquired_at and found_at",
    )
    check(isinstance(body["inputs"], dict), "inputs must be an object")
    for name, value in body["inputs"].items():
        check(
            isinstance(value, dict)
            and set(value) == {"digest", "source"}
            and _digest(value["digest"])
            and _text(value["source"]),
            f"inputs.{name} needs a digest and a source",
        )
    check(body["until"] is None or _text(body["until"]), "until must be null or a stage")
    check(isinstance(body["stages"], dict), "stages must be an object")
    for name, stage in body["stages"].items():
        _validate_stage(name, stage, check)


def _validate_stage(name: str, stage: Any, check: Any) -> None:
    where = f"stages.{name}"
    check(isinstance(stage, dict) and set(stage) == set(_STAGE), f"{where} fields")
    check(_text(stage["stage"]) and _text(stage["scope"]), f"{where} needs stage and scope")
    check(
        stage["instance"] == "" or bool(INSTANCE.fullmatch(str(stage["instance"]))),
        f"{where}.instance",
    )
    check(name == key(stage["stage"], stage["instance"]), f"{where} key names another instance")
    check(stage["status"] in STATUSES, f"{where}.status must be one of {', '.join(STATUSES)}")
    check(_count(stage["attempt"], 1), f"{where}.attempt must be a positive integer")
    check(_relative(stage["directory"]), f"{where}.directory must be a run-relative path")
    check(_digest(stage["definition"]), f"{where}.definition must be a digest")
    digests = stage["input_digests"]
    check(
        isinstance(digests, dict) and all(_digest(value) for value in digests.values()),
        f"{where}.input_digests must map names to digests",
    )
    outputs = stage["outputs"]
    check(isinstance(outputs, list), f"{where}.outputs must be a list")
    for output in outputs:
        check(
            isinstance(output, dict)
            and set(output) == {"path", "sha256", "size", "mode"}
            and _relative(output["path"])
            and isinstance(output["sha256"], str)
            and bool(_HEX.fullmatch(output["sha256"]))
            and _count(output["size"], 0)
            and isinstance(output["mode"], str)
            and bool(_MODE.fullmatch(output["mode"])),
            f"{where}.outputs needs path, sha256, size and mode",
        )
    check(isinstance(stage["products"], dict), f"{where}.products must be an object")
    check(
        isinstance(stage["processes"], list)
        and all(isinstance(process, dict) for process in stage["processes"]),
        f"{where}.processes must be a list of identities",
    )
    command = stage["command"]
    check(
        command is None
        or (
            isinstance(command, dict)
            and set(command) == {"argv", "cwd", "exit_code", "log"}
            and _strings(command["argv"])
            and _relative(command["cwd"])
            and (command["exit_code"] is None or _count(command["exit_code"], -255))
            and _relative(command["log"])
        ),
        f"{where}.command must be null or argv, cwd, exit_code and log",
    )
    check(_number(stage["started_at"]), f"{where}.started_at must be a number")
    check(
        stage["ended_at"] is None or _number(stage["ended_at"]),
        f"{where}.ended_at must be null or a number",
    )
    failure = stage["failure"]
    check(
        failure is None
        or (
            isinstance(failure, dict)
            and set(failure) == {"message", "recovery", "context"}
            and _text(failure["message"])
            and _text(failure["recovery"])
            and isinstance(failure["context"], dict)
        ),
        f"{where}.failure must be null or message, recovery and context",
    )
    running = stage["status"] == RUNNING
    check(running == (stage["ended_at"] is None), f"{where}: only a running stage has no end")
    check(
        (stage["status"] in {RUNNING, PASSED, INVALIDATED}) == (failure is None),
        f"{where}: a failed, cancelled or timed-out stage records its failure",
    )
    invalidated = stage["status"] == INVALIDATED
    check(
        (stage["invalidation"] is None) != invalidated
        and (stage["invalidation"] is None or _text(stage["invalidation"])),
        f"{where}: an invalidated stage, and only it, records why",
    )


def _lock(value: Any, fields: set[str]) -> bool:
    if not isinstance(value, dict) or set(value) != fields:
        return False
    owner = value["owner"]
    return (
        all(_number(value[name]) for name in fields - {"owner"})
        and isinstance(owner, dict)
        and set(owner) == set(_OWNER)
        and _count(owner["pid"], 1)
        and _count(owner["start_ticks"], 0)
        and all(_text(owner[name]) for name in ("boot_id", "hostname", "user"))
        and _strings(owner["argv"])
    )


def _relative(value: Any) -> bool:
    """A path inside the run directory: relative, and never through `..`."""
    if not _text(value):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _start_ticks(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _count(value: Any, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
