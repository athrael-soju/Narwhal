from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx

from narwhal.engines.attestation import EngineIdentity, fetch_engine_identity
from narwhal.engines.host_process import process_clock
from narwhal.runtime.listeners import check_http_bind

from . import stages
from .launch_engine.backend import cache_capture, engine_backend, plan_launcher
from .launch_engine.check import read_checked
from .launch_engine.plan import (
    check_model_files,
    config_files_changed,
    kv_events_directory,
    launch_headers,
    load,
    model_file_changed,
    read_env,
    remove_kv_events_directory,
)
from .launch_engine.runtime import digest, write_private
from .launch_engine.start import (
    READY_SECONDS,
    distinct_roles_and_ports,
    gpu_memory,
    validate_shared_runs,
)


def process_identity(pid: int) -> dict[str, int | str]:
    """The pid, boot ID and start ticks of a live process group leader."""
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat.rsplit(")", 1)[1].split()
    if len(fields) < 20 or fields[0] in {"Z", "X"} or os.getpgid(pid) != pid:
        raise ValueError(f"process {pid} is not an owned process group leader")
    return {"pid": pid, "boot_id": boot_id, "start_ticks": int(fields[19])}


def native_identity(run: Path, plan: dict, pid: int | str) -> EngineIdentity:
    backend = engine_backend(plan.get("engine"))
    headers = launch_headers(run, plan)
    return asyncio.run(
        fetch_engine_identity(
            plan["endpoint"],
            headers=headers,
            reader=backend.identity,
            process=process_clock(pid=int(pid)),
        )
    )


def owns_process(identity: dict) -> bool:
    """Whether the leader that `identity` records is still alive as that leader."""
    try:
        return process_identity(identity["pid"]) == identity
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return False


def _process_stat(pid: int) -> tuple[str, int, int, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[2]), int(fields[3]), int(fields[19])


def group_members(identity: dict) -> dict[int, int]:
    """Live members of the recorded leader's session, by pid and start ticks.

    The members stay visible after the leader exits. A leader pid that now names another
    process yields none.
    """
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
    checked = read_checked(run)
    if (
        checked.get("backend") != "native"
        or checked.get("plan_sha256") != digest(run / "launch.json")
        or checked.get("python_executable") != plan["python_executable"]
    ):
        raise ValueError("native runtime check differs from the launch plan")
    if digest(Path(plan["model_config_path"])) != plan["model_config_sha256"]:
        raise ValueError("model config changed after launch preparation")
    if not Path(plan["model_path"]).exists() or model_file_changed(plan):
        raise ValueError("model file changed after launch preparation")
    # The file status check covers a model file that model_sha256 names.
    check_model_files(plan)
    if cache_capture(plan_launcher(plan)) is not None and (
        digest(run / "hook/sitecustomize.py") != plan["cache_capture_sha256"]
    ):
        raise ValueError("cache capture hook changed after launch preparation")
    if digest(run / "hook/launch_engine.py") != plan["launcher_sha256"]:
        raise ValueError("cache capture launcher changed after launch preparation")
    if digest(run / "hook/launch.json") != digest(run / "launch.json"):
        raise ValueError("cache capture plan changed after launch preparation")
    if changed := config_files_changed(run, plan):
        raise ValueError(f"engine configuration {changed} changed after launch preparation")
    return checked


def _environment(run: Path, plan: dict) -> dict[str, str]:
    engine = plan_launcher(plan)
    environment = {**os.environ, engine.api_key_env: "", **read_env(run / "engine.env")}
    if cache_capture(engine) is not None:
        environment.update(
            NARWHAL_CAPTURE_CACHE="1",
            NARWHAL_CACHE_PLAN=str(run / "hook/launch.json"),
            NARWHAL_CACHE_OUTPUT=str(run / "cache-layout.json"),
        )
    return environment


