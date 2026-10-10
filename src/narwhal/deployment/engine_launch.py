from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path

from narwhal.deployment.launch_engine.backend import launcher
from narwhal.deployment.launch_engine.plan import check_shared_memory


def selected_launch(document: dict, role: str, env: dict[str, str]) -> dict:
    if document.get("schema") != "narwhal.engine-launch" or document.get("schema_version") != 1:
        raise ValueError("Launch config requires narwhal.engine-launch schema version 1")
    if role not in document.get("engines", {}):
        raise ValueError(f"Launch config requires an allocation for {role}")
    entry = copy.deepcopy(document["engines"][role])
    name = entry.get("accelerator")
    if not isinstance(name, str) or not name.strip() or "<" in name:
        raise ValueError(f"{role}: set the declared accelerator product in the launch config")
    devices = entry.get("gpu_ids")
    if (
        not isinstance(devices, list)
        or not devices
        or any(not isinstance(d, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", d) for d in devices)
        or len(set(devices)) != len(devices)
    ):
        raise ValueError(f"{role}: gpu_ids must select distinct GPU indices or UUIDs")
    tp = entry.get("tensor_parallel_size")
    if type(tp) is not int or not 1 <= tp <= len(devices):
        raise ValueError(f"{role}: tensor_parallel_size must fit the declared GPU allocation")
    visibility = entry.get("gpu_visibility_env")
    if visibility not in {"ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"}:
        raise ValueError(f"{role}: select the ROCm or CUDA GPU visibility variable")
    transfer = dict(entry.get("transfer", {}))
    if transfer.get("transport") not in {"ucx_tcp", "ucx_rdma"}:
        raise ValueError(f"{role}: declare ucx_tcp or ucx_rdma transfer")
    gpu_tls = transfer.get("gpu_tls", "cuda" if visibility == "CUDA_VISIBLE_DEVICES" else "rocm")
    allowed_gpu_tls = {"cuda", "cuda_copy"} if visibility == "CUDA_VISIBLE_DEVICES" else {"rocm"}
    if gpu_tls not in allowed_gpu_tls:
        raise ValueError(f"{role}: transfer.gpu_tls is incompatible with the GPU runtime")
    transfer["gpu_tls"] = gpu_tls
    net = transfer.get("net_devices", "")
    if not isinstance(net, str):
        raise ValueError(f"{role}: transfer.net_devices must name the UCX devices")
    ref = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", net)
    if ref:
        if ref[1] != "NARWHAL_FABRIC_INTERFACE":
            raise ValueError(f"{role}: use NARWHAL_FABRIC_INTERFACE or literal UCX device names")
        net = env.get(ref[1], "")
    if not net or not re.fullmatch(r"[A-Za-z0-9_.:,-]+", net):
        raise ValueError(f"{role}: supply transfer.net_devices before preparation")
    if transfer["transport"] == "ucx_tcp" and ":" in net:
        raise ValueError(f"{role}: ucx_tcp selects Ethernet interface names")
    if transfer["transport"] == "ucx_rdma" and ":" not in net:
        raise ValueError(f"{role}: ucx_rdma selects an HCA and port")
    transfer["net_devices"] = net
    for paths in (entry.get("accelerator_devices"), transfer.get("devices")):
        if not isinstance(paths, list) or any(
            not isinstance(p, str) or not p.startswith("/dev/") or ".." in Path(p).parts
            for p in paths
        ):
            raise ValueError(f"{role}: declare accelerator and transfer device paths under /dev")
    if visibility == "ROCR_VISIBLE_DEVICES" and (
        "/dev/kfd" not in entry["accelerator_devices"]
        or not any(p.startswith("/dev/dri") for p in entry["accelerator_devices"])
    ):
        raise ValueError(f"{role}: ROCm allocation requires /dev/kfd and its DRI device mappings")
    if transfer["transport"] == "ucx_rdma" and not transfer["devices"]:
        raise ValueError(f"{role}: declare the RDMA device mappings")
    if entry.get("network_mode") != "host":
        raise ValueError(f"{role}: this launch record requires host networking")
    sources = entry.get("sources", {})
    if not isinstance(sources, dict) or any(
        not isinstance(sources.get(field), str) or not sources[field].strip()
        for field in ("allocation", "devices", "transfer")
    ):
        raise ValueError(f"{role}: name the allocation, device and transfer sources")
    engine = launcher(entry.get("runtime", {}).get("backend"))
    if "runtime" in entry:
        engine.validate_runtime(entry["runtime"])
    shared = entry.get("shared_device")
    if shared is not None:
        if not isinstance(shared, dict) or set(shared) != {
            "group",
            "gpu_uuid",
            "device_allowance",
            "gpu_memory_utilization",
        }:
            raise ValueError(f"{role}: shared_device requires group, GPU UUID and memory limits")
        if visibility != "CUDA_VISIBLE_DEVICES" or len(devices) != 1 or tp != 1:
            raise ValueError(f"{role}: shared_device requires one CUDA GPU and TP=1")
        if not all(
            isinstance(shared[name], str) and shared[name] for name in ("group", "gpu_uuid")
        ):
            raise ValueError(f"{role}: shared_device group and GPU UUID must be nonempty")
        for name in ("device_allowance", "gpu_memory_utilization"):
            fraction = shared[name]
            if (
                type(fraction) not in (int, float)
                or not math.isfinite(fraction)
                or not 0 < fraction <= 1
            ):
                raise ValueError(f"{role}: shared_device.{name} must be above 0 and at most 1")
        if shared["gpu_memory_utilization"] > shared["device_allowance"]:
            raise ValueError(f"{role}: GPU memory budget exceeds device allowance")
        check_shared_memory(
            engine, role, entry["runtime"]["extra_args"], shared["gpu_memory_utilization"]
        )
    entry["transfer"] = transfer
    entry["environment"] = {visibility: ",".join(devices), "UCX_NET_DEVICES": net}
    entry[engine.args_field] = engine.tensor_parallel_args(tp)
    return {"schema": "narwhal.engine-launch", "schema_version": 1, "role": role, **entry}


def expose_colocated_gpus(launches: dict[str, dict], hosts: list[list[str]]) -> None:
    for roles in hosts:
        cuda = [
            role
            for role in roles
            if role in launches
            and launches[role]["gpu_visibility_env"] == "CUDA_VISIBLE_DEVICES"
            and "shared_device" not in launches[role]
        ]
        for role in cuda:
            own = launches[role]["gpu_ids"]
            peers = [d for other in cuda for d in launches[other]["gpu_ids"] if d not in own]
            launches[role]["environment"]["CUDA_VISIBLE_DEVICES"] = ",".join(
                [*own, *dict.fromkeys(peers)]
            )


def load_launches(path: Path, roles: list[str], envs: dict[str, dict[str, str]]) -> dict[str, dict]:
    if not path.is_file():
        raise ValueError("NARWHAL_LAUNCH_CONFIG must select the supplied per-engine launch file")
    document = json.loads(path.read_text())
    return {role: selected_launch(document, role, envs[role]) for role in roles}
