"""Run fixed adapter jobs with durable ownership on an existing Linux SSH host.

The transport sends this standard-library module from the verified installed
package. Its JSON protocol is private to the adapter and accepts trusted argv,
never MCP arguments. No SSH session is an ownership receipt.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import fcntl
import hashlib
import http.client
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

MAX_REQUEST = 1_048_576
MAX_RECEIPT = 1_048_576
MAX_STDOUT = 16_777_216
MAX_STDERR = 65_536
MAX_READ = 65_536
CONTEXT_ENV = "NARWHAL_SSH_JOB_FD"
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEALS = sum(
    getattr(fcntl, name, value)
    for name, value in (
        ("F_SEAL_WRITE", 8),
        ("F_SEAL_GROW", 4),
        ("F_SEAL_SHRINK", 2),
        ("F_SEAL_SEAL", 1),
    )
)
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "recovery_required"}
LABEL_PREFIX = "io.narwhal.management."


class WorkerError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise WorkerError("invalid_input", "Remote job identifier is invalid")
    return value


def _absolute(value: Any) -> Path:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or ".." in Path(value).parts
        or any(c in value for c in "\0\r\n")
    ):
        raise WorkerError("invalid_input", "Remote path is invalid")
    return Path(value)


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkerError("invalid_input", "Remote job bound is invalid")
    return value


def _decode(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite value")),
    )
    if not isinstance(value, dict):
        raise WorkerError("invalid_input", "Remote request must be an object")
    return value


def _encoded(value: dict[str, Any]) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_RECEIPT:
        raise WorkerError("source_truncated", "Remote receipt exceeds its bound")
    return raw


@contextlib.contextmanager
def _directory(path: Path, *, create: bool = False, private: bool = True) -> Iterator[int]:
    _absolute(str(path))
    with contextlib.ExitStack() as stack:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        stack.callback(os.close, fd)
        for index, part in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
            fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            stack.callback(os.close, fd)
        info = os.fstat(fd)
        if (
            info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or (private and stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise WorkerError("permission_denied", "Remote job directory is not private")
        yield fd


def _read(directory: int, name: str, *, maximum: int = MAX_RECEIPT) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise WorkerError("permission_denied", "Remote job file is not private")
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise WorkerError("source_truncated", "Remote job file exceeds its bound")
        return raw


def _write(directory: int, name: str, data: bytes) -> None:
    temporary = ".pending-" + uuid4().hex
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _record(directory: int, value: dict[str, Any]) -> None:
    value["updated_at_ms"] = time.time_ns() // 1_000_000
    _write(directory, "receipt.json", _encoded(value))


def _proc(pid: int) -> tuple[str, int, int, int] | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return value[0], int(value[1]), int(value[2]), int(value[19])
    except (OSError, ValueError, IndexError):
        return None


def _boot() -> str:
    return _uuid(Path("/proc/sys/kernel/random/boot_id").read_text().strip())


def _identity(pid: int) -> dict[str, Any]:
    row = _proc(pid)
    if row is None:
        raise WorkerError("recovery_required", "Remote process identity is unavailable")
    return {"boot_id": _boot(), "pid": pid, "start_ticks": row[3]}


def _alive(identity: dict[str, Any]) -> bool:
    row = _proc(identity["pid"])
    return (
        identity["boot_id"] == _boot()
        and row is not None
        and row[0] not in {"Z", "X"}
        and row[3] == identity["start_ticks"]
    )


def _members(known: dict[int, int], supervisor: dict[str, Any]) -> dict[int, int]:
    if supervisor["boot_id"] != _boot():
        return {}
    rows = {}
    for path in Path("/proc").iterdir():
        if (
            path.name.isdigit()
            and (row := _proc(int(path.name))) is not None
            and row[0] not in {"Z", "X"}
        ):
            rows[int(path.name)] = row
    owned = {pid: ticks for pid, ticks in known.items() if pid in rows and rows[pid][3] == ticks}
    anchor = supervisor["pid"]
    if anchor in rows and rows[anchor][3] == supervisor["start_ticks"]:
        owned[anchor] = supervisor["start_ticks"]
    for _ in range(len(rows) + 1):
        added = {pid: row[3] for pid, row in rows.items() if pid not in owned and row[1] in owned}
        if not added:
            break
        owned.update(added)
    owned.pop(anchor, None)
    if len(owned) > 4096:
        raise WorkerError("source_truncated", "Remote process tree exceeds its bound")
    return owned


def _signal(processes: dict[int, int], sig: int) -> None:
    for pid, ticks in processes.items():
        row = _proc(pid)
        if row is not None and row[0] not in {"Z", "X"} and row[3] == ticks:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)


def _command(argv: list[str], deadline: float, *, maximum: int = MAX_RECEIPT) -> bytes:
    executable = shutil.which(argv[0])
    if executable is None:
        raise WorkerError(
            "prerequisite_failed", "Required remote inspection command is unavailable"
        )
    process = subprocess.Popen(
        [executable, *argv[1:]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    error_size = 0
    with selectors.DefaultSelector() as selected:
        assert process.stdout is not None and process.stderr is not None
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selected.register(stream, selectors.EVENT_READ)
        try:
            while selected.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("stage_timeout", "Remote inspection deadline expired")
                for key, _ in selected.select(min(remaining, 0.05)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selected.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        output.extend(data)
                    else:
                        error_size += len(data)
                    if len(output) > maximum or error_size > MAX_STDERR:
                        raise WorkerError(
                            "source_truncated", "Remote inspection output exceeds its bound"
                        )
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if code:
                raise WorkerError("source_unavailable", "Remote inspection command failed")
            return bytes(output)
        finally:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()


def _labels(owner: dict[str, Any]) -> dict[str, str]:
    return {
        LABEL_PREFIX + name: owner[key]
        for name, key in (
            ("operation", "operation_id"),
            ("stage", "stage_id"),
            ("launch", "launch_token"),
        )
    }


def _containers(record: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
    if record.get("daemon_id") is None:
        return []
    daemon = _command(["docker", "info", "--format", "{{.ID}}"], deadline).decode().strip()
    if daemon != record["daemon_id"]:
        raise WorkerError("ownership_conflict", "Remote container daemon identity changed")
    labels = record["container_labels"]
    args = ["docker", "ps", "-aq", "--no-trunc"]
    for name, value in labels.items():
        args += ["--filter", f"label={name}={value}"]
    ids = _command(args, deadline).decode().split()
    if len(ids) > 128 or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        raise WorkerError("invalid_input", "Remote container listing is invalid")
    if not ids:
        return []
    rows = json.loads(_command(["docker", "inspect", *ids], deadline))
    results = []
    for row in rows:
        actual = row.get("Config", {}).get("Labels") or {}
        if row.get("Id") not in ids or any(
            actual.get(key) != value for key, value in labels.items()
        ):
            raise WorkerError("ownership_conflict", "Remote container ownership changed")
        process = None
        descendants: dict[str, int] = {}
        state = row.get("State", {})
        pid = state.get("Pid")
        restarting_without_process = (
            state.get("Restarting") is True and type(pid) is int and pid == 0
        )
        if state.get("Running") and not restarting_without_process:
            if type(pid) is not int or pid <= 0:
                raise WorkerError("source_unavailable", "Container host PID is unavailable")
            process = _identity(pid)
            descendants = {str(key): value for key, value in _members({}, process).items()}
            if not _alive(process):
                raise WorkerError(
                    "source_unavailable", "Container process changed during inspection"
                )
        results.append(
            {
                "daemon_id": daemon,
                "container_id": row["Id"],
                "labels": labels,
                "running": bool(state.get("Running")),
                "process": process,
                "descendants": descendants,
            }
        )
    if len(results) != len(ids):
        raise WorkerError("source_unavailable", "Remote container inspection is incomplete")
    return results


def _stop_containers(record: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
    for item in _containers(record, deadline):
        _command(["docker", "rm", "-f", item["container_id"]], deadline)
    return _containers(record, deadline)


def _context_fd(root: Path, record: dict[str, Any]) -> int:
    fd = os.memfd_create("narwhal-ssh-job", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    payload = {
        "schema": "narwhal.ssh-context",
        "schema_version": 1,
        "root": str(root),
        "job_id": record["job_id"],
        "owner": record["owner"],
        "fence": record["fence"],
        "supervisor": record["supervisor"],
    }
    os.write(fd, _encoded(payload))
    os.lseek(fd, 0, os.SEEK_SET)
    fcntl.fcntl(
        fd,
        _F_ADD_SEALS,
        _SEALS,
    )
    return fd


def inherited_owner() -> dict[str, str] | None:
    """Authenticate a sealed descriptor and the live remote supervisor ancestry."""
    value = os.environ.get(CONTEXT_ENV)
    if value is None:
        return None
    try:
        fd = int(value)
        info = os.fstat(fd)
        required = _SEALS
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or fcntl.fcntl(fd, _F_GET_SEALS) & required != required
            or info.st_size > MAX_RECEIPT
        ):
            raise ValueError("Unsafe context")
        context = _decode(os.pread(fd, MAX_RECEIPT, 0))
        with _directory(_absolute(context["root"]) / _uuid(context["job_id"])) as directory:
            record = _decode(_read(directory, "receipt.json"))
        if (
            context.get("schema") != "narwhal.ssh-context"
            or context.get("schema_version") != 1
            or record["owner"] != context["owner"]
            or record["fence"] != context["fence"]
            or record["supervisor"] != context["supervisor"]
            or not _alive(record["supervisor"])
            or record["state"] not in {"running", "awaiting_retain", "retained"}
        ):
            raise ValueError("Invalid context")
        pid = os.getpid()
        for _ in range(256):
            row = _proc(pid)
            if row is None:
                break
            if pid == record["supervisor"]["pid"] and row[3] == record["supervisor"]["start_ticks"]:
                return dict(record["owner"])
            if row[1] <= 1 or row[1] == pid:
                break
            pid = row[1]
        raise ValueError("Unrelated process")
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkerError("permission_denied", "Remote job context is invalid") from None


def _spec(request: dict[str, Any]) -> dict[str, Any]:
    required = {
        "job_id",
        "operation_id",
        "stage_id",
        "fence",
        "argv",
        "cwd",
        "env",
        "timeout_ms",
        "term_grace_ms",
        "kill_grace_ms",
    }
    if set(request) - required - {"containers", "secret_env"} or not required <= request.keys():
        raise WorkerError("invalid_input", "Remote job fields are invalid")
    _uuid(request["job_id"])
    _uuid(request["operation_id"])
    if not isinstance(request["stage_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", request["stage_id"]
    ):
        raise WorkerError("invalid_input", "Remote stage is invalid")
    _integer(request["fence"], 1, 2**63 - 1)
    args = request["argv"]
    if (
        not isinstance(args, list)
        or not 1 <= len(args) <= 512
        or any(not isinstance(arg, str) or len(arg) > 65536 or "\0" in arg for arg in args)
    ):
        raise WorkerError("invalid_input", "Remote argument vector is invalid")
    _absolute(args[0])
    _absolute(request["cwd"])
    env = request["env"]
    if (
        not isinstance(env, dict)
        or len(env) > 256
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str)
            or len(value) > 65536
            or "\0" in value
            or key.startswith("NARWHAL_SSH_JOB_")
            for key, value in env.items()
        )
    ):
        raise WorkerError("invalid_input", "Remote environment is invalid")
    for name in ("timeout_ms", "term_grace_ms", "kill_grace_ms"):
        _integer(request[name], 1, 86_400_000)
    if type(request.get("containers", False)) is not bool:
        raise WorkerError("invalid_input", "Remote container selection is invalid")
    names = request.get("secret_env", [])
    if not isinstance(names, list) or any(
        not isinstance(name, str) or name not in env for name in names
    ):
        raise WorkerError("invalid_input", "Remote credential references are invalid")
    return request


def _redacted(data: bytes, secrets: list[bytes], *, final: bool) -> bytes:
    if not final and secrets:
        data = data[: -max(map(len, secrets))] if len(data) > max(map(len, secrets)) else b""
    for secret in sorted(secrets, key=len, reverse=True):
        data = data.replace(secret, b"[redacted]")
    return data


def _finish_cleanup(
    directory: int, record: dict[str, Any], known: dict[int, int], term_ms: int, kill_ms: int
) -> None:
    started = time.monotonic()
    deadline = started + (term_ms + kill_ms) / 1000
    for sig, budget in ((signal.SIGTERM, term_ms), (signal.SIGKILL, kill_ms)):
        until = min(deadline, time.monotonic() + budget / 1000)
        while True:
            current = _members(known, record["supervisor"])
            known.update(current)
            _signal(current, sig)
            if not current or time.monotonic() >= until:
                break
            time.sleep(min(0.02, max(0, until - time.monotonic())))
    containers: list[dict[str, Any]] = []
    error = None
    try:
        containers = _stop_containers(record, deadline)
    except WorkerError as exc:
        error = exc.code
    remaining = _members(known, record["supervisor"])
    record["processes"] = {str(pid): ticks for pid, ticks in remaining.items()}
    record["containers"] = containers
    record["cleanup"] = {
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "surviving_processes": record["processes"],
        "containers": containers,
        "error": error,
    }
    if remaining or containers or error:
        record["state"] = "recovery_required"
    _record(directory, record)


def _flag(directory: int, name: str, record: dict[str, Any]) -> bool:
    try:
        value = _decode(_read(directory, name))
    except FileNotFoundError:
        return False
    if value != {"owner": record["owner"], "fence": record["fence"]}:
        raise WorkerError("ownership_conflict", "Remote job control identity differs")
    return True


def _monitor(root: Path, job: dict[str, Any], ready: int, inherited_lock: int) -> None:
    os.close(inherited_lock)
    os.setsid()
    for fd in (0, 1, 2):
        replacement = os.open(os.devnull, os.O_RDWR)
        os.dup2(replacement, fd)
        if replacement > 2:
            os.close(replacement)
    if os.read(ready, 1) != b"1":
        os._exit(0)
    os.close(ready)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        os._exit(1)
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    with _directory(root / job["job_id"]) as directory:
        record = _decode(_read(directory, "receipt.json"))
        context_fd = _context_fd(root, record)
        gate_read, gate_write = os.pipe()
        code = (
            "import os,sys; fd=int(sys.argv[1]); ok=os.read(fd,1); os.close(fd); "
            "os.execvpe(sys.argv[2],sys.argv[2:],os.environ) if ok==b'1' else sys.exit(125)"
        )
        environment = {**job["env"], CONTEXT_ENV: str(context_fd)}
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(gate_read), *job["argv"]],
            cwd=job["cwd"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(gate_read, context_fd),
        )
        os.close(gate_read)
        os.close(context_fd)
        identity = _identity(child.pid)
        known = {child.pid: identity["start_ticks"]}
        record.update(
            state="running", child=identity, processes={str(child.pid): identity["start_ticks"]}
        )
        _record(directory, record)
        os.write(gate_write, b"1")
        os.close(gate_write)
        end = time.monotonic() + job["timeout_ms"] / 1000
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {"stdout": MAX_STDOUT, "stderr": MAX_STDERR}
        secret_names = set(job.get("secret_env", [])) | {
            name for name in job["env"] if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", name)
        }
        secrets = [job["env"][name].encode() for name in secret_names if job["env"][name]]
        next_save = 0.0
        next_containers = 0.0
        failure = None
        with selectors.DefaultSelector() as selected:
            assert child.stdout is not None and child.stderr is not None
            for stream, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selected.register(stream, selectors.EVENT_READ, name)
            try:
                while True:
                    now = time.monotonic()
                    current = _members(known, record["supervisor"])
                    known.update(current)
                    record["processes"] = {str(pid): ticks for pid, ticks in current.items()}
                    if now >= next_containers:
                        try:
                            record["containers"] = _containers(
                                record,
                                min(end, now + 2) if record["state"] != "retained" else now + 2,
                            )
                            record.pop("container_error", None)
                        except WorkerError as exc:
                            record["container_error"] = exc.code
                        next_containers = now + 1
                    if _flag(directory, "cancel.json", record):
                        record["state"] = "cancelled"
                        failure = "stage_cancelled"
                        break
                    retained = _flag(directory, "retain.json", record)
                    if retained:
                        record["state"] = "retained"
                    elif now >= end:
                        record["state"] = "timed_out"
                        failure = "stage_timeout"
                        break
                    for key, _ in selected.select(0.02):
                        data = os.read(key.fd, 65536)
                        name = key.data
                        if not data:
                            selected.unregister(key.fileobj)
                        else:
                            remaining = limits[name] - len(buffers[name])
                            buffers[name].extend(data[:remaining])
                            if len(data) > remaining:
                                record[name + "_truncated"] = True
                                if not retained:
                                    record["state"] = "failed"
                                    failure = "source_truncated"
                    if failure:
                        break
                    returncode = child.poll()
                    record["exit_code"] = returncode
                    if returncode is not None and not selected.get_map():
                        if record.get("daemon_id") is not None:
                            try:
                                record["containers"] = _containers(
                                    record,
                                    min(end, time.monotonic() + 2)
                                    if not retained
                                    else time.monotonic() + 2,
                                )
                                record.pop("container_error", None)
                            except WorkerError as exc:
                                record["container_error"] = exc.code
                        if returncode:
                            record["state"] = "failed"
                            failure = "command_failed"
                            break
                        if (
                            not current
                            and not record.get("containers")
                            and not record.get("container_error")
                        ):
                            record["state"] = "succeeded"
                            break
                        if not retained:
                            record["state"] = "awaiting_retain"
                    if now >= next_save:
                        _record(directory, record)
                        for name, content in buffers.items():
                            _write(
                                directory,
                                name + ".log",
                                _redacted(bytes(content), secrets, final=False),
                            )
                        next_save = now + 0.5
            except BaseException:
                failure = "adapter_failed"
                record["state"] = "failed"
            finally:
                for stream in (child.stdout, child.stderr):
                    stream.close()
                if failure:
                    record["error"] = {"code": failure, "message": "Remote job did not complete"}
                    _finish_cleanup(
                        directory, record, known, job["term_grace_ms"], job["kill_grace_ms"]
                    )
                for name, content in buffers.items():
                    value = _redacted(bytes(content), secrets, final=True)
                    _write(directory, name + ".log", value)
                    record[name + "_bytes"] = len(value)
                if child.poll() is not None:
                    record["exit_code"] = child.returncode
                _record(directory, record)
                with contextlib.suppress(ChildProcessError):
                    while os.waitpid(-1, os.WNOHANG)[0]:
                        pass
    os._exit(0)


def submit(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    job = _spec(request)
    secret_names = set(job.get("secret_env", [])) | {
        name for name in job["env"] if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", name)
    }
    public = {
        **job,
        "env": {
            name: "[credential]" if name in secret_names else value
            for name, value in job["env"].items()
        },
    }
    digest = hashlib.sha256(_encoded(public)).hexdigest()
    with _directory(root, create=True) as root_fd, contextlib.suppress(FileExistsError):
        os.mkdir(job["job_id"], 0o700, dir_fd=root_fd)
        os.fsync(root_fd)
    with _directory(root / job["job_id"]) as directory:
        lock = os.open(
            "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
        try:
            metadata = os.fstat(lock)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
            ):
                raise WorkerError("permission_denied", "Remote job lock is unsafe")
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                record = _decode(_read(directory, "receipt.json"))
            except FileNotFoundError:
                record = None
            if record is not None:
                if record["request_sha256"] != digest:
                    raise WorkerError(
                        "request_conflict", "Remote job identifier has different inputs"
                    )
                return record
            daemon = None
            if job.get("containers"):
                daemon = (
                    _command(["docker", "info", "--format", "{{.ID}}"], time.monotonic() + 5)
                    .decode()
                    .strip()
                )
                if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", daemon):
                    raise WorkerError(
                        "prerequisite_failed", "Remote container daemon identity is invalid"
                    )
            owner = {
                "operation_id": job["operation_id"],
                "stage_id": job["stage_id"],
                "launch_token": job["job_id"],
            }
            record = {
                "schema": "narwhal.ssh-job",
                "schema_version": 1,
                "job_id": job["job_id"],
                "owner": owner,
                "fence": job["fence"],
                "request_sha256": digest,
                "state": "queued",
                "submitted_at_ms": time.time_ns() // 1_000_000,
                "deadline_at_ms": time.time_ns() // 1_000_000 + job["timeout_ms"],
                "supervisor": None,
                "child": None,
                "processes": {},
                "daemon_id": daemon,
                "container_labels": _labels(owner),
                "containers": [],
                "exit_code": None,
                "error": None,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "term_grace_ms": job["term_grace_ms"],
                "kill_grace_ms": job["kill_grace_ms"],
            }
            _write(directory, "request.json", _encoded(public))
            ready_read, ready_write = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(ready_write)
                try:
                    _monitor(root, job, ready_read, lock)
                except BaseException:
                    os._exit(1)
            os.close(ready_read)
            try:
                record["supervisor"] = _identity(pid)
                _record(directory, record)
                os.write(ready_write, b"1")
            finally:
                os.close(ready_write)
            return record
        finally:
            os.close(lock)


def status(root: Path, job_id: str) -> dict[str, Any]:
    with _directory(root / _uuid(job_id)) as directory:
        record = _decode(_read(directory, "receipt.json"))
    record["supervisor_present"] = _alive(record["supervisor"])
    known = {int(pid): ticks for pid, ticks in record["processes"].items()}
    record["observed_processes"] = {
        str(pid): ticks for pid, ticks in _members(known, record["supervisor"]).items()
    }
    if not record["supervisor_present"] and record["state"] not in TERMINAL:
        record["state"] = "recovery_required"
        record["error"] = {
            "code": "recovery_required",
            "message": "Remote supervisor identity is absent or changed",
        }
    return record


def retain(root: Path, job_id: str) -> dict[str, Any]:
    record = status(root, job_id)
    if (
        record["state"] not in {"running", "awaiting_retain", "retained"}
        or not record["supervisor_present"]
    ):
        raise WorkerError("ownership_conflict", "Remote job cannot retain services")
    with _directory(root / job_id) as directory:
        _write(
            directory, "retain.json", _encoded({"owner": record["owner"], "fence": record["fence"]})
        )
    return status(root, job_id)


def cancel(root: Path, job_id: str) -> dict[str, Any]:
    record = status(root, job_id)
    if record["state"] in TERMINAL and record["state"] != "recovery_required":
        return record
    with _directory(root / job_id) as directory:
        _write(
            directory, "cancel.json", _encoded({"owner": record["owner"], "fence": record["fence"]})
        )
        if not record["supervisor_present"]:
            record["state"] = "cancelled"
            _finish_cleanup(
                directory,
                record,
                {int(pid): ticks for pid, ticks in record["processes"].items()},
                record["term_grace_ms"],
                record["kill_grace_ms"],
            )
            # Loss of the supervisor may have hidden a process before its first receipt.
            record["state"] = "recovery_required"
            _record(directory, record)
    return status(root, job_id)


def read(
    root: Path, job_id: str, stream: str, offset: int = 0, max_bytes: int = MAX_READ
) -> dict[str, Any]:
    if stream not in {"stdout", "stderr"}:
        raise WorkerError("invalid_input", "Remote output stream is invalid")
    _integer(offset, 0, MAX_STDOUT)
    _integer(max_bytes, 1, MAX_READ)
    with _directory(root / _uuid(job_id)) as directory:
        content = _read(directory, stream + ".log", maximum=MAX_STDOUT + MAX_RECEIPT)
    if offset > len(content):
        raise WorkerError("invalid_input", "Remote output offset exceeds its size")
    data = content[offset : offset + max_bytes]
    return {
        "job_id": job_id,
        "stream": stream,
        "offset": offset,
        "data_base64": base64.b64encode(data).decode(),
        "next_offset": offset + len(data) if offset + len(data) < len(content) else None,
        "bytes": len(content),
    }


def _checkpoint(path: Path, deadline: float) -> dict[str, Any]:
    """Use checkpoint_manifest's v2 identity format, with pinned regular-file reads."""
    root = path.resolve(strict=True)
    if not root.is_dir() or not (root / "config.json").is_file():
        raise WorkerError("invalid_input", "Registered checkpoint needs config.json")
    stopped = threading.Event()

    def inspect_file(item: Path) -> dict[str, Any]:
        if stopped.is_set() or time.monotonic() >= deadline:
            raise WorkerError("stage_timeout", "Checkpoint inspection deadline expired")
        relative = item.relative_to(root)
        resolved = item.resolve(strict=True)
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise WorkerError("invalid_input", "Checkpoint contains a nonregular entry")
            digest = hashlib.sha256()
            while data := stream.read(8 * 1024 * 1024):
                if stopped.is_set() or time.monotonic() >= deadline:
                    raise WorkerError("stage_timeout", "Checkpoint inspection deadline expired")
                digest.update(data)
            after = os.fstat(stream.fileno())
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or item.resolve(strict=True) != resolved
            or resolved.stat().st_ino != after.st_ino
        ):
            raise WorkerError("stale_plan", "Checkpoint changed during inspection")
        return {"path": relative.as_posix(), "size": after.st_size, "sha256": digest.hexdigest()}

    files: list[dict[str, Any]] = []
    batch: list[Path] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            for item in sorted(root.rglob("*")):
                relative = item.relative_to(root)
                if relative == Path("README.md") or relative.parts[:2] == (
                    ".cache",
                    "huggingface",
                ):
                    continue
                if item.is_dir() and not item.is_symlink():
                    continue
                if len(files) + len(batch) >= 100_000:
                    raise WorkerError(
                        "source_truncated", "Checkpoint file manifest exceeds its bound"
                    )
                batch.append(item)
                if len(batch) == 4:
                    pending = [pool.submit(inspect_file, selected) for selected in batch]
                    files.extend(
                        future.result(timeout=max(0, deadline - time.monotonic()))
                        for future in pending
                    )
                    batch.clear()
            pending = [pool.submit(inspect_file, selected) for selected in batch]
            files.extend(
                future.result(timeout=max(0, deadline - time.monotonic())) for future in pending
            )
        except TimeoutError:
            raise WorkerError("stage_timeout", "Checkpoint inspection deadline expired") from None
        finally:
            stopped.set()
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_RECEIPT - 1024:
        raise WorkerError("source_truncated", "Checkpoint file manifest exceeds its bound")
    return {
        "manifest_version": 2,
        "excluded_paths": ["README.md", ".cache/huggingface/**"],
        "model_tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": files,
        "file_count": len(files),
        "bytes_verified": sum(row["size"] for row in files),
    }


