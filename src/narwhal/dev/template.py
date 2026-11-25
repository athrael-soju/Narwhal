from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from decimal import ROUND_CEILING, Decimal
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from narwhal.config.loading import load as load_fleet
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine.backend import launcher
from narwhal.deployment.launch_engine.runtime import write_private
from narwhal.deployment.launch_engine.start import gpu_memory
from narwhal.runtime.listeners import check_engine_bind

_PORT_RANGE_OFFSETS = {"engine_first": 1, "attestation_first": 101, "side_channel_first": 201}


def reference() -> dict:
    return _read_template("reference-v1.json")


def default_template() -> dict:
    return _read_template("small-cuda-v1.json")


def _read_template(filename: str) -> dict:
    source = resources.files("narwhal.dev").joinpath(filename)
    document = json.loads(source.read_text())
    if document.get("schema") != "narwhal.dev-template" or document.get("schema_version") != 1:
        raise ValueError(f"installed Narwhal dev template {filename} requires schema version 1")
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


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_Ifaddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_ifu", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]


def _ipv4_addresses(interface: str) -> list[str]:
    # getifaddrs(3) lists every address of every interface, as `ip address show` does. It names
    # a labelled alias such as eth0:1 by its label, which `ip address show dev eth0` includes.
    libc = ctypes.CDLL(None, use_errno=True)
    name = os.fsencode(interface)
    head = ctypes.POINTER(_Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), "getifaddrs failed")
    try:
        found = []
        node = head
        while node:
            entry = node.contents
            label = entry.ifa_name
            if entry.ifa_addr and label and (label == name or label.startswith(name + b":")):
                family = ctypes.c_ushort.from_address(entry.ifa_addr).value
                if family == socket.AF_INET:
                    raw = ctypes.string_at(entry.ifa_addr + 4, 4)
                    found.append(socket.inet_ntop(socket.AF_INET, raw))
            node = entry.ifa_next
        return found
    finally:
        libc.freeifaddrs(head)


def _address(interface: str) -> str:
    if not interface or not all(char.isalnum() or char in "_.-" for char in interface):
        raise ValueError("fabric interface must be a Linux network interface name")
    try:
        socket.if_nametoindex(interface)
        addresses = _ipv4_addresses(interface)
    except OSError as exc:
        raise ValueError(f"fabric interface {interface!r} is unavailable") from exc
    if len(addresses) != 1:
        raise ValueError(f"select an interface with one IPv4 address: {interface}")
    return addresses[0]


def _check_packages(packages: dict[str, str]) -> None:
    for name, expected in packages.items():
        try:
            found = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError(f"native runtime requires installed {name}=={expected}") from exc
        if found != expected:
            raise ValueError(f"native runtime requires {name}=={expected}; found {found}")


def model_files(model: dict, model_path: Path) -> dict[Path, str]:
    if "filename" in model:
        return {model_path: model["sha256"]}
    return {model_path / name: expected for name, expected in model["files_sha256"].items()}


