"""Start checked serving containers, alone or several on one shared GPU."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from .. import stages
from .check import require_checked
from .docker import docker, run_runtime_script
from .plan import container_options, env_file_name, load, read_env
from .runtime import digest, write_private

# Readiness budget for each engine of a shared start.
READY_SECONDS = 180


def _create_container(run: Path, plan: dict) -> str:
    require_checked(run, plan)
    if digest(run / "hook/sitecustomize.py") != plan["cache_capture_sha256"]:
        raise ValueError("cache capture hook changed; prepare a fresh launch plan")
    if digest(run / "hook/launch_engine.py") != plan["launcher_sha256"]:
        raise ValueError("cache capture launcher changed; prepare a fresh launch plan")
    if digest(run / "hook/launch.json") != digest(run / "launch.json"):
        raise ValueError("cache capture plan changed; prepare a fresh launch plan")
    if (run / "container.id").exists():
        raise ValueError("launch already has a container; inspect its recorded ID before recovery")
    cid = docker(
        [
            "create",
            "--name",
            plan["name"],
            *container_options(plan),
            "--env",
            "NARWHAL_CAPTURE_CACHE=1",
            "--env",
            "NARWHAL_CACHE_PLAN=/narwhal-hooks/launch.json",
            "--env",
            "NARWHAL_CACHE_OUTPUT=/tmp/narwhal-cache-layout.json",
            "--entrypoint",
            "python3",
            plan["image"],
            *plan["args"],
        ],
        run,
        "launch.log",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("Docker returned an invalid container ID; inspect launch.log")
    return cid


def start(run: Path, plan: dict) -> None:
    cid = _create_container(run, plan)
    write_private(run / "container.id", cid + "\n")
    docker(["start", cid], run, "launch.log")
    print("Container started; follow its logs and verify the HTTP endpoints.")


def gpu_memory(gpu_uuid: str) -> dict[str, int]:
    """Read live device pressure before advancing a shared-GPU startup."""
    executable = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    result = subprocess.run(
        [
            executable,
            "--query-gpu=uuid,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise ValueError(f"GPU memory inspection failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) == 3 and fields[0] == gpu_uuid:
            used, total = map(int, fields[1:])
            if not 0 <= used <= total or total == 0:
                break
            return {"used_mib": used, "total_mib": total}
    raise ValueError(f"GPU memory inspection did not find usable device {gpu_uuid}")


def validate_shared_gpu(run: Path, plan: dict) -> None:
    """Bind the serving CUDA selection to the UUID used for shared memory accounting."""
    values = read_env(run / env_file_name(plan))
    selected = values.get("CUDA_VISIBLE_DEVICES", "")
    expected = plan["shared_device"]["gpu_uuid"]
    if selected == expected:
        return
    if not (
        re.fullmatch(r"[0-9]+", selected)
        or (selected.startswith("GPU-") and expected.startswith(selected))
    ):
        raise ValueError(
            f"{plan['role']}: CUDA_VISIBLE_DEVICES={selected!r} differs from shared GPU {expected}"
        )
    # CUDA ordinals resolve in the serving runtime; NVML indices can use another order.
    script = """import torch
from uuid import UUID
if torch.cuda.device_count() != 1:
    raise ValueError('shared launch requires exactly one visible CUDA device')