def _image(image: Any, deadline: float) -> dict[str, Any]:
    if not isinstance(image, str) or not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", image
    ):
        raise WorkerError("invalid_input", "Remote image requires an immutable digest")
    rows = json.loads(_command(["docker", "image", "inspect", image], deadline))
    if len(rows) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", rows[0]["Id"]):
        raise WorkerError("invalid_input", "Remote image inspection is invalid")
    return {"id": rows[0]["Id"], "repo_digests": rows[0].get("RepoDigests") or []}


def _gpu_clients(gpus: list[dict[str, Any]], deadline: float) -> dict[str, Any]:
    """Observe device holders, rejecting permission gaps and bounded-scan omissions."""
    devices = {item["device"]: item["pci"] for item in gpus}
    result: dict[str, Any] = {"complete": True, "processes": []}
    if not devices:
        return result
    seen = 0
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        if time.monotonic() >= deadline:
            raise WorkerError("stage_timeout", "GPU client inspection deadline expired")
        identity = _proc(int(process.name))
        if identity is None:
            continue
        selected = set()
        try:
            for entry in (process / "fd").iterdir():
                seen += 1
                if seen > 100_000:
                    result["complete"] = False
                    return result
                try:
                    target = os.readlink(entry).removesuffix(" (deleted)")
                except FileNotFoundError:
                    continue
                if target == "/dev/kfd":
                    selected.add("all")
                elif target in devices:
                    selected.add(devices[target])
        except FileNotFoundError:
            continue
        except PermissionError:
            result["complete"] = False
            continue
        after = _proc(int(process.name))
        if selected and after is not None and after[3] == identity[3]:
            result["processes"].append(
                {"pid": int(process.name), "start_ticks": identity[3], "devices": sorted(selected)}
            )
            if len(result["processes"]) >= 4096:
                result["complete"] = False
                return result
    return result