def _port_layout(template: dict, count: int) -> tuple[dict[str, int], set[int]]:
    ports = template["ports"]
    if not 2 <= count <= 8:
        raise ValueError("Narwhal shared-device fleet requires 2 to 8 engines")
    used = {ports["router"]}
    if not 1 <= ports["router"] <= 65535:
        raise ValueError("router port is invalid")
    for key in _PORT_RANGE_OFFSETS:
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
        try:
            check_engine_bind(address, port)
        except OSError as exc:
            raise ValueError(f"port {port} is unavailable on {address}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_FRACTIONS = ("gpu_memory_utilization", "device_allowance")
_MIB = ("engine_memory_mib", "device_memory_mib")


def _sized_in_mib(spec: dict) -> bool:
    return any(name in spec["allocation"] for name in _MIB)


def _fraction(mib: int, total_mib: int) -> float:
    """The smallest fraction of the GPU, to four places, that holds `mib`."""
    share = (Decimal(mib) / Decimal(total_mib)).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
    return float(min(share, Decimal(1)))


def _check_budgets(allocation: dict, count: int) -> None:
    fraction = Decimal(str(allocation["gpu_memory_utilization"]))
    allowance = Decimal(str(allocation["device_allowance"]))
    if (
        not fraction.is_finite()
        or not allowance.is_finite()
        or not 0 < fraction <= 1
        or not fraction * count <= allowance <= 1
    ):
        raise ValueError("engine budgets exceed the shared GPU allowance")


def instance_gpus(value: dict) -> list[str]:
    """The GPU UUIDs of an instance's engines: one shared GPU, or one GPU per engine."""
    return list(value["gpu_uuids"]) if "gpu_uuids" in value else [value["gpu_uuid"]]


def _select_gpus(
    rows: list[dict[str, str]], gpu_uuid: str | None, gpu_uuids: list[str] | None, count: int
) -> list[dict[str, str]]:
    """The discovered GPU of a shared instance, or the GPU of each engine in engine order."""
    if gpu_uuids is None:
        selected = [row for row in rows if gpu_uuid is None or row["uuid"] == gpu_uuid]
        if len(selected) != 1:
            raise ValueError("select exactly one physical GPU with gpu_uuid")
        return selected
    found = {row["uuid"]: row for row in rows}
    missing = [uuid for uuid in gpu_uuids if uuid not in found]
    if missing:
        raise ValueError(f"nvidia-smi lists no GPU {', '.join(missing)}")
    selected = [found[uuid] for uuid in gpu_uuids[:count]]
    if len({row["name"] for row in selected}) != 1:
        raise ValueError("the engines' GPUs must be one product")
    return selected


def _check_gpu_list(gpu_uuids: list[str], count: int) -> None:
    repeated = sorted({uuid for uuid in gpu_uuids if gpu_uuids.count(uuid) > 1})
    if repeated:
        raise ValueError(f"--gpus names {', '.join(repeated)} more than once")
    if not all(uuid.startswith("GPU-") for uuid in gpu_uuids):
        raise ValueError("--gpus takes physical GPU UUIDs that start with GPU-")
    if count > len(gpu_uuids):
        raise ValueError(f"{count} engines need {count} GPUs; --gpus names {len(gpu_uuids)}")


def starts_directly(value: dict) -> bool:
    """Whether a template or instance starts its engines as plain processes."""
    return value.get("start") == "direct"


def check_instance_ports(value: dict, root: Path) -> None:
    ports = value.get("ports")
    missing = sorted(set(_PORT_RANGE_OFFSETS) - set(ports)) if isinstance(ports, dict) else []
    if missing:
        raise ValueError(
            f"instance {root} has no ports.{missing[0]}. "
            "Create a fresh instance with narwhal dev init --instance <new-directory>"
        )


def _template_differences(requested: dict, existing: dict, prefix: str = "") -> list[str]:
    differences = []
    for key in sorted(requested.keys() | existing.keys()):
        field = f"{prefix}.{key}" if prefix else key
        if key not in requested or key not in existing:
            differences.append(field)
        elif isinstance(requested[key], dict) and isinstance(existing[key], dict):
            differences.extend(_template_differences(requested[key], existing[key], field))
        elif requested[key] != existing[key]:
            differences.append(field)
    return differences


def materialize(
    output: Path,
    *,
    model_dir: Path | None = None,
    model_path: Path | None = None,
    fabric_interface: str | None = None,
    gpu_uuid: str | None = None,
    gpu_uuids: list[str] | None = None,
    template: dict | None = None,
    engine_count: int | None = None,
    port_base: int | None = None,
    gpu_memory_utilization: float | None = None,
    device_allowance: float | None = None,
    engine_binary: Path | None = None,
    accelerated: bool | None = None,
    prepared: bool = False,
    recorded: dict | None = None,
    engine_memory_mib: int | None = None,
    device_memory_mib: int | None = None,
) -> Path:
    output = output.expanduser().resolve()
    existing = None
    saved = None
    # A prepared directory holds downloaded files and no instance yet.
    if output.exists() and not (prepared and not (output / "instance.json").exists()):
        existing = json.loads((output / "instance.json").read_text())
        if existing.get("schema") != "narwhal.dev-instance" or existing.get("schema_version") != 1:
            raise ValueError(f"unsupported instance configuration: {output}")
        if not starts_directly(existing):
            check_instance_ports(existing, output)
        saved = json.loads((output / "template.json").read_text())
    if gpu_uuid is not None and gpu_uuids is not None:
        raise ValueError("select one shared GPU with --gpu or a GPU per engine with --gpus")
    source = template if template is not None else saved
    spec = copy.deepcopy(source if source is not None else default_template())
    if spec.get("schema") != "narwhal.dev-template" or spec.get("schema_version") != 1:
        raise ValueError("Narwhal dev template requires schema version 1")
    if existing is not None and starts_directly(existing) != starts_directly(spec):
        raise ValueError(
            f"existing instance {output} starts its engines another way than this template; "
            "create a fresh instance with narwhal dev init --instance <new-directory>"
        )
    if starts_directly(spec):
        from . import direct

        refused = [
            flag
            for flag, value in (
                ("--model-dir", model_dir),
                ("--gpu", gpu_uuid),
                ("--gpus", gpu_uuids),
                ("--interface", fabric_interface),
                ("--gpu-memory-utilization", gpu_memory_utilization),
                ("--device-allowance", device_allowance),
                ("--engine-memory-mib", engine_memory_mib),
                ("--device-memory-mib", device_memory_mib),
            )
            if value is not None
        ]
        if refused:
            raise ValueError(f"this template takes no {', '.join(refused)}")
        return direct.materialize(
            output,
            spec,
            existing=existing,
            saved=saved,
            engine_binary=engine_binary,
            model_path=model_path,
            engine_count=engine_count,
            port_base=port_base,
            accelerated=accelerated,
            template_given=template is not None,
            prepared=prepared,
            recorded=recorded,
        )
    if engine_binary is not None or accelerated is not None or prepared:
        raise ValueError("this template takes no --engine-binary or --no-accelerator")
    allocation = {
        "engine_count": engine_count,
        "gpu_memory_utilization": gpu_memory_utilization,
        "device_allowance": device_allowance,
        "engine_memory_mib": engine_memory_mib,
        "device_memory_mib": device_memory_mib,
    }
    given = {
        kind: [name for name in names if allocation[name] is not None]
        for kind, names in (("fractions", _FRACTIONS), ("mib", _MIB))
    }
    if given["fractions"] and given["mib"]:
        raise ValueError(
            "size the engines by fractions of the GPU or in MiB, with one pair of options"
        )
    for kind, names in (("fractions", _FRACTIONS), ("mib", _MIB)):
        other = _MIB if kind == "fractions" else _FRACTIONS
        # A pair of one kind replaces the template's pair of the other kind.
        if given[kind] and any(name in spec["allocation"] for name in other):
            if len(given[kind]) == len(names):
                for name in other:
                    spec["allocation"].pop(name, None)
            elif existing is None:
                flags = " and ".join("--" + name.replace("_", "-") for name in names)
                raise ValueError(f"this template needs both {flags}")
    for field, allocation_value in allocation.items():
        if allocation_value is not None:
            spec["allocation"][field] = allocation_value
    if port_base is not None:
        spec["ports"].update(
            router=port_base,
            **{key: port_base + offset for key, offset in _PORT_RANGE_OFFSETS.items()},
        )
    if existing is not None and saved is not None:
        conflicts = []
        for flag, field, value in (
            (
                "--model-dir",
                "model_dir",
                str(model_dir.expanduser().resolve()) if model_dir else None,
            ),
            (
                "--model",
                "model_path",
                str(model_path.expanduser().absolute()) if model_path else None,
            ),
            ("--interface", "fabric_interface", fabric_interface),
            ("--gpu", "gpu_uuid", gpu_uuid),
            ("--engine-count", "engine_count", engine_count),
        ):
            if value is not None and value != existing.get(field):
                conflicts.append(flag)
        # The engines run on the first GPUs of the list, one each.
        if gpu_uuids is not None:
            _check_gpu_list(gpu_uuids, existing["engine_count"])
            _select_gpus(_gpu_rows(), None, gpu_uuids, existing["engine_count"])
            if gpu_uuids[: existing["engine_count"]] != existing.get("gpu_uuids"):
                conflicts.append("--gpus")
        conflicts.extend(
            "--" + field.replace("_", "-")
            for field in (*_FRACTIONS, *_MIB)
            if allocation[field] is not None and allocation[field] != saved["allocation"].get(field)
        )
        if port_base is not None and any(
            spec["ports"][field] != existing["ports"][field]
            for field in ("router", *_PORT_RANGE_OFFSETS)
        ):
            conflicts.append("--port-base")
        if template is not None:
            requested, recorded_spec = copy.deepcopy(spec), copy.deepcopy(saved)
            if _sized_in_mib(spec):
                # init wrote the fractions it derived for this GPU beside the MiB values.
                for name in _FRACTIONS:
                    requested["allocation"].pop(name, None)
                    recorded_spec["allocation"].pop(name, None)
            conflicts.extend(
                f"--template {field}" for field in _template_differences(requested, recorded_spec)
            )
        if conflicts:
            raise ValueError(
                f"existing instance {output} conflicts with {', '.join(conflicts)}; "
                "create a fresh instance with narwhal dev init --instance <new-directory> "
                "and the requested settings"
            )
        return output
    hub = Path.home() / ".cache/huggingface/hub"
    model = spec["model"]
    snapshot = hub / ("models--" + model["repository"].replace("/", "--")) / "snapshots"
    model_path = model_path or snapshot / model["revision"] / model.get("filename", "")
    model_dir = (
        model_dir
        or hub
        / ("models--" + model["tokenizer_repository"].replace("/", "--"))
        / "snapshots"
        / model["tokenizer_revision"]
    )
    fabric_interface = fabric_interface if fabric_interface is not None else "eth0"
    model_dir = model_dir.expanduser().resolve(strict=True)
    # Hugging Face snapshot files are often symlinks to blobs.
    model_path = model_path.expanduser().absolute()
    if "filename" not in model:
        if not model_path.is_dir():
            raise ValueError("model path must name a checkpoint directory")
        for path, expected in model_files(model, model_path).items():
            if _sha256(path) != expected:
                raise ValueError(f"model file {path.name} differs from the pinned revision")
    elif not model_path.is_file():
        raise ValueError("GGUF model path must name a file")
    elif model_path.name != model["filename"]:
        raise ValueError(f"template requires {model['filename']}")
    elif _sha256(model_path) != model["sha256"]:
        raise ValueError("GGUF file SHA-256 differs from the pinned model revision")
    config_path = model_dir / "config.json"
    if not config_path.is_file():
        raise ValueError("model directory requires config.json")
    for name, expected in spec["model"].get("tokenizer_sha256", {}).items():
        if _sha256(model_dir / name) != expected:
            raise ValueError(f"tokenizer file {name} differs from the pinned revision")
    count = spec["allocation"]["engine_count"]
    ports, used_ports = _port_layout(spec, count)
    if gpu_uuids is not None:
        _check_gpu_list(gpu_uuids, count)
    # Engines on a shared GPU split its allowance; an engine on its own GPU has the allowance
    # of that GPU.
    per_gpu = count if gpu_uuids is None else 1
    sized = _sized_in_mib(spec)
    if not sized:
        _check_budgets(spec["allocation"], per_gpu)
    gpus = _select_gpus(_gpu_rows(), gpu_uuid, gpu_uuids, count)
    product = spec["gpu"].get("product")
    if product and gpus[0]["name"] != product:
        raise ValueError(f"template requires {product}; found {gpus[0]['name']}")
    memories = {gpu["uuid"]: gpu_memory(gpu["uuid"]) for gpu in gpus}
    # The smallest GPU sets the fractions, so that every GPU holds the stated MiB.
    total_mib = min(memory["total_mib"] for memory in memories.values())
    if total_mib < spec["gpu"].get("minimum_total_mib", 0):
        raise ValueError("GPU VRAM is below the qualified template minimum")
    if sized:
        needs = spec["allocation"]
        for name in _MIB:
            if type(needs[name]) is not int or needs[name] < 1:
                raise ValueError(f"allocation.{name} must be a positive integer")
        if needs["engine_memory_mib"] * per_gpu > needs["device_memory_mib"]:
            raise ValueError(
                f"{count} engines of {needs['engine_memory_mib']} MiB exceed the "
                f"{needs['device_memory_mib']} MiB the engines are allowed"
                if gpu_uuids is None
                else f"an engine of {needs['engine_memory_mib']} MiB exceeds the "
                f"{needs['device_memory_mib']} MiB each GPU allows"
            )
        if needs["device_memory_mib"] > total_mib:
            raise ValueError(
                f"GPU VRAM of {total_mib} MiB is below the "
                f"{needs['device_memory_mib']} MiB the engines are allowed"
            )
        # The launch records carry each budget as a fraction of this GPU's memory. Each
        # fraction rounds up, so the allowance also covers the engines' rounded fractions.
        share = _fraction(needs["engine_memory_mib"], total_mib)
        covering = float((Decimal(str(share)) * per_gpu).quantize(Decimal("0.0001")))
        needs.update(
            gpu_memory_utilization=share,
            device_allowance=max(_fraction(needs["device_memory_mib"], total_mib), covering),
        )
        _check_budgets(needs, per_gpu)
    fraction = spec["allocation"]["gpu_memory_utilization"]
    allowance = spec["allocation"]["device_allowance"]
    # A shared GPU holds the whole allowance; a GPU of its own holds one engine's fraction.
    held = allowance if gpu_uuids is None else fraction
    for uuid, memory in memories.items():
        available = memory["total_mib"] - memory["used_mib"]
        required = int(memory["total_mib"] * held) + spec["gpu"]["reserve_mib"]
        if available < required:
            raise ValueError(
                ("GPU VRAM" if gpu_uuids is None else f"GPU {uuid} VRAM")
                + f" reserve failed: {available} MiB free, {required} MiB required"
            )
    ready = spec["runtime"].get("ready_seconds", 1)
    if type(ready) is not int or ready < 1:
        raise ValueError("runtime.ready_seconds must be a positive integer")
    engine = launcher(spec["runtime"].get("backend"))
    _check_packages(spec["runtime"]["expected_packages"])
    engine.check_dev_imports(spec["runtime"])
    engine.check_dev_files(spec["runtime"])
    address = _address(fabric_interface)
    _check_free_ports(used_ports, "127.0.0.1")
    # Engines on one GPU form its shared_device group; engines on their own GPUs form none.
    shared = (
        {
            "group": f"{socket.gethostname()}:{gpus[0]['uuid']}",
            "gpu_uuid": gpus[0]["uuid"],
            "device_allowance": allowance,
            "gpu_memory_utilization": fraction,
        }
        if gpu_uuids is None
        else None
    )
    runtime = engine.dev_runtime(spec["runtime"], model_dir, fraction)
    engine.validate_runtime(runtime)
    launch: dict[str, Any] = {
        "schema": "narwhal.engine-launch",
        "schema_version": 1,
        "engines": {},
    }
    fleet: dict[str, Any] = {
        "schema": "narwhal.fleet",
        "schema_version": 2,
        "model": spec["model"]["served_name"],
        "hardware": {
            "accelerator": gpus[0]["name"],
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
        "engine": {
            "first_token_timeout_s": 10.0,
            **(
                {"backend": runtime["backend"], "connector": runtime["connector"]}
                if "backend" in runtime
                else {}
            ),
            **(
                {"engine_api_key_env": "NARWHAL_ENGINE_API_KEY"}
                if os.environ.get("NARWHAL_ENGINE_API_KEY")
                else {}
            ),
        },
    }
    for index in range(count):
        number = index + 1
        name = f"engine-{number}"
        engine_port = ports["engine_first"] + index
        attest_port = ports["attestation_first"] + index
        gpu = gpus[0] if shared is not None else gpus[index]
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
            **({"shared_device": shared} if shared is not None else {}),
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
                **({"shared_device": shared} if shared is not None else {}),
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
                    **(
                        {"gpu_uuid": gpus[0]["uuid"]}
                        if shared is not None
                        else {"gpu_uuids": [gpu["uuid"] for gpu in gpus]}
                    ),
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