device = torch.cuda.get_device_properties(0)
print('NARWHAL_SHARED_GPU=GPU-' + str(UUID(bytes=bytes(device.uuid.bytes))))
"""
    output = run_runtime_script(run, plan, script, [], f"shared-gpu-{uuid.uuid4().hex}.log")
    prefix = "NARWHAL_SHARED_GPU="
    observed = [
        line.removeprefix(prefix) for line in output.splitlines() if line.startswith(prefix)
    ]
    if observed != [expected]:
        raise ValueError(
            f"{plan['role']}: CUDA_VISIBLE_DEVICES={selected!r} resolved to {observed!r}, "
            f"expected shared GPU {expected}"
        )


def validate_shared_runs(
    runs: list[Path], *, backend: str = "container"
) -> list[tuple[Path, dict]]:
    """Check every budget and port before starting a colocated engine."""
    if backend not in {"container", "native"}:
        raise ValueError(f"unsupported engine launch backend: {backend}")
    if not 2 <= len(runs) <= 8 or len(set(runs)) != len(runs):
        raise ValueError("shared GPU start requires two to eight distinct launch directories")
    selected = [(run, load(run)) for run in runs]
    group = selected[0][1].get("shared_device")
    if not group:
        raise ValueError("shared GPU start requires a declared device allocation")
    allowance = Decimal(str(group["device_allowance"]))
    total = Decimal(0)
    ports: dict[int, str] = {}
    roles: set[str] = set()
    for run, plan in selected:
        if (plan.get("backend") or "container") != backend:
            raise ValueError(f"{plan['role']}: launch backend differs from requested startup")
        if backend == "container":
            require_checked(run, plan)
        else:
            checked = json.loads((run / "checked.json").read_text())
            if (
                checked.get("backend") != "native"
                or checked.get("plan_sha256") != digest(run / "launch.json")
                or checked.get("python_executable") != plan["python_executable"]
            ):
                raise ValueError(
                    f"{plan['role']}: native runtime check differs from the launch plan"
                )
        shared = plan.get("shared_device")
        if not shared or any(
            shared[key] != group[key] for key in ("group", "gpu_uuid", "device_allowance")
        ):
            raise ValueError(f"{plan['role']}: shared GPU identity or allowance differs")
        validate_shared_gpu(run, plan)
        role = plan["role"]
        if role in roles:
            raise ValueError(f"{role}: duplicate engine role in shared GPU start")
        roles.add(role)
        total += Decimal(str(shared["gpu_memory_utilization"]))
        for label, port in (
            ("engine", urlsplit(plan["endpoint"]).port),
            ("attestation", plan["attestation_port"]),
            ("NIXL", plan["side_channel_port"]),
        ):
            if port in ports:
                raise ValueError(f"{role} {label} port {port} collides with {ports[port]}")
            ports[port] = f"{role} {label}"
        if any(
            (run / name).exists()
            for name in ("shared-start.json", "container.id", "native-process.json")
        ):
            raise ValueError(f"{role}: launch directory already has a startup record or engine")
    if total > allowance:
        raise ValueError(f"shared GPU budgets total {total} above allowance {allowance}")
    return selected


def wait_ready(run: Path, plan: dict, cid: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    url = plan["endpoint"].rstrip("/") + "/health"
    while time.monotonic() < deadline:
        state = json.loads(
            docker(["inspect", "--format", "{{json .State}}", cid], run, "launch.log")
        )
        if not state.get("Running"):
            logs = docker(["logs", "--tail", "80", cid], run, "launch.log", include_stderr=True)
            raise ValueError(
                f"{plan['role']}: container exited {state.get('ExitCode')}: {logs[-2000:]}"
            )
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise ValueError(f"{plan['role']}: health endpoint did not respond within {seconds}s")


def _rollback_shared_containers(
    started: list[tuple[Path, str, dict]],
    cause: str,
    removed: set[str] | None = None,
) -> list[str]:
    errors = []
    for run, cid, record in reversed(started):
        record["status"] = "failed"
        record.setdefault("error", f"shared startup rolled back: {cause}")
        try:
            if cid not in (removed or set()):
                docker(["rm", "--force", cid], run, "launch.log")
            record["cleanup_status"] = "removed"
        except (OSError, ValueError, subprocess.SubprocessError) as cleanup_error:
            record.update(cleanup_status="failed", cleanup_error=str(cleanup_error))
            errors.append(f"{record['role']} ({cid}): {cleanup_error}")
        try:
            pending = run / f".shared-start-{uuid.uuid4().hex}.json"
            write_private(pending, json.dumps(record, indent=2) + "\n")
            pending.replace(run / "shared-start.json")
        except OSError as evidence_error:
            errors.append(f"{record['role']}: cleanup record failed: {evidence_error}")
    return errors


def start_shared(runs: list[Path], ready_seconds: int) -> None:
    selected = validate_shared_runs(runs)
    gpu_uuid = selected[0][1]["shared_device"]["gpu_uuid"]
    started: list[tuple[Path, str, dict]] = []
    baseline_used: int | None = None
    try:
        for run, plan in selected:
            role = plan["role"]
            shared = plan["shared_device"]
            before = gpu_memory(gpu_uuid)
            if baseline_used is None:
                baseline_used = before["used_mib"]
            budget_mib = Decimal(str(shared["gpu_memory_utilization"])) * before["total_mib"]
            allowance = Decimal(str(shared["device_allowance"])) * before["total_mib"]
            record = {
                "role": role,
                "backend": "container",
                "shared_device": shared,
                "ucx_tls": plan["ucx_tls"],
                "plan_sha256": digest(run / "launch.json"),
                "gpu_before": before,
                "gpu_baseline_used_mib": baseline_used,
                "budget_mib": float(budget_mib),
                "device_allowance_mib": float(allowance),
            }
            try:
                if Decimal(before["total_mib"] - before["used_mib"]) < budget_mib:
                    raise ValueError(
                        f"{role}: free GPU memory is below its {budget_mib} MiB allocation"
                    )
                cid = _create_container(run, plan)
                # Ownership is recorded before any filesystem or Docker start failure.
                started.append((run, cid, record))
                record["container_id"] = cid
                write_private(run / "container.id", cid + "\n")
                docker(["start", cid], run, "launch.log")
                wait_ready(run, plan, cid, ready_seconds)
                state = json.loads(
                    docker(["inspect", "--format", "{{json .State}}", cid], run, "launch.log")
                )
                command = json.loads(
                    docker(["inspect", "--format", "{{json .Config.Cmd}}", cid], run, "launch.log")
                )
                image_id = docker(["inspect", "--format", "{{.Image}}", cid], run, "launch.log")
                checked = json.loads((run / "checked.json").read_text())
                if (
                    not state.get("Running")
                    or type(state.get("Pid")) is not int
                    or state["Pid"] < 1
                    or command != plan["args"]
                    or image_id != checked["image_id"]
                ):
                    raise ValueError(f"{role}: live process, image or arguments differ from plan")
                after = gpu_memory(gpu_uuid)
                aggregate_delta = after["used_mib"] - baseline_used
                record.update(
                    status="running",
                    process_id=state["Pid"],
                    image_id=image_id,
                    vllm_args=command,
                    gpu_after=after,
                    observed_delta_mib=after["used_mib"] - before["used_mib"],
                    aggregate_delta_mib=aggregate_delta,
                )
                if Decimal(aggregate_delta) > allowance:
                    raise ValueError(
                        f"observed shared GPU use {aggregate_delta} MiB above the "
                        f"{baseline_used} MiB baseline exceeds the {allowance} MiB device allowance"
                    )
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                stages.StageCancelled,
            ) as error:
                record.update(status="failed", error=str(error))
                if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
                    record.update(failure_stage=error.stage, failure_context=error.context)
                if "gpu_after" not in record:
                    try:
                        record["gpu_after"] = gpu_memory(gpu_uuid)
                    except (OSError, ValueError, subprocess.SubprocessError) as inspection_error:
                        record["gpu_after_error"] = str(inspection_error)
                write_private(run / "shared-start.json", json.dumps(record, indent=2) + "\n")
                if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
                    raise
                raise ValueError(
                    f"{role}: shared GPU start failed at {before['used_mib']}/"
                    f"{before['total_mib']} MiB used, {budget_mib} MiB budget: {error}"
                ) from error
            write_private(run / "shared-start.json", json.dumps(record, indent=2) + "\n")
            print(f"{role}: ready on {gpu_uuid}; {record['gpu_after']['used_mib']} MiB used")
    except (OSError, ValueError, subprocess.SubprocessError, stages.StageCancelled) as error:
        removed = set()
        if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
            removed.update(error.context.get("docker_reconciliation", {}).get("removed", []))
        cleanup_errors = _rollback_shared_containers(started, str(error), removed)
        if cleanup_errors:
            if isinstance(error, (stages.StageTimeout, stages.StageCancelled)):
                error.context["container_cleanup_errors"] = cleanup_errors
                raise
            raise ValueError(
                f"{error}; container cleanup failed: {'; '.join(cleanup_errors)}"
            ) from error
        raise
