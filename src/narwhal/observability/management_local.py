"""Collect registered local development evidence without remote or deployment effects."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import shutil
import signal
import stat
from pathlib import Path
from time import monotonic
from typing import Literal

from narwhal.deployment.management_access import (
    AccessError,
    InspectionAccess,
    directory,
    now,
)
from narwhal.deployment.management_registry import ManagementTarget

from .management_http import endpoint
from .management_targets import build_targets
from .management_types import MonitoringBinding, SourceCapture


def _log(target: ManagementTarget, log_id: str, maximum: int) -> SourceCapture:
    selected = next((item for item in target.logs if item.id == log_id), None)
    if selected is None or selected.host_id != "local":
        raise AccessError("invalid_input", "Log is not registered on this host")
    path = selected.source
    if (
        path.name == ".env"
        or path.name.startswith(".env.")
        or path.suffix.lower() in {".env", ".pem", ".key", ".p12", ".pfx"}
        or path.name
        in {"id_rsa", "id_ed25519", "authorized_keys", "credentials", "credentials.json"}
    ):
        raise AccessError("permission_denied", "Credential files cannot be collected")
    observed = now()
    with directory(path.parent) as parent:
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
            )
        except FileNotFoundError:
            raise
        except OSError:
            raise AccessError(
                "permission_denied", "Registered log cannot be opened safely"
            ) from None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o022
                or before.st_nlink != 1
            ):
                raise AccessError("permission_denied", "Registered log ownership or mode is unsafe")
            offset = max(0, before.st_size - maximum)
            content = os.pread(descriptor, maximum, offset)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    complete = after.st_size >= before.st_size and len(content) == before.st_size - offset
    if offset:
        while content and content[0] & 0xC0 == 0x80:
            content = content[1:]
            offset += 1
    try:
        content.decode("utf-8")
        if b"\0" in content:
            raise UnicodeError
    except UnicodeError:
        raise AccessError("unsupported_media_type", "Selected log is not UTF-8 text") from None
    return SourceCapture(
        log_id,
        content,
        observed,
        complete=complete,
        status="ok" if complete else "truncated",
        error_code=None if complete else "source_truncated",
        provenance={"offset": offset, "size_bytes": before.st_size},
    )


async def _utility(name: str, arguments: list[str], deadline: float, maximum: int) -> SourceCapture:
    observed = now()
    executable = shutil.which(name, path=os.defpath)
    if executable is None:
        return SourceCapture(
            name, b"", observed, complete=False, status="unavailable", error_code="utility_missing"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            start_new_session=True,
        )
    except OSError:
        return SourceCapture(
            name, b"", observed, complete=False, status="unavailable", error_code="utility_failed"
        )
    content = bytearray()
    status, code = "ok", None
    try:
        async with asyncio.timeout(min(5, max(0, deadline - monotonic()))):
            if process.stdout is None:
                raise OSError("Utility output is unavailable")
            while block := await process.stdout.read(min(65_536, maximum - len(content) + 1)):
                available = maximum - len(content)
                content.extend(block[:available])
                if len(block) > available:
                    status, code = "truncated", "source_truncated"
                    break
            if status == "ok" and await process.wait() != 0:
                status, code = "error", "utility_failed"
    except (TimeoutError, OSError):
        status, code = "timeout", "source_unavailable"
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(1):
                await process.wait()
    return SourceCapture(
        name, bytes(content), observed, complete=status == "ok", status=status, error_code=code
    )


class LocalSiteProvider:
    """Implement only the registered local host; fleet transport is injected separately."""

    def __init__(self, access: InspectionAccess) -> None:
        self.access = access

    async def monitoring_binding(
        self, target: ManagementTarget, *, deadline: float
    ) -> MonitoringBinding:
        """Read the dev instance's fleet and registered monitoring origins."""
        if target.kind != "dev":
            raise AccessError("adapter_unavailable", "Target requires its registered site adapter")
        if monotonic() >= deadline:
            raise AccessError("source_unavailable", "Monitoring binding deadline expired")
        _, fleet = self.access.fleet(target)
        return MonitoringBinding(
            build_targets(fleet, endpoint(target, "router")),
            endpoint(target, "prometheus"),
            "local",
        )

    async def collect(
        self,
        target: ManagementTarget,
        kind: Literal["inventory", "log"],
        subject_id: str,
        *,
        deadline: float,
        max_bytes: int,
    ) -> tuple[SourceCapture, ...]:
        """Capture fixed local inventory utilities or one registered regular log tail."""
        if target.kind != "dev":
            raise AccessError("adapter_unavailable", "Target requires its registered site adapter")
        with directory(target.working_directory):
            pass
        if monotonic() >= deadline:
            raise AccessError("source_unavailable", "Host collection deadline expired")
        if kind == "log":
            try:
                return (_log(target, subject_id, max_bytes),)
            except FileNotFoundError:
                return (
                    SourceCapture(subject_id, b"", now(), False, "unavailable", "source_missing"),
                )
        if subject_id != "local":
            raise AccessError("invalid_input", "Host is not registered")
        basic = json.dumps(
            {
                "system": platform.system(),
                "release": platform.release(),
                "architecture": platform.machine(),
                "cpu_count": os.cpu_count(),
            }
        ).encode()
        captures = [SourceCapture("system", basic, now())]
        remaining = max_bytes - len(basic)
        utilities = [("ip", ["-json", "address"])]
        utilities.append(
            ("rocminfo", [])
            if Path("/dev/kfd").exists()
            else ("nvidia-smi", ["--query-gpu=uuid,name,memory.total", "--format=csv,noheader"])
        )
        for name, arguments in utilities:
            if remaining <= 0 or monotonic() >= deadline:
                captures.append(
                    SourceCapture(name, b"", now(), False, "unavailable", "collection_limit")
                )
                continue
            capture = await _utility(name, arguments, deadline, remaining)
            captures.append(capture)
            remaining -= len(capture.content)
        return tuple(captures)