def _http_get(parameters: dict[str, Any], path: str, deadline: float) -> bytes:
    if set(parameters) - {"url", "api_key"} or not isinstance(parameters.get("url"), str):
        raise WorkerError("invalid_input", "Engine generation probe fields are invalid")
    origin = urlsplit(parameters["url"])
    if (
        origin.scheme not in {"http", "https"}
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise WorkerError("invalid_input", "Engine probe requires a registered HTTP origin")
    headers = {}
    if parameters.get("api_key") is not None:
        secret = parameters["api_key"]
        if not isinstance(secret, str) or not secret or any(c in secret for c in "\r\n\0"):
            raise WorkerError("invalid_input", "Engine credential is invalid")
        headers["Authorization"] = "Bearer " + secret

    connection_type = (
        http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WorkerError("stage_timeout", "Engine generation probe deadline expired")
    connection = connection_type(origin.hostname, origin.port, timeout=remaining)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise WorkerError("source_unavailable", "Engine generation endpoint is unavailable")
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("stage_timeout", "Engine generation probe deadline expired")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RECEIPT + 1 - len(body)))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
            if len(body) > MAX_RECEIPT:
                raise WorkerError("source_truncated", "Engine generation endpoint is too large")
    finally:
        connection.close()


def _generation(parameters: dict[str, Any], deadline: float) -> dict[str, Any]:
    try:
        version = _decode(_http_get(parameters, "/version", deadline)).get("version")
        if not isinstance(version, str) or not version or len(version) > 256:
            raise ValueError("Invalid version")
        metrics = _http_get(parameters, "/metrics", deadline).decode()
        samples = re.findall(
            r"(?m)^process_start_time_seconds(?:\{[^\n]*\})?\s+([^\s]+)(?:\s+[^\n]+)?$",
            metrics,
        )
        values = {Decimal(value) for value in samples}
        if len(values) != 1:
            raise ValueError("Missing or inconsistent generation")
        start = next(iter(values))
        if not start.is_finite() or start <= 0:
            raise ValueError("Invalid generation")
        return {"version": version, "process_start_time_seconds": str(start)}
    except WorkerError:
        raise
    except (OSError, ValueError, InvalidOperation, http.client.HTTPException):
        raise WorkerError("source_unavailable", "Engine generation response is invalid") from None


