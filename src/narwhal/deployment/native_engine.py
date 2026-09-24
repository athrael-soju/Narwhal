"""Run checked vLLM plans as owned Linux processes on a shared GPU."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import socket
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx

from narwhal.engines.attestation import fetch_engine_identity

from .launch_engine import digest, gpu_memory, validate_shared_runs, write_private


def process_identity(pid: int) -> dict[str, int | str]:
    """Bind a PID to its Linux boot and kernel start tick to reject PID reuse."""
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat.rsplit(")", 1)[1].split()
    if len(fields) < 20 or fields[0] in {"Z", "X"} or os.getpgid(pid) != pid:
        raise ValueError(f"process {pid} is not an owned process group leader")
    return {"pid": pid, "boot_id": boot_id, "start_ticks": int(fields[19])}


def _owns_process(identity: dict) -> bool:
    try:
        return process_identity(identity["pid"]) == identity
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return False


def _checked_plan(run: Path, plan: dict) -> dict:
    if plan.get("backend") != "native":
        raise ValueError("native start requires a native launch plan")
    checked = json.loads((run / "checked.json").read_text())
    if (
        checked.get("backend") != "native"
        or checked.get("plan_sha256") != digest(run / "launch.json")
        or checked.get("python_executable") != plan["python_executable"]
    ):
        raise ValueError("native runtime check differs from the launch plan")
    if digest(Path(plan["model_config_path"])) != plan["model_config_sha256"]:
        raise ValueError("model config changed after launch preparation")
    model = Path(plan["model_path"])
    if not model.exists() or (plan.get("model_sha256") and digest(model) != plan["model_sha256"]):
        raise ValueError("model file changed after launch preparation")
    if digest(run / "hook/sitecustomize.py") != plan["cache_capture_sha256"]:
        raise ValueError("cache capture hook changed after launch preparation")
    if digest(run / "hook/launch_engine.py") != plan["launcher_sha256"]:
        raise ValueError("cache capture launcher changed after launch preparation")
    if digest(run / "hook/launch.json") != digest(run / "launch.json"):
        raise ValueError("cache capture plan changed after launch preparation")
    return checked


def _environment(run: Path) -> dict[str, str]:
    values = dict(line.split("=", 1) for line in (run / "engine.env").read_text().splitlines())
    return {
        **os.environ,
        **values,
        "NARWHAL_CAPTURE_CACHE": "1",
        "NARWHAL_CACHE_PLAN": str(run / "hook/launch.json"),
        "NARWHAL_CACHE_OUTPUT": str(run / "cache-layout.json"),
    }


def _ports_free(selected: list[tuple[Path, dict]]) -> None:
    for _, plan in selected:
        for port in (
            urlsplit(plan["endpoint"]).port,
            plan["attestation_port"],
            plan["side_channel_port"],
        ):
            for family, address in (
                (socket.AF_INET, "0.0.0.0"),
                (socket.AF_INET6, "::"),
            ):
                try:
                    with socket.socket(family, socket.SOCK_STREAM) as listener:
                        if family == socket.AF_INET6:
                            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        listener.bind((address, port))
                except OSError as error:
                    if error.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
                        continue
                    raise ValueError(
                        f"{plan['role']}: port {port} is unavailable: {error}"
                    ) from error


def _wait_ready(run: Path, plan: dict, identity: dict, seconds: int) -> None:
    url = plan["endpoint"].rstrip("/") + "/health"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _owns_process(identity):
            log = (run / "startup.log").read_text(errors="replace")[-2000:]
            raise ValueError(f"{plan['role']}: native engine exited before readiness: {log}")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise ValueError(f"{plan['role']}: health endpoint did not respond within {seconds}s")


def _terminate(identity: dict, grace_seconds: int = 10) -> None:
    if not _owns_process(identity):
        raise ValueError("native engine PID or start time changed; refusing to signal")
    os.killpg(identity["pid"], signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while _owns_process(identity) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _owns_process(identity):
        os.killpg(identity["pid"], signal.SIGKILL)


def stop(run: Path) -> None:
    """Stop only the process group recorded for this native launch directory."""
    identity = json.loads((run / "native-process.json").read_text())
    _terminate(identity)
    write_private(
        run / "native-stop.json",
        json.dumps({"identity": identity, "stopped_at": time.time()}, indent=2) + "\n",
    )


def start_shared(runs: list[Path], ready_seconds: int = 180) -> None:
    """Launch checked engines sequentially against live shared GPU headroom."""
    if ready_seconds < 1:
        raise ValueError("ready_seconds must be positive")
    selected = validate_shared_runs(runs, backend="native")
    _ports_free(selected)
    gpu_uuid = selected[0][1]["shared_device"]["gpu_uuid"]
    started: list[Path] = []
    try:
        for run, plan in selected:
            _checked_plan(run, plan)
            before = gpu_memory(gpu_uuid)
            budget = (
                Decimal(str(plan["shared_device"]["gpu_memory_utilization"])) * before["total_mib"]
            )
            record = {
                "role": plan["role"],
                "backend": "native",
                "shared_device": plan["shared_device"],
                "plan_sha256": digest(run / "launch.json"),
                "gpu_before": before,
                "budget_mib": float(budget),
                "vllm_args": plan["args"],
                "expected_packages": plan["expected_packages"],
                "model_sha256": plan.get("model_sha256", plan["model_config_sha256"]),
            }
            identity = None
            process = None
            try:
                if Decimal(before["total_mib"] - before["used_mib"]) < budget:
                    raise ValueError(f"free GPU memory is below the {budget} MiB allocation")
                if (run / "native-process.json").exists():
                    raise ValueError("native launch already has a process record")
                log_fd = os.open(run / "startup.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(log_fd, "w") as log:
                    process = subprocess.Popen(
                        [plan["python_executable"], *plan["args"]],
                        env=_environment(run),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                identity = process_identity(process.pid)
                write_private(run / "native-process.json", json.dumps(identity, indent=2) + "\n")
                _wait_ready(run, plan, identity, ready_seconds)
                key = _environment(run).get("VLLM_API_KEY", "")
                headers = {"Authorization": f"Bearer {key}"} if key else None
                live = asyncio.run(fetch_engine_identity(plan["endpoint"], headers=headers))
                if live.vllm_version != _checked_plan(run, plan)["vllm_api_version"]:
                    raise ValueError("live vLLM version differs from the checked native runtime")
                record.update(
                    status="running",
                    process=identity,
                    vllm_version=live.vllm_version,
                    process_start_time_seconds=live.process_start_time_seconds,
                    model_revision=plan["model_revision"],
                    gpu_after=gpu_memory(gpu_uuid),
                )
                started.append(run)
            except (OSError, ValueError, subprocess.SubprocessError, httpx.HTTPError) as error:
                record.update(status="failed", error=str(error))
                try:
                    if identity is not None and _owns_process(identity):
                        _terminate(identity)
                    elif process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                except (OSError, ValueError, subprocess.SubprocessError) as cleanup_error:
                    record["cleanup_error"] = str(cleanup_error)
                try:
                    record["gpu_after"] = gpu_memory(gpu_uuid)
                except (OSError, ValueError, subprocess.SubprocessError) as inspection_error:
                    record["gpu_after_error"] = str(inspection_error)
                write_private(run / "shared-start.json", json.dumps(record, indent=2) + "\n")
                message = f"{plan['role']}: native launch failed: {error}"
                if record.get("cleanup_error"):
                    message += f"; cleanup failed: {record['cleanup_error']}"
                raise ValueError(message) from error
            write_private(run / "shared-start.json", json.dumps(record, indent=2) + "\n")
    except (OSError, ValueError, subprocess.SubprocessError, httpx.HTTPError) as error:
        cleanup_errors = []
        for run in reversed(started):
            try:
                stop(run)
            except (OSError, ValueError) as cleanup_error:
                cleanup_errors.append(f"{run}: {cleanup_error}")
        if cleanup_errors:
            raise ValueError(
                f"{error}; native engine cleanup failed: {'; '.join(cleanup_errors)}"
            ) from error
        raise
