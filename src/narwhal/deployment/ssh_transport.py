"""Exchange bounded private requests with the fixed installed SSH worker."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import ssh_worker
from .management_access import open_input
from .management_executor import StageContext
from .management_records import OperationError
from .ssh_settings import SSHHost, SSHSettings

MAX_EXCHANGE = 2 * 1024 * 1024
READ_ONLY = {"probe", "status", "read", "read_file", "inspect_containers"}


def _stop(process: subprocess.Popen[bytes], deadline: float) -> None:
    if process.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            process.wait(timeout=max(0.001, min(0.2, deadline - time.monotonic())))
            return
        except subprocess.TimeoutExpired:
            pass
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.1)


def _exchange(
    document: dict[str, Any], deadline: float, *, cancelled: threading.Event | None = None
) -> bytes:
    executable = shutil.which("ssh", path=os.defpath)
    if executable is None:
        raise OperationError("prerequisite_failed", "OpenSSH client is unavailable")
    known_fd = document["known_hosts_fd"]
    argv = [
        executable,
        "-F",
        os.devnull,
        "-T",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"UserKnownHostsFile=/proc/{os.getpid()}/fd/{known_fd}",
        "-o",
        f"ConnectTimeout={document['connect_timeout_s']}",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
        "-o",
        "ClearAllForwardings=yes",
    ]
    passed = [known_fd]
    request = json.dumps(document["request"], separators=(",", ":"), allow_nan=False).encode()
    if len(request) > ssh_worker.MAX_REQUEST:
        raise OperationError("invalid_input", "SSH request exceeds its byte limit")
    with tempfile.TemporaryFile() as password:
        if document.get("password") is not None:
            sshpass = shutil.which("sshpass", path=os.defpath)
            if sshpass is None:
                raise OperationError("prerequisite_failed", "Password SSH requires sshpass")
            password.write(document["password"].encode() + b"\n")
            password.seek(0)
            passed.append(password.fileno())
            argv = [
                sshpass,
                "-d",
                str(password.fileno()),
                *argv,
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        else:
            argv += ["-o", "BatchMode=yes"]
        argv += [document["destination"], shlex.join(["python3", "-c", document["worker_source"]])]
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in {"HOME", "USER", "LOGNAME", "SSH_AUTH_SOCK", "LANG"}
            or name.startswith("LC_")
        }
        environment["PATH"] = os.defpath
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            pass_fds=tuple(passed),
            start_new_session=True,
        )
        output = bytearray()
        stderr_size = 0
        sent = 0
        action_end = deadline - min(0.4, max(0, deadline - time.monotonic()) / 10)
        assert (
            process.stdin is not None and process.stdout is not None and process.stderr is not None
        )
        with selectors.DefaultSelector() as selected:
            for stream, event in (
                (process.stdin, selectors.EVENT_WRITE),
                (process.stdout, selectors.EVENT_READ),
                (process.stderr, selectors.EVENT_READ),
            ):
                os.set_blocking(stream.fileno(), False)
                selected.register(stream, event)
            try:
                while selected.get_map():
                    if cancelled is not None and cancelled.is_set():
                        raise OperationError("cancelled", "SSH inspection was cancelled")
                    remaining = action_end - time.monotonic()
                    if remaining <= 0:
                        raise OperationError("stage_timeout", "SSH exchange deadline expired")
                    for key, _ in selected.select(min(remaining, 0.05)):
                        ready_stream = key.fileobj
                        if ready_stream is process.stdin:
                            try:
                                sent += os.write(key.fd, request[sent : sent + 65536])
                            except BrokenPipeError:
                                selected.unregister(ready_stream)
                                process.stdin.close()
                                continue
                            if sent == len(request):
                                selected.unregister(ready_stream)
                                process.stdin.close()
                            continue
                        data = os.read(key.fd, 65536)
                        if not data:
                            selected.unregister(ready_stream)
                        elif ready_stream is process.stdout:
                            output.extend(data)
                        else:
                            stderr_size += len(data)
                        if len(output) > MAX_EXCHANGE or stderr_size > ssh_worker.MAX_STDERR:
                            raise OperationError(
                                "source_truncated", "SSH exchange output exceeds its limit"
                            )
                code = process.wait(timeout=max(0.001, action_end - time.monotonic()))
                if code:
                    raise OperationError(
                        "source_unavailable",
                        "SSH connection or remote worker failed; remote effects remain unknown",
                    )
                return bytes(output)
            except subprocess.TimeoutExpired:
                raise OperationError("stage_timeout", "SSH exchange deadline expired") from None
            finally:
                _stop(process, deadline)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


def _worker_source() -> str:
    worker = Path(ssh_worker.__file__).read_text()
    files = Path(__file__).with_name("ssh_files.py")
    if files.is_file():
        # The optional file helper shares the verified package identity. Its own
        # namespace prevents bootstrap definitions from replacing worker helpers.
        source = files.read_text()
        prefix = (
            "_files_namespace={'__name__':'narwhal_ssh_files'}\nexec("
            + repr(source)
            + ",_files_namespace)\nfile_dispatch=_files_namespace['file_dispatch']\n"
        )
        # Future imports must remain first within the worker compilation unit.
        return prefix + "exec(" + repr(worker) + ")\n"
    return worker


async def inspect_rpc(
    settings: SSHSettings,
    host: SSHHost,
    environment: dict[str, str],
    request: dict[str, Any],
    *,
    deadline: float,
) -> dict[str, Any]:
    """Run one read-only exchange and reap its local SSH process on cancellation."""
    if request.get("command", request.get("op")) not in READ_ONLY:
        raise OperationError("permission_denied", "Inspection cannot perform remote effects")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OperationError("stage_timeout", "SSH inspection deadline expired")
    destination = environment.get(host.ssh_env)
    password = environment.get(host.password_env) if host.password_env else None
    if (
        not destination
        or destination.startswith("-")
        or any(c.isspace() or ord(c) < 32 for c in destination)
        or (host.password_env and not password)
    ):
        raise OperationError("invalid_input", "SSH access reference is unavailable")
    stop = threading.Event()
    with open_input(Path(settings.known_hosts_path), maximum=ssh_worker.MAX_REQUEST) as (fd, trust):
        if not trust.strip():
            raise OperationError("prerequisite_failed", "SSH host trust store is empty")
        document = {
            "request": {
                "root": settings.remote_root,
                **request,
                "timeout_ms": max(1, int(remaining * 1000)),
            },
            "destination": destination,
            "password": password,
            "known_hosts_fd": fd,
            "connect_timeout_s": min(settings.connect_timeout_s, max(1, int(remaining))),
            "worker_source": _worker_source(),
        }
        task = asyncio.create_task(asyncio.to_thread(_exchange, document, deadline, cancelled=stop))
        try:
            raw = await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            with contextlib.suppress(OperationError, OSError):
                await asyncio.shield(task)
            raise
    try:
        value = ssh_worker._decode(raw)
        if value.get("ok") is False and isinstance(value.get("error"), dict):
            code = value["error"].get("code")
            if isinstance(code, str) and re_code(code):
                raise OperationError(code, "Registered SSH inspection failed")
        if value.get("ok") is not True or not isinstance(value.get("data"), dict):
            raise ValueError("Invalid response")
        return value["data"]
    except OperationError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError):
        raise OperationError("invalid_input", "Remote inspection response is invalid") from None


class SSHTransport:
    """Resolve registered aliases and never infer absence from a failed connection."""

    def __init__(
        self,
        context: StageContext,
        settings: SSHSettings,
        hosts: tuple[SSHHost, ...],
        environment: dict[str, str],
    ):
        self.context = context
        self.settings = settings
        self.hosts = {host.id: host for host in hosts}
        self.environment = environment

    def rpc(
        self, host_id: str, request: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Exchange one bounded request with a registered host."""
        host = self.hosts.get(host_id)
        if host is None:
            raise OperationError("invalid_input", "SSH host alias is not registered")
        if self.context.read_only and request.get("command", request.get("op")) not in READ_ONLY:
            raise OperationError(
                "permission_denied", "Reconciliation cannot perform remote effects"
            )
        if not self.context.read_only:
            self.context.assert_current()
        remaining = min(self.context.deadline, self.context.operation_deadline) - time.monotonic()
        if timeout_s is not None:
            remaining = min(remaining, timeout_s)
        if remaining <= 0:
            raise OperationError("stage_timeout", "SSH exchange deadline expired")
        deadline = time.monotonic() + remaining
        destination = self.environment.get(host.ssh_env)
        password = self.environment.get(host.password_env) if host.password_env else None
        if (
            not destination
            or destination.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in destination)
            or (host.password_env and not password)
        ):
            raise OperationError("invalid_input", "SSH access reference is unavailable")
        selected = {
            "root": self.settings.remote_root,
            **request,
            "timeout_ms": max(1, int(remaining * 1000)),
        }
        with open_input(Path(self.settings.known_hosts_path), maximum=ssh_worker.MAX_REQUEST) as (
            known_fd,
            trust,
        ):
            if not trust.strip():
                raise OperationError("prerequisite_failed", "SSH host trust store is empty")
            document = {
                "request": selected,
                "destination": destination,
                "password": password,
                "known_hosts_fd": known_fd,
                "connect_timeout_s": min(self.settings.connect_timeout_s, max(1, int(remaining))),
                "worker_source": _worker_source(),
                "deadline": deadline,
            }
            if self.context.read_only:
                raw = _exchange(document, deadline)
            else:
                data = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
                if len(data) > 2 * MAX_EXCHANGE:
                    raise OperationError(
                        "invalid_input", "SSH helper request exceeds its byte limit"
                    )
                descriptor = os.memfd_create(
                    "narwhal-ssh-request", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
                )
                try:
                    os.write(descriptor, data)
                    fcntl.fcntl(
                        descriptor,
                        ssh_worker._F_ADD_SEALS,
                        ssh_worker._SEALS,
                    )
                    completed = self.context.run_command(
                        [
                            sys.executable,
                            "-m",
                            "narwhal.deployment.ssh_transport",
                            "--request-fd",
                            str(descriptor),
                        ],
                        pass_fds=(descriptor, known_fd),
                        helper_slot="ssh-" + host_id,
                    )
                    if completed.returncode:
                        raise OperationError(
                            "source_unavailable",
                            "SSH exchange failed; remote effects remain unknown",
                        )
                    raw = completed.stdout.encode()
                finally:
                    os.close(descriptor)
        try:
            result = ssh_worker._decode(raw)
            if result.get("ok") is False:
                error = result["error"]
                if not isinstance(error.get("code"), str) or not re_code(error["code"]):
                    raise ValueError("Invalid error")
                raise OperationError(
                    error["code"],
                    "Remote SSH operation failed; inspect retained operation evidence",
                )
            if result.get("ok") is not True or not isinstance(result.get("data"), dict):
                raise ValueError("Invalid response")
            return result["data"]
        except OperationError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError):
            raise OperationError("invalid_input", "Remote worker response is invalid") from None

    def _owned_exchange(
        self, host_id: str, job_id: str, *, deadline: float, cancel: bool
    ) -> dict[str, Any]:
        record = self.context.read()
        owner = {
            "operation_id": self.context.operation_id,
            "stage_id": self.context.stage_id,
            "launch_token": job_id,
        }
        worker = record.get("worker") or {}
        effects = [
            effect
            for stage in record["stages"]
            if stage["stage_id"] == self.context.stage_id
            for effect in stage["effects"]
        ]
        if (
            self.context.read_only
            or worker.get("fence") != self.context.fence
            or record["state"] not in {"running", "cancelling"}
            or not any(
                effect.get("host_id") == host_id and effect.get("owner") == owner
                for effect in effects
            )
        ):
            raise OperationError("permission_denied", "Remote cleanup has no current owned intent")
        deadline = min(deadline, self.context.hard_deadline)
        host = self.hosts.get(host_id)
        if host is None or time.monotonic() >= deadline:
            raise OperationError("stage_timeout", "Remote cleanup deadline expired")
        destination = self.environment.get(host.ssh_env)
        password = self.environment.get(host.password_env) if host.password_env else None
        if (
            not destination
            or destination.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in destination)
            or (host.password_env and not password)
        ):
            raise OperationError("invalid_input", "SSH cleanup access reference is unavailable")
        with open_input(Path(self.settings.known_hosts_path), maximum=ssh_worker.MAX_REQUEST) as (
            known_fd,
            trust,
        ):
            if not trust.strip():
                raise OperationError("prerequisite_failed", "SSH host trust store is empty")
            document = {
                "destination": destination,
                "password": password,
                "known_hosts_fd": known_fd,
                "connect_timeout_s": min(
                    self.settings.connect_timeout_s, max(1, int(deadline - time.monotonic()))
                ),
                "worker_source": _worker_source(),
            }

            def call(command: str) -> dict[str, Any]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OperationError("stage_timeout", "Remote cleanup deadline expired")
                request = {
                    "root": self.settings.remote_root,
                    "command": command,
                    "job_id": job_id,
                    "timeout_ms": max(1, int(remaining * 1000)),
                }
                try:
                    response = ssh_worker._decode(
                        _exchange({**document, "request": request}, deadline)
                    )
                    value = response.get("data")
                    if response.get("ok") is not True or not isinstance(value, dict):
                        raise ValueError("Unavailable remote receipt")
                    if (
                        value.get("owner") != owner
                        or value.get("fence") != self.context.fence
                        or value.get("job_id") != job_id
                    ):
                        raise OperationError("ownership_conflict", "Remote cleanup owner differs")
                    return value
                except OperationError:
                    raise
                except (OSError, ValueError, TypeError):
                    raise OperationError(
                        "recovery_required", "Remote ownership remains unknown"
                    ) from None

            observed = call("status")
            return call("cancel") if cancel else observed

    def cancel_owned(self, host_id: str, job_id: str, *, deadline: float) -> dict[str, Any]:
        """Cancel an already recorded job after action cancellation or timeout."""
        return self._owned_exchange(host_id, job_id, deadline=deadline, cancel=True)

    def status_owned(self, host_id: str, job_id: str, *, deadline: float) -> dict[str, Any]:
        """Inspect cleanup without admitting a new action after cancellation."""
        return self._owned_exchange(host_id, job_id, deadline=deadline, cancel=False)

    def probe(self, host_id: str, kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Inspect a fixed registered resource without changing remote state."""
        return self.rpc(host_id, {"command": "probe", "kind": kind, "parameters": parameters})

    def submit(self, host_id: str, **job: Any) -> dict[str, Any]:
        """Admit a finite remote job and return its durable receipt."""
        return self.rpc(
            host_id, {"command": "submit", "job": job}, timeout_s=self.settings.command_timeout_s
        )

    def status(self, host_id: str, job_id: str) -> dict[str, Any]:
        """Observe a recorded job without changing its resources."""
        return self.rpc(
            host_id,
            {"command": "status", "job_id": job_id},
            timeout_s=self.settings.command_timeout_s,
        )

    def retain(self, host_id: str, job_id: str) -> dict[str, Any]:
        """Acknowledge completed startup and retain the owned services."""
        return self.rpc(
            host_id,
            {"command": "retain", "job_id": job_id},
            timeout_s=self.settings.command_timeout_s,
        )

    def cancel(self, host_id: str, job_id: str) -> dict[str, Any]:
        """Request cleanup of the selected recorded remote job."""
        return self.rpc(
            host_id,
            {"command": "cancel", "job_id": job_id},
            timeout_s=self.settings.command_timeout_s,
        )

    def read(
        self,
        host_id: str,
        job_id: str,
        stream: str,
        offset: int = 0,
        max_bytes: int = ssh_worker.MAX_READ,
    ) -> dict[str, Any]:
        """Read a bounded chunk of retained redacted command output."""
        return self.rpc(
            host_id,
            {
                "command": "read",
                "job_id": job_id,
                "stream": stream,
                "offset": offset,
                "max_bytes": max_bytes,
            },
            timeout_s=self.settings.command_timeout_s,
        )

    def inspect_containers(self, host_id: str, job_id: str) -> dict[str, Any]:
        """Inspect exact daemon and management container ownership."""
        return self.rpc(
            host_id,
            {"command": "inspect_containers", "job_id": job_id},
            timeout_s=self.settings.command_timeout_s,
        )

    def stop_containers(self, host_id: str, job_id: str) -> dict[str, Any]:
        """Remove only containers whose daemon and owner still match."""
        return self.rpc(
            host_id,
            {"command": "stop_containers", "job_id": job_id},
            timeout_s=self.settings.command_timeout_s,
        )


def re_code(value: str) -> bool:
    return (
        bool(value) and len(value) <= 64 and all(c in "abcdefghijklmnopqrstuvwxyz_" for c in value)
    )


def main() -> int:
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--request-fd":
            raise OperationError("invalid_input", "SSH helper arguments are invalid")
        raw = os.pread(int(sys.argv[2]), 2 * MAX_EXCHANGE + 1, 0)
        if len(raw) > 2 * MAX_EXCHANGE:
            raise OperationError("invalid_input", "SSH helper request exceeds its limit")
        request = ssh_worker._decode(raw)
        result = _exchange(request, request["deadline"])
    except OperationError as error:
        result = json.dumps(
            {"ok": False, "error": {"code": error.code, "message": error.message}}
        ).encode()
    except (OSError, ValueError, KeyError, TypeError):
        result = (
            b'{"ok":false,"error":{"code":"source_unavailable","message":"SSH exchange failed"}}'
        )
    sys.stdout.buffer.write(result + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