def _log_tail(parameters: dict[str, Any]) -> dict[str, Any]:
    if set(parameters) != {"path", "max_bytes"}:
        raise WorkerError("invalid_input", "Registered log fields are invalid")
    path = _absolute(parameters["path"])
    maximum = _integer(parameters["max_bytes"], 1, MAX_READ)
    if (
        path.name == ".env"
        or path.name.startswith(".env.")
        or path.suffix.lower() in {".env", ".pem", ".key", ".p12", ".pfx"}
        or path.name
        in {"id_rsa", "id_ed25519", "authorized_keys", "credentials", "credentials.json"}
    ):
        raise WorkerError("permission_denied", "Credential files cannot be collected")
    with _directory(path.parent, private=False) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o022
                or before.st_nlink != 1
            ):
                raise WorkerError("permission_denied", "Registered log ownership or type is unsafe")
            offset = max(0, before.st_size - maximum)
            content = os.pread(fd, maximum, offset)
            after = os.fstat(fd)
        finally:
            os.close(fd)
    # A tail may begin within a request body or a UTF-8 codepoint. Drop that partial line.
    if offset:
        prefix, found, content = content.partition(b"\n")
        offset += len(prefix) + len(found)
    try:
        content.decode("utf-8")
        if b"\0" in content:
            raise UnicodeError
    except UnicodeError:
        raise WorkerError("unsupported_media_type", "Registered log is not UTF-8 text") from None
    complete = before.st_size <= after.st_size and len(content) == before.st_size - offset
    return {
        "data_base64": base64.b64encode(content).decode(),
        "offset": offset,
        "size_bytes": before.st_size,
        "complete": complete,
    }


