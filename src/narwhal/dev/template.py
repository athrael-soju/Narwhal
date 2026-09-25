"""Materialize the installed native workstation reference using Narwhal contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from narwhal.config.loading import load as load_fleet
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine import gpu_memory, validate_runtime, write_private


def reference() -> dict:
    """Read the versioned template from the installed Narwhal distribution."""
    source = resources.files("narwhal.dev").joinpath("reference-v1.json")
    document = json.loads(source.read_text())
    if document.get("schema") != "narwhal.dev-template" or document.get("schema_version") != 1:
        raise ValueError("installed Narwhal Dev template requires schema version 1")
    return document


def _gpu_rows() -> list[dict[str, str]]:
    executable = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
            [executable, "--query-gpu=name,uuid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "NVIDIA GPU discovery failed; native template requires nvidia-smi"
        ) from exc
    rows = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2 or not fields[0] or not fields[1].startswith("GPU-"):
            raise ValueError(f"NVIDIA GPU discovery returned an invalid row: {line!r}")
        rows.append({"name": fields[0], "uuid": fields[1]})
    if not rows:
        raise ValueError("NVIDIA GPU discovery found no GPUs")
    return rows


def _address(interface: str) -> str:
    if not interface or not all(char.isalnum() or char in "_.-" for char in interface):
        raise ValueError("fabric interface must be a Linux network interface name")
    try:
        result = subprocess.run(  # noqa: S603 - validated interface, no shell
            [shutil.which("ip") or "/usr/sbin/ip", "-json", "address", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"fabric interface {interface!r} is unavailable") from exc
    addresses = [
        entry["local"]
        for device in json.loads(result.stdout)
        for entry in device.get("addr_info", [])
        if entry.get("family") == "inet"
    ]
    if len(addresses) != 1:
        raise ValueError(f"select an interface with one IPv4 address: {interface}")
    return str(addresses[0])


def _check_runtime(packages: dict[str, str]) -> None:
    for name, expected in packages.items():
        try:
            found = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError(f"native runtime requires installed {name}=={expected}") from exc
        if found != expected:
            raise ValueError(f"native runtime requires {name}=={expected}; found {found}")
    # Use the same vLLM connector API checked by narwhal-engine, before writing an instance.
    script = """from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