def _ports_free(selected: list[tuple[Path, dict]]) -> None:
    for run, plan in selected:
        backend = engine_backend(plan.get("engine"))
        engine = backend.engine_launcher()
        endpoint = urlsplit(plan["endpoint"])
        host, port = endpoint.hostname, endpoint.port
        assert host is not None and port is not None
        try:
            backend.fabric.check_engine_bind(host, port)
        except OSError as error:
            raise ValueError(
                f"{plan['role']}: engine port {port} is unavailable at {host}: {error}"
            ) from error
        host, port = engine.side_channel_address(_environment(run, plan))
        try:
            backend.fabric.check_engine_bind(host, port, fabric=True)
        except OSError as error:
            raise ValueError(
                f"{plan['role']}: {engine.side_channel} port {port} is unavailable at {host}: "
                f"{error}"
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
        if not owns_process(identity):
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


def terminate(identity: dict, grace_seconds: float = 10.0) -> None:
    """Stop the recorded group with SIGTERM, then SIGKILL, refusing a changed leader."""
    members = group_members(identity)
    if not owns_process(identity):
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
    while members := group_members(identity):
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
    identity = json.loads((run / "native-process.json").read_text())
    terminate(identity)
    remove_kv_events_directory(json.loads((run / "launch.json").read_text()))
    write_private(
        run / "native-stop.json",
        json.dumps({"identity": identity, "stopped_at": time.time()}, indent=2) + "\n",
    )


# Files that show a launch directory already started an engine.
STARTUP_RECORDS = ("shared-start.json", "native-start.json", "container.id", "native-process.json")


def own_gpu(run: Path, plan: dict) -> str:
    """The one GPU UUID that a plan's CUDA_VISIBLE_DEVICES selects."""
    selected = read_env(run / "engine.env").get("CUDA_VISIBLE_DEVICES", "")
    if not re.fullmatch(r"GPU-[0-9A-Za-z-]+", selected):
        raise ValueError(
            f"{plan['role']}: CUDA_VISIBLE_DEVICES={selected!r} must name one GPU by its UUID"
        )
    return selected


def _memory_fraction(plan: dict) -> Decimal:
    value = plan_launcher(plan).memory_fraction(plan["args"])
    try:
        fraction = Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        fraction = None
    if fraction is None or not fraction.is_finite() or not 0 < fraction <= 1:
        raise ValueError(
            f"{plan['role']}: native start requires one memory fraction argument above 0 "
            "and at most 1"
        )
    return fraction


def validate_native_runs(runs: list[Path]) -> list[tuple[Path, dict]]:
    """Check one to eight native plans that each start an engine on a GPU of its own."""
    if not 1 <= len(runs) <= 8 or len(set(runs)) != len(runs):
        raise ValueError("native start requires one to eight distinct launch directories")
    selected = [(run, load(run)) for run in runs]
    gpus: dict[str, str] = {}
    for run, plan in selected:
        if plan.get("backend") != "native":
            raise ValueError(f"{plan['role']}: native start requires a native launch plan")
        if plan.get("shared_device") is not None:
            raise ValueError(f"{plan['role']}: the plan shares its GPU; start it with start-shared")
        gpu = own_gpu(run, plan)
        if gpu in gpus:
            raise ValueError(f"{plan['role']}: GPU {gpu} is also the GPU of {gpus[gpu]}")
        gpus[gpu] = plan["role"]
        _memory_fraction(plan)
        if any((run / name).exists() for name in STARTUP_RECORDS):
            raise ValueError(f"{plan['role']}: launch directory already has a startup record")
    distinct_roles_and_ports(selected)
    return selected


def start_shared(runs: list[Path], ready_seconds: int = READY_SECONDS) -> None:
    """Start two to eight checked plans on the GPU their shared_device names."""
    if ready_seconds < 1:
        raise ValueError("ready_seconds must be positive")
    selected = validate_shared_runs(runs, backend="native")
    _ports_free(selected)
    _start(selected, ready_seconds, shared=True)


def start_each(runs: list[Path], ready_seconds: int = READY_SECONDS) -> None:
    """Start one to eight checked plans, each on the GPU its CUDA_VISIBLE_DEVICES names."""
    if ready_seconds < 1:
        raise ValueError("ready_seconds must be positive")
    selected = validate_native_runs(runs)
    _ports_free(selected)
    _start(selected, ready_seconds, shared=False)


def _start(selected: list[tuple[Path, dict]], ready_seconds: int, *, shared: bool) -> None:
    # A shared start bounds its GPU's growth from the first reading. An engine on its own GPU
    # needs free memory for its fraction only.
    record_name = "shared-start.json" if shared else "native-start.json"
    started: list[Path] = []
    baseline_used: int | None = None
    try:
        for run, plan in selected:
            _checked_plan(run, plan)
            if shared:
                gpu_uuid = plan["shared_device"]["gpu_uuid"]
                fraction = Decimal(str(plan["shared_device"]["gpu_memory_utilization"]))
            else:
                gpu_uuid = own_gpu(run, plan)
                fraction = _memory_fraction(plan)
            before = gpu_memory(gpu_uuid)
            budget = fraction * before["total_mib"]
            record: dict = {
                "role": plan["role"],
                "backend": "native",
                **({"shared_device": plan["shared_device"]} if shared else {"gpu_uuid": gpu_uuid}),
                "plan_sha256": digest(run / "launch.json"),
                "gpu_before": before,
                "budget_mib": float(budget),
                "engine_args": plan["args"],
                "expected_packages": plan["expected_packages"],
                "model_sha256": plan.get("model_sha256", plan["model_config_sha256"]),
            }
            allowance = Decimal(0)
            if shared:
                if baseline_used is None:
                    baseline_used = before["used_mib"]
                allowance = (
                    Decimal(str(plan["shared_device"]["device_allowance"])) * before["total_mib"]
                )
                record.update(
                    gpu_baseline_used_mib=baseline_used, device_allowance_mib=float(allowance)
                )
            identity = None
            process = None
            try:
                if Decimal(before["total_mib"] - before["used_mib"]) < budget:
                    raise ValueError(f"free GPU memory is below the {budget} MiB allocation")
                if (run / "native-process.json").exists():
                    raise ValueError("native launch already has a process record")
                kv_events_directory(plan)
                log_fd = os.open(run / "startup.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(log_fd, "w") as log:
                    process = subprocess.Popen(
                        [plan["python_executable"], *plan["args"]],
                        env=_environment(run, plan),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                identity = process_identity(process.pid)
                write_private(run / "native-process.json", json.dumps(identity, indent=2) + "\n")
                _wait_ready(run, plan, identity, ready_seconds)
                live = native_identity(run, plan, identity["pid"])
                if live.version != _checked_plan(run, plan)["engine_version"]:
                    raise ValueError("live engine version differs from the checked native runtime")
                after = gpu_memory(gpu_uuid)
                record.update(
                    status="running",
                    process=identity,
                    engine_version=live.version,
                    process_start_time_seconds=live.process_start_time_seconds,
                    model_revision=plan["model_revision"],
                    gpu_after=after,
                    observed_delta_mib=after["used_mib"] - before["used_mib"],
                )
                if baseline_used is not None:
                    aggregate_delta = after["used_mib"] - baseline_used
                    record["aggregate_delta_mib"] = aggregate_delta
                    if Decimal(aggregate_delta) > allowance:
                        raise ValueError(
                            f"observed shared GPU use {aggregate_delta} MiB exceeds "
                            f"the {allowance} MiB device allowance"
                        )
                started.append(run)
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                httpx.HTTPError,
                stages.StageCancelled,
            ) as error:
                record.update(status="failed", error=str(error))
                if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
                    record.update(failure_stage=error.stage, failure_context=error.context)
                try:
                    if identity is not None and group_members(identity):
                        terminate(identity)
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
                write_private(run / record_name, json.dumps(record, indent=2) + "\n")
                if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
                    raise
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
            write_private(run / record_name, json.dumps(record, indent=2) + "\n")
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        stages.StageCancelled,
    ) as error:
        cleanup_errors = []
        for run in reversed(started):
            try:
                stop(run)
            except (OSError, ValueError) as cleanup_error:
                cleanup_errors.append(f"{run}: {cleanup_error}")
        if cleanup_errors:
            if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
                error.context["native_cleanup_errors"] = cleanup_errors
                raise
            raise ValueError(
                f"{error}; native engine cleanup failed: {'; '.join(cleanup_errors)}"
            ) from error
        raise
