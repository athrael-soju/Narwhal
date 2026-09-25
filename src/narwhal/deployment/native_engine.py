"""Run checked vLLM plans as owned Linux processes on a shared GPU."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx

from narwhal.engines.attestation import fetch_engine_identity
from narwhal.runtime.listeners import check_engine_bind, check_http_bind

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


def _process_stat(pid: int) -> tuple[str, int, int, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[2]), int(fields[3]), int(fields[19])


def _group_members(identity: dict) -> dict[int, int]:
    """Find live group members; an absent leader leaves their ownership unresolved."""
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != identity["boot_id"]:
        return {}
    leader = identity["pid"]
    try:
        _, group, session, started = _process_stat(leader)
    except (FileNotFoundError, ProcessLookupError):
        pass
    else:
        if (group, session, started) != (leader, leader, identity["start_ticks"]):
            return {}
    members = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        pid = int(path.name)
        try:
            state, group, session, started = _process_stat(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
        if state not in {"Z", "X"} and group == session == leader:
            members[pid] = started
    return members


def _signal_members(members: dict[int, int], sig: signal.Signals) -> None:
    for pid, started in members.items():
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            # A pidfd targets this process even if its numeric PID is reused later.
            if _process_stat(pid)[3] == started:
                signal.pidfd_send_signal(descriptor, sig)
        except (FileNotFoundError, ProcessLookupError):
            pass
        finally:
            os.close(descriptor)


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
        "VLLM_API_KEY": "",
        **values,
        "NARWHAL_CAPTURE_CACHE": "1",
        "NARWHAL_CACHE_PLAN": str(run / "hook/launch.json"),
        "NARWHAL_CACHE_OUTPUT": str(run / "cache-layout.json"),
    }


def _ports_free(selected: list[tuple[Path, dict]]) -> None:
    for run, plan in selected:
        endpoint = urlsplit(plan["endpoint"])
        host, port = endpoint.hostname, endpoint.port
        assert host is not None and port is not None
        try:
            check_engine_bind(host, port)
        except OSError as error:
            raise ValueError(
                f"{plan['role']}: engine port {port} is unavailable at {host}: {error}"
            ) from error
        values = _environment(run)
        host = values["VLLM_NIXL_SIDE_CHANNEL_HOST"]
        port = int(values["VLLM_NIXL_SIDE_CHANNEL_PORT"])
        try:
            check_engine_bind(host, port, nixl=True)
        except OSError as error:
            raise ValueError(
                f"{plan['role']}: NIXL port {port} is unavailable at {host}: {error}"
            ) from error
        node = plan["role"].removeprefix("engine-")
        attestation = os.environ.get(
            f"NARWHAL_NODE_{node}_ATTESTATION_URL", plan.get("attestation_url", "")
        )
        if attestation:
            endpoint = urlsplit(attestation)
            host, port = endpoint.hostname, endpoint.port
            if endpoint.scheme != "http" or not host or not port:
                raise ValueError(f"{plan['role']}: invalid attestation URL {attestation!r}")
            try:
                check_http_bind(host, port)
            except OSError as error:
                raise ValueError(
                    f"{plan['role']}: attestation port {port} is unavailable at {host}: {error}"
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


def _terminate(identity: dict, grace_seconds: float = 10.0) -> None:
    members = _group_members(identity)
    if not _owns_process(identity):
        detail = (
            f"; surviving group PIDs {sorted(members)} require operator inspection"
            if members
            else ""
        )
        raise ValueError("native engine PID or start time changed; refusing to signal" + detail)
    owned = members
    _signal_members(owned, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    escalated = False
    while members := _group_members(identity):
        # A surviving recorded member anchors this group through leader exit.
        if not any(members.get(pid) == started for pid, started in owned.items()):
            raise ValueError(
                f"group ownership expired; surviving group PIDs {sorted(members)} "
                "require operator inspection"
            )
        newcomers = {pid: started for pid, started in members.items() if pid not in owned}
        owned.update(members)
        if time.monotonic() >= deadline:
            if escalated:
                raise ValueError(f"owned group PIDs {sorted(members)} survived SIGKILL")
            _signal_members(members, signal.SIGKILL)
            deadline = time.monotonic() + 5.0
            escalated = True
        elif newcomers:
            _signal_members(newcomers, signal.SIGKILL if escalated else signal.SIGTERM)
        time.sleep(0.1)


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
    baseline_used: int | None = None
    try:
        for run, plan in selected:
            _checked_plan(run, plan)
            before = gpu_memory(gpu_uuid)
            if baseline_used is None:
                baseline_used = before["used_mib"]
            budget = (
                Decimal(str(plan["shared_device"]["gpu_memory_utilization"])) * before["total_mib"]
            )
            allowance = (
                Decimal(str(plan["shared_device"]["device_allowance"])) * before["total_mib"]
            )
            record = {
                "role": plan["role"],
                "backend": "native",
                "shared_device": plan["shared_device"],
                "plan_sha256": digest(run / "launch.json"),
                "gpu_before": before,
                "gpu_baseline_used_mib": baseline_used,
                "budget_mib": float(budget),
                "device_allowance_mib": float(allowance),
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
                after = gpu_memory(gpu_uuid)
                aggregate_delta = after["used_mib"] - baseline_used
                record.update(
                    status="running",
                    process=identity,
                    vllm_version=live.vllm_version,
                    process_start_time_seconds=live.process_start_time_seconds,
                    model_revision=plan["model_revision"],
                    gpu_after=after,
                    observed_delta_mib=after["used_mib"] - before["used_mib"],
                    aggregate_delta_mib=aggregate_delta,
                )
                if Decimal(aggregate_delta) > allowance:
                    raise ValueError(
                        f"observed shared GPU use {aggregate_delta} MiB exceeds "
                        f"the {allowance} MiB device allowance"
                    )
                started.append(run)
            except (OSError, ValueError, subprocess.SubprocessError, httpx.HTTPError) as error:
                record.update(status="failed", error=str(error))
                try:
                    if identity is not None and _group_members(identity):
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
                    record["gpu_after_cleanup"] = gpu_memory(gpu_uuid)
                except (OSError, ValueError, subprocess.SubprocessError) as inspection_error:
                    record["gpu_after_error"] = str(inspection_error)
                write_private(run / "shared-start.json", json.dumps(record, indent=2) + "\n")
                message = (
                    f"{plan['role']}: native launch failed with "
                    f"{before['used_mib']} MiB used before start, "
                    f"{budget} MiB allocation"
                )
                if "observed_delta_mib" in record:
                    message += f", {record['observed_delta_mib']} MiB observed increase"
                message += f": {error}"
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