config = KVTransferConfig(
    kv_connector='NixlConnector', kv_role='kv_both',
    kv_connector_extra_config={'backends': ['UCX'], 'enforce_handshake_compat': True},
)
KVConnectorFactory.get_connector_class(config)
"""
    try:
        result = subprocess.run(  # noqa: S603 - this interpreter runs a fixed import check
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr[-800:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ValueError(f"native vLLM/NIXL connector import failed: {detail}") from exc
    if result.stderr and "Traceback" in result.stderr:
        raise ValueError(f"native vLLM/NIXL connector import reported: {result.stderr[-800:]}")


def check_plugin(runtime: dict) -> None:
    """Bind the GGUF Python overlay and compiled extension to the qualified runtime."""
    if "gguf_plugin_python_sha256" not in runtime:
        return
    package = Path(str(metadata.distribution("vllm-gguf-plugin").locate_file("vllm_gguf_plugin")))
    digest = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        digest.update(source.relative_to(package).as_posix().encode() + b"\0")
        digest.update(source.read_bytes() + b"\0")
    if digest.hexdigest() != runtime["gguf_plugin_python_sha256"]:
        raise ValueError("GGUF plugin Python sources differ from the qualified revision")
    if _sha256(package / "_C_gguf.abi3.so") != runtime["gguf_plugin_extension_sha256"]:
        raise ValueError("GGUF plugin CUDA extension differs from the qualified wheel")


def _port_layout(template: dict, count: int) -> tuple[dict[str, int], set[int]]:
    ports = template["ports"]
    if not 2 <= count <= 8:
        raise ValueError("Narwhal shared-device fleet requires 2 to 8 engines")
    used = {ports["router"]}
    if not 1 <= ports["router"] <= 65535:
        raise ValueError("router port is invalid")
    for key in ("engine_first", "attestation_first", "nixl_first"):
        first = ports[key]
        if type(first) is not int or not 1 <= first <= 65536 - count:
            raise ValueError(f"{key} cannot fit {count} engine ports")
        for port in range(first, first + count):
            if port in used:
                raise ValueError(f"template ports collide at {port}")
            used.add(port)
    return ports, used


def _check_free_ports(ports: set[int], address: str) -> None:
    for port in sorted(ports):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((address, port))
            except OSError as exc:
                raise ValueError(f"port {port} is unavailable on {address}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def materialize(
    output: Path,
    *,
    model_dir: Path,
    model_path: Path,
    fabric_interface: str,
    gpu_uuid: str | None = None,
    template: dict | None = None,
) -> Path:
    """Check the host and write one private native instance without replacing files."""
    spec = copy.deepcopy(template if template is not None else reference())
    if spec.get("schema") != "narwhal.dev-template" or spec.get("schema_version") != 1:
        raise ValueError("Narwhal Dev template requires schema version 1")
    output = output.expanduser().resolve()
    if output.exists():
        existing = json.loads((output / "instance.json").read_text())
        if existing.get("schema") != "narwhal.dev-instance" or existing.get("schema_version") != 1:
            raise ValueError(f"unsupported instance configuration: {output}")
        return output
    model_dir = model_dir.expanduser().resolve(strict=True)
    # Keep the snapshot filename: Hugging Face cache files are often symlinks to blobs.
    model_path = model_path.expanduser().absolute()
    if not model_path.is_file():
        raise ValueError("GGUF model path must name a file")
    if model_path.name != spec["model"]["filename"]:
        raise ValueError(f"template requires {spec['model']['filename']}")
    if _sha256(model_path) != spec["model"]["sha256"]:
        raise ValueError("GGUF file SHA-256 differs from the pinned model revision")
    config_path = model_dir / "config.json"
    if not config_path.is_file():
        raise ValueError("model directory requires config.json")
    for name, expected in spec["model"].get("tokenizer_sha256", {}).items():
        if _sha256(model_dir / name) != expected:
            raise ValueError(f"tokenizer file {name} differs from the pinned revision")
    count = spec["allocation"]["engine_count"]
    ports, used_ports = _port_layout(spec, count)
    fraction = spec["allocation"]["gpu_memory_utilization"]
    allowance = spec["allocation"]["device_allowance"]
    if not 0 < fraction <= 1 or not fraction * count <= allowance <= 1:
        raise ValueError("engine budgets exceed the shared GPU allowance")
    rows = _gpu_rows()
    selected = [row for row in rows if gpu_uuid is None or row["uuid"] == gpu_uuid]
    if len(selected) != 1:
        raise ValueError("select exactly one physical GPU with gpu_uuid")
    gpu = selected[0]
    if gpu["name"] != spec["gpu"]["product"]:
        raise ValueError(f"template requires {spec['gpu']['product']}; found {gpu['name']}")
    memory = gpu_memory(gpu["uuid"])
    if memory["total_mib"] < spec["gpu"]["minimum_total_mib"]:
        raise ValueError("GPU VRAM is below the qualified template minimum")
    available = memory["total_mib"] - memory["used_mib"]
    required = int(memory["total_mib"] * allowance) + spec["gpu"]["reserve_mib"]
    if available < required:
        raise ValueError(f"GPU VRAM reserve failed: {available} MiB free, {required} MiB required")
    _check_runtime(spec["runtime"]["expected_packages"])
    check_plugin(spec["runtime"])
    address = _address(fabric_interface)
    _check_free_ports(used_ports, "127.0.0.1")
    hostname = socket.gethostname()
    group = f"{hostname}:{gpu['uuid']}"
    shared = {
        "group": group,
        "gpu_uuid": gpu["uuid"],
        "device_allowance": allowance,
        "gpu_memory_utilization": fraction,
    }
    runtime = {
        "expected_packages": spec["runtime"]["expected_packages"],
        "model_dtype": spec["runtime"]["model_dtype"],
        "kv_cache_dtype": spec["runtime"]["kv_cache_dtype"],
        "block_size": spec["runtime"]["block_size"],
        "environment": spec["runtime"]["environment"],
        "extra_args": [
            "--tokenizer",
            str(model_dir),
            "--hf-config-path",
            str(model_dir),
            "--load-format",
            "gguf",
            "--language-model-only",
            "--max-model-len",
            str(spec["runtime"]["max_model_len"]),
            "--gpu-memory-utilization",
            str(fraction),
            "--max-num-seqs",
            str(spec["runtime"]["max_num_seqs"]),
            "--enforce-eager",
        ],
    }
    validate_runtime(runtime)
    launch: dict[str, Any] = {
        "schema": "narwhal.engine-launch",
        "schema_version": 1,
        "engines": {},
    }
    fleet: dict[str, Any] = {
        "schema": "narwhal.fleet",
        "schema_version": 1,
        "model": spec["model"]["served_name"],
        "hardware": {
            "accelerator": gpu["name"],
            "accelerators_per_engine": 1,
            "tensor_parallel": 1,
        },
        "engines": [],
        "slo": spec["slo"],
        "controller": {
            "min_prefill": 1,
            "min_decode": 1,
            **spec["controller"],
        },
        "profiles": {"path": str(output / "profiles.json")},
        "recovery": {"state_path": str(output / "router-state.json")},
        "engine": {"first_token_timeout_s": 10.0},
    }
    for index in range(count):
        number = index + 1
        name = f"engine-{number}"
        engine_port = ports["engine_first"] + index
        attest_port = ports["attestation_first"] + index
        launch["engines"][name] = {
            "accelerator": gpu["name"],
            "gpu_ids": [gpu["uuid"]],
            "tensor_parallel_size": 1,
            "gpu_visibility_env": "CUDA_VISIBLE_DEVICES",
            "accelerator_devices": [],
            "network_mode": "host",
            "transfer": {
                "transport": "ucx_tcp",
                "net_devices": fabric_interface,
                "devices": [],
                "gpu_tls": "cuda_copy",
            },
            "shared_device": shared,
            "sources": {
                "allocation": "live NVIDIA GPU UUID and VRAM discovery",
                "devices": "native CUDA runtime",
                "transfer": fabric_interface,
            },
            "runtime": runtime,
        }
        fleet["engines"].append(
            {
                "iid": f"n{number}",
                "url": f"http://127.0.0.1:{engine_port}",
                "attestation_url": f"http://127.0.0.1:{attest_port}/v1/attestation",
                "role": "prefill" if index < count // 2 else "decode",
                "shared_device": shared,
            }
        )
        selected_launch(launch, name, {})
    router_url = f"http://127.0.0.1:{ports['router']}"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="narwhal-dev-check-") as temporary:
        trial = Path(temporary) / "fleet.json"
        trial.write_text(json.dumps(fleet))
        load_fleet(trial)
    output.mkdir(mode=0o700)
    try:
        write_private(output / "template.json", json.dumps(spec, indent=2) + "\n")
        write_private(output / "fleet.json", json.dumps(fleet, indent=2) + "\n")
        write_private(output / "engine-launch.json", json.dumps(launch, indent=2) + "\n")
        write_private(
            output / "instance.json",
            json.dumps(
                {
                    "schema": "narwhal.dev-instance",
                    "schema_version": 1,
                    "backend": "native",
                    "model_dir": str(model_dir),
                    "model_path": str(model_path),
                    "model_revision": spec["model"]["revision"],
                    "model_config_sha256": _sha256(config_path),
                    "gpu_uuid": gpu["uuid"],
                    "fabric_interface": fabric_interface,
                    "fabric_address": address,
                    "router_url": router_url,
                    "ucx_range": ports["ucx_range"],
                    "ports": ports,
                    "engine_count": count,
                    "narwhal_version": metadata.version("narwhal-inference"),
                    "python_executable": sys.executable,
                },
                indent=2,
            )
            + "\n",
        )
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return output