def probe(kind: str, parameters: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Read registered inventory and complete artifacts without enrollment or installation."""
    if kind == "checkpoint":
        if set(parameters) != {"model_dir"}:
            raise WorkerError("invalid_input", "Checkpoint probe fields are invalid")
        return _checkpoint(_absolute(parameters["model_dir"]), deadline)
    if kind == "log":
        return _log_tail(parameters)
    if kind == "image" and set(parameters) == {"image"}:
        return _image(parameters["image"], deadline)
    if kind == "generation":
        return _generation(parameters, deadline)
    if kind == "router_state":
        if set(parameters) != {"url"} or urlsplit(parameters["url"]).hostname not in {
            "127.0.0.1",
            "::1",
        }:
            raise WorkerError("invalid_input", "Router state requires a registered loopback origin")
        try:
            return _decode(_http_get(parameters, "/narwhal/state", deadline))
        except WorkerError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise WorkerError("source_unavailable", "Router state response is invalid") from None
    if kind != "inventory" or set(parameters) - {"interface", "image"}:
        raise WorkerError("invalid_input", "Remote probe is unsupported")
    machine = Path("/etc/machine-id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{32}", machine):
        raise WorkerError("prerequisite_failed", "Remote machine identity is invalid")
    tools = (
        "python3",
        "git",
        "make",
        "curl",
        "docker",
        "ip",
        "rocminfo",
        "nvidia-smi",
        "iperf3",
        "ib_write_bw",
    )
    interpreter = Path(sys.executable).resolve(strict=True)
    if not stat.S_ISREG(interpreter.stat().st_mode):
        raise WorkerError("prerequisite_failed", "Remote Python executable is not a regular file")
    result: dict[str, Any] = {
        "machine_id": machine,
        "boot_id": _boot(),
        "netns": os.readlink("/proc/self/ns/net"),
        "hostname": os.uname().nodename,
        "python": list(sys.version_info[:3]),
        "python_executable": str(interpreter),
        "gpus": [],
        "interfaces": {},
        "ports": [],
        "containers": [],
        "tools": {name: shutil.which(name) for name in tools},
    }
    if Path("/dev/kfd").exists():
        rendered: dict[int, list[dict[str, Any]]] = {}
        for node in sorted(
            Path("/sys/class/kfd/kfd/topology/nodes").glob("*"), key=lambda value: int(value.name)
        ):
            props = dict(
                line.split(maxsplit=1)
                for line in (node / "properties").read_text().splitlines()
                if " " in line
            )
            if int(props.get("drm_render_minor", "0")) < 128:
                continue
            location = int(props["location_id"])
            domain = int(props.get("domain", "0"))
            rendered.setdefault(location, []).append(
                {
                    "uuid": props.get("unique_id")
                    if props.get("unique_id") not in {None, "0"}
                    else None,
                    "pci": (
                        f"{domain:04x}:{location >> 8:02x}:"
                        f"{(location >> 3) & 31:02x}.{location & 7}"
                    ),
                    "device": "/dev/dri/renderD" + props["drm_render_minor"],
                }
            )
        agents = re.split(r"(?m)^\s*Agent\s+\d+\s*$", _command(["rocminfo"], deadline).decode())
        agents = [agent for agent in agents if re.search(r"Device Type:\s+GPU", agent)]
        for index, agent in enumerate(agents):
            product = re.search(r"Marketing Name:\s*([^\n]+)", agent)
            bdf = re.search(r"BDFID:\s+(\d+)", agent)
            matches = rendered.get(int(bdf[1]), []) if bdf else []
            if product is None or len(matches) != 1:
                raise WorkerError("prerequisite_failed", "ROCm GPU and PCI identities do not match")
            result["gpus"].append({"index": index, "product": product[1].strip(), **matches[0]})
        if len(result["gpus"]) != sum(len(items) for items in rendered.values()):
            raise WorkerError("prerequisite_failed", "ROCm GPU inventory is incomplete")
        result["runtime"] = "rocm"
    elif result["tools"]["nvidia-smi"]:
        raw = _command(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,pci.bus_id,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            deadline,
        )
        for line in raw.decode().splitlines():
            index_text, uid, pci, product_name, total = [part.strip() for part in line.split(",")]
            result["gpus"].append(
                {
                    "index": int(index_text),
                    "uuid": uid,
                    "pci": pci.lower(),
                    "product": product_name,
                    "memory_total_mib": int(total),
                    "device": "/dev/nvidia" + index_text,
                }
            )
        result["runtime"] = "cuda"
    else:
        result["runtime"] = None
    result["gpu_clients"] = _gpu_clients(result["gpus"], deadline)
    command = ["ip", "-json", "address", "show"]
    if "interface" in parameters:
        interface = parameters["interface"]
        if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", interface):
            raise WorkerError("invalid_input", "Remote interface is invalid")
        command += ["dev", interface]
    if result["tools"]["ip"]:
        interfaces = json.loads(_command(command, deadline))
        result["interfaces"] = {
            row["ifname"]: [
                entry["local"]
                for entry in row.get("addr_info", [])
                if isinstance(entry.get("local"), str)
            ]
            for row in interfaces
        }
    ports = set()
    for name in ("tcp", "tcp6"):
        for line in Path("/proc/net/" + name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    result["ports"] = sorted(ports)
    if result["tools"]["docker"]:
        ids = _command(["docker", "ps", "-aq", "--no-trunc"], deadline).decode().split()
        if len(ids) > 128 or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
            raise WorkerError("source_truncated", "Remote container inventory exceeds its bound")
        if ids:
            rows = json.loads(_command(["docker", "inspect", *ids], deadline))
            result["containers"] = [
                {
                    "id": row["Id"],
                    "labels": row.get("Config", {}).get("Labels") or {},
                    "running": bool(row.get("State", {}).get("Running")),
                }
                for row in rows
            ]
    if "image" in parameters:
        result["image"] = _image(parameters["image"], deadline)
    return result


def _alarm(*_arguments: Any) -> None:
    raise WorkerError("stage_timeout", "Remote request deadline expired")


@contextlib.contextmanager
def _probe_channel(descriptor: int) -> Iterator[None]:
    """Interrupt a stateless probe when its held-open SSH input channel closes."""
    mode = os.fstat(descriptor).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        raise WorkerError("invalid_input", "Remote probe requires a live input channel")
    finished = threading.Event()
    sending = threading.Lock()
    main_thread = threading.get_ident()
    cancellation_signal = signal.SIGUSR1
    previous_handler = signal.getsignal(cancellation_signal)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {cancellation_signal})

    def interrupted(*_arguments: Any) -> None:
        if not finished.is_set():
            raise WorkerError("stage_cancelled", "Remote probe caller disconnected")

    def observe() -> None:
        try:
            with selectors.DefaultSelector() as selected:
                selected.register(descriptor, selectors.EVENT_READ)
                while not finished.is_set():
                    if not selected.select(0.05):
                        continue
                    # No more request bytes are valid after the bounded JSON line.
                    # EOF or extra input ends the stateless request's lifetime.
                    os.read(descriptor, 1)
                    break
        except (OSError, ValueError):
            pass
        with sending:
            if not finished.is_set():
                signal.pthread_kill(main_thread, cancellation_signal)

    signal.signal(cancellation_signal, interrupted)
    watcher = threading.Thread(target=observe, daemon=True, name="ssh-probe-channel")
    try:
        watcher.start()
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {cancellation_signal})
        yield
    finally:
        finished.set()
        signal.pthread_sigmask(signal.SIG_BLOCK, {cancellation_signal})
        with sending:
            pass
        if watcher.ident is not None:
            watcher.join(timeout=1)
        while signal.sigtimedwait({cancellation_signal}, 0) is not None:
            pass
        signal.signal(cancellation_signal, previous_handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if watcher.is_alive():
            raise WorkerError("stage_timeout", "Remote probe channel observer did not stop")


def main() -> int:
    try:
        raw = sys.stdin.buffer.readline(MAX_REQUEST + 2)
        framed = raw.endswith(b"\n")
        if framed:
            raw = raw[:-1]
        if len(raw) > MAX_REQUEST:
            raise WorkerError("invalid_input", "Remote request exceeds its bound")
        request = _decode(raw)
        command = request.get("command")
        seconds = _integer(request.get("timeout_ms", 30_000), 1, 86_400_000) / 1000
        deadline = time.monotonic() + seconds
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        if request.get("op") in {"put", "read_file"}:
            handler = globals().get("file_dispatch")
            if handler is None:
                raise WorkerError("prerequisite_failed", "Remote file helper is unavailable")
            result = handler(request)
        elif command == "probe":
            if not framed:
                raise WorkerError("invalid_input", "Remote probe requires a live input channel")
            with _probe_channel(sys.stdin.fileno()):
                result = probe(request["kind"], request["parameters"], deadline)
        else:
            root = _absolute(request["root"])
            if command == "submit":
                result = submit(root, request["job"])
            elif command == "status":
                result = status(root, request["job_id"])
            elif command == "cancel":
                result = cancel(root, request["job_id"])
            elif command == "retain":
                result = retain(root, request["job_id"])
            elif command == "read":
                result = read(
                    root,
                    request["job_id"],
                    request["stream"],
                    request.get("offset", 0),
                    request.get("max_bytes", MAX_READ),
                )
            elif command in {"inspect_containers", "stop_containers"}:
                record = status(root, request["job_id"])
                result = {
                    "containers": (
                        _stop_containers if command == "stop_containers" else _containers
                    )(record, deadline)
                }
            else:
                raise WorkerError("invalid_input", "Remote command is unsupported")
        reply = {"ok": True, "data": result}
    except WorkerError as error:
        reply = {"ok": False, "error": {"code": error.code, "message": error.message}}
    except FileNotFoundError:
        reply = {
            "ok": False,
            "error": {"code": "input_missing", "message": "Remote registered input is missing"},
        }
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        reply = {
            "ok": False,
            "error": {
                "code": "invalid_input",
                "message": "Remote request or recorded input is invalid",
            },
        }
    try:
        content = _encoded(reply)
    except WorkerError as error:
        content = _encoded({"ok": False, "error": {"code": error.code, "message": error.message}})
    signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout.buffer.write(content + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
