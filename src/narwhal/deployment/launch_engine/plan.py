from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from .backend import (
    EngineLauncher,
    ModelFormats,
    cache_capture,
    engine_backend,
    launcher,
    plan_launcher,
)
from .checkpoint import checkpoint_status, file_status, inspect_checkpoint, json_digest
from .runtime import code_digest, digest, regular_file_digest, write_private
from .settings import KV_SIDE_CHANNEL_PORT_ENV, switch

# Launch backends: Docker on the host, a Kubernetes engine pod, or the host's Python runtime.
BACKENDS = ("container", "kubernetes", "native")
# Short root for per-plan event sockets; launch directories can exceed the socket path limit.
KV_EVENTS_ROOT = Path("/tmp")
# An engine in an image reads NARWHAL_MODEL_DIR at this path, and its hook files at HOOK_MOUNT.
MODEL_MOUNT = "/model"
HOOK_MOUNT = "/narwhal-hooks"
KV_EVENTS_MOUNT = "/narwhal-kv-events"
# A pod engine advertises its pod address, which Kubernetes substitutes for this reference.
POD_ADDRESS_ENV = "NARWHAL_POD_IP"
POD_ADDRESS = f"$({POD_ADDRESS_ENV})"
KV_EVENTS_SOCKETS = {"endpoint": "events.sock", "replay_endpoint": "replay.sock"}
# sockaddr_un holds 108 bytes, including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 107
SECRET_WORDS = ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "SSH")
MAX_DISCOVERED_CONTEXT = 16384
# Files the launch itself writes into the hook directory.
HOOK_FILES = frozenset({"launch_engine.py", "launch.json", "sitecustomize.py"})


def validate_runtime(runtime: dict) -> None:
    launcher(runtime.get("backend")).validate_runtime(runtime)


def check_versions_and_dtype(runtime: dict) -> None:
    packages = runtime.get("expected_packages", {})
    if any(not isinstance(v, str) or not v or "<" in v for v in packages.values()):
        raise ValueError("runtime.expected_packages requires resolved version strings")
    if (
        runtime.get("model_dtype") not in ("bfloat16", "float16")
        or runtime.get("kv_cache_dtype") != "auto"
    ):
        raise ValueError("this launcher uses a two-byte model dtype with kv_cache_dtype=auto")


def check_extra_args(args: list[str], value_options: set[str], flag_options: set[str]) -> None:
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("runtime.extra_args must be an argument list")
    index = 0
    while index < len(args):
        flag = args[index]
        if flag in value_options and index + 1 < len(args) and not args[index + 1].startswith("--"):
            index += 2
        elif flag in flag_options:
            index += 1
        else:
            raise ValueError(f"unsupported or incomplete runtime option: {flag}")


def check_environment(
    environment: dict,
    managed: set[str],
    prefixes: tuple[str, ...],
    forbidden: tuple[str, ...] = SECRET_WORDS,
) -> None:
    for name, value in environment.items():
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            or name in managed
            or not (name.startswith(prefixes) or name in ("LD_LIBRARY_PATH", "PYTHONPATH"))
            or any(word in name for word in forbidden)
            or not isinstance(value, str)
            or any(c in value for c in "\r\n\0")
        ):
            raise ValueError(f"unsupported runtime environment field: {name}")


def option(args: list[str], name: str) -> str | None:
    positions = [i for i, arg in enumerate(args) if arg == name]
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        return None
    return args[positions[0] + 1]


def discovered_settings(
    observed: dict, setting: Callable[[str, str], str], default_args: list[str]
) -> tuple[str, int, list, dict, dict]:
    dtype = setting("MODEL_DTYPE", observed.get("model_dtype") or "bfloat16")
    if dtype not in {"bfloat16", "float16"}:
        raise ValueError("set MODEL_DTYPE to bfloat16 or float16 in .env")
    max_len = min(
        int(observed.get("max_model_len") or MAX_DISCOVERED_CONTEXT), MAX_DISCOVERED_CONTEXT
    )
    context_length = int(setting("CONTEXT_LENGTH", str(max_len)))
    args = json.loads(setting("ENGINE_ARGS", json.dumps(default_args)))
    if not isinstance(args, list):
        raise ValueError("ENGINE_ARGS must be a JSON array")
    environment = dict(observed["image_environment"])
    overrides = json.loads(setting("ENGINE_ENV", "{}"))
    if not isinstance(overrides, dict):
        raise ValueError("ENGINE_ENV must be a JSON object")
    environment.update(overrides)
    return dtype, context_length, args, environment, overrides


def discovered_launch_settings(setting: Callable[[str, str], str]) -> dict:
    """Return the optional runtime fields that .env sets."""
    fields: dict = {}
    for name in ("PREFIX_CACHING", "KV_EVENTS"):
        raw = setting(name, "")
        if raw:
            if raw not in ("true", "false"):
                raise ValueError(f"set {name} to true or false in .env")
            fields[name.lower()] = raw == "true"
    tokenizer = setting("TOKENIZER", "")
    if tokenizer:
        fields["tokenizer"] = tokenizer
    return fields


def gpu_transport(role: str, transfer: Mapping, visibility: str) -> str:
    """Return the UCX GPU transport of a launch record, or its default when it names none.

    `ucx_rdma` takes the GPU runtime's copy transport, `cuda_copy` or `rocm_copy`: without GPU
    IPC, and with the shared-memory devices off, engines on one host move KV over their HCAs.
    """
    rocm = visibility == "ROCR_VISIBLE_DEVICES"
    allowed: tuple[str, ...]
    if transfer.get("transport") == "ucx_rdma":
        allowed = ("rocm_copy",) if rocm else ("cuda_copy",)
    else:
        allowed = ("rocm",) if rocm else ("cuda", "cuda_copy")
    value = transfer.get("gpu_tls", allowed[0])
    if value not in allowed:
        raise ValueError(
            f"{role}: transfer.gpu_tls is incompatible with the GPU runtime and transport; "
            f"use {' or '.join(allowed)}"
        )
    return value


def ipc_cache_off(ipc_cache: str | None) -> bool:
    return ipc_cache is not None and (ipc_cache.lower() in ("n", "no") or ipc_cache == "0")


def publishes_kv_events(engine: EngineLauncher, runtime: dict) -> bool:
    return switch(runtime, engine.launch_settings, "prefix_caching") and switch(
        runtime, engine.launch_settings, "kv_events"
    )


def side_channel_port(env: Mapping[str, str]) -> int:
    return int(env[KV_SIDE_CHANNEL_PORT_ENV])


def kv_events_policy(
    engine: EngineLauncher, runtime: dict, socket_dir: Path, engine_dir: str
) -> dict | None:
    if not publishes_kv_events(engine, runtime):
        return None
    for name in KV_EVENTS_SOCKETS.values():
        if len(str(socket_dir / name).encode()) > MAX_SOCKET_PATH_BYTES:
            raise ValueError(
                f"cache-event socket path under {socket_dir} exceeds "
                f"{MAX_SOCKET_PATH_BYTES} bytes; the socket root or user ID is too long"
            )
    return {
        "socket_dir": str(socket_dir),
        **{key: f"ipc://{engine_dir}/{name}" for key, name in KV_EVENTS_SOCKETS.items()},
    }


def kv_events_directory(plan: dict) -> None:
    # A Kubernetes engine binds its sockets in a volume of its pod, not on this host.
    if plan.get("kv_events") is None or plan.get("backend") == "kubernetes":
        return
    directory = Path(plan["kv_events"]["socket_dir"])
    for path in (directory.parent, directory):
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError(f"{path} must be a directory private to the launching user")


def container_options(plan: dict) -> list[str]:
    kv_events_directory(plan)
    return list(plan["common"])


def remove_kv_events_directory(plan: dict) -> None:
    if plan.get("kv_events") is None:
        return
    directory = Path(plan["kv_events"]["socket_dir"])
    for name in KV_EVENTS_SOCKETS.values():
        (directory / name).unlink(missing_ok=True)
    with contextlib.suppress(FileNotFoundError):
        directory.rmdir()


def requires_remote_code(model_dir: Path, *, include_tokenizer: bool = True) -> bool:
    def has_auto_map(value: object) -> bool:
        if isinstance(value, dict):
            return bool(value.get("auto_map")) or any(has_auto_map(item) for item in value.values())
        if isinstance(value, list):
            return any(has_auto_map(item) for item in value)
        return False

    names = ("config.json", "tokenizer_config.json") if include_tokenizer else ("config.json",)
    for name in names:
        path = model_dir / name
        if path.is_file() and has_auto_map(json.loads(path.read_text())):
            return True
    return False


def conv_state_layers(model: dict) -> bool:
    # State-space and short-convolution layers keep a convolutional state beside the KV cache.
    # Discovery runs this function's source in the remote probe, so it stays self-contained.
    text_model = model.get("text_config", model)
    linear = text_model.get("linear_attn_config", {})
    if (
        isinstance(linear, dict)
        and linear.get("kda_layers")
        and linear.get("short_conv_kernel_size")
    ):
        return True
    if text_model.get("mamba_d_conv") or text_model.get("mamba_d_state"):
        return True
    if text_model.get("linear_conv_kernel_dim") and "linear_attention" in text_model.get(
        "layer_types", []
    ):
        return True
    return any(
        isinstance(layer, str) and ("mamba" in layer.lower() or "ssm" in layer.lower())
        for layer in text_model.get("layer_types", [])
    )


def model_conv_state_layers(model_dir: Path) -> bool:
    return conv_state_layers(json.loads((model_dir / "config.json").read_text()))


def check_shared_memory(engine: EngineLauncher, role: str, args: list[str], budget: float) -> None:
    value = engine.memory_fraction(args)
    if value is None:
        raise ValueError(f"{role}: shared GPU launch requires one memory-utilization argument")
    try:
        matches = float(value) == budget
    except (ValueError, TypeError):
        matches = False
    if not matches:
        raise ValueError(f"{role}: engine memory setting differs from shared GPU budget")


def container_model_file(model_dir: str, path: str) -> str:
    """Return the container path of a model file that lies in the mounted model directory."""
    model = Path(path)
    try:
        relative = model.parent.resolve().relative_to(model_dir) / model.name
    except ValueError:
        raise ValueError("NARWHAL_MODEL_PATH must name a file in NARWHAL_MODEL_DIR") from None
    return f"{MODEL_MOUNT}/{relative.as_posix()}"


def model_file_changed(plan: dict) -> bool:
    """Whether the model file that `prepare` hashed is missing or no longer a file.

    A container plan's file must also remain a regular file in the mounted model directory at
    the path the engine loads. `check_model_files` covers the file's content.
    """
    if not plan.get("model_sha256"):
        return False
    path = Path(plan["model_path"])
    if plan.get("backend") == "native":
        return not path.is_file()
    model_flag = plan_launcher(plan).launch_settings.model_flag
    try:
        if container_model_file(plan["model_dir"], str(path)) != option(plan["args"], model_flag):
            return True
        return not stat.S_ISREG(os.lstat(path).st_mode)
    except (OSError, ValueError):
        return True


def append_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(data)


def env_file_name(plan: dict) -> str:
    return "engine.env" if plan.get("backend") == "native" else "container.env"


def read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def launch_headers(run: Path, plan: dict) -> dict[str, str] | None:
    key = read_env(run / env_file_name(plan)).get(plan_launcher(plan).api_key_env, "")
    return {"Authorization": f"Bearer {key}"} if key else None


def build(
    record: dict, env: dict[str, str], output: Path, *, backend: str = "container"
) -> tuple[dict, dict[str, str]]:
    """Return a launch plan and its engine environment for one launch backend.

    The `container` and `kubernetes` backends serve the engine from its image, with the model
    at MODEL_MOUNT and the hook files at HOOK_MOUNT. A Kubernetes engine takes its GPUs and its
    HCA from device plugins, so the plan names neither, and advertises its pod address.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unsupported engine launch backend: {backend}")
    in_image = backend != "native"
    pod = backend == "kubernetes"
    runtime = record["runtime"]
    engine = launcher(runtime.get("backend"))
    engine.validate_runtime(runtime)
    role = record["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("select an engine role")
    node = role.split("-")[1]
    transfer = record["transfer"]
    if pod and transfer["transport"] != "ucx_rdma":
        raise ValueError(f"{role}: a Kubernetes engine transfers KV over ucx_rdma")
    if pod and record.get("shared_device") is not None:
        raise ValueError(f"{role}: a Kubernetes engine takes its GPUs whole, without shared_device")
    image = env["NARWHAL_ENGINE_IMAGE"] if in_image else ""
    if pod and not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError(
            "NARWHAL_ENGINE_IMAGE must be a registry digest, <repository>@sha256:<hex>"
        )
    if backend == "container" and not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", image
    ):
        raise ValueError("NARWHAL_ENGINE_IMAGE must be an immutable image ID or registry digest")
    port = int(env["NARWHAL_ENGINE_PORT"])
    side_port = side_channel_port(env)
    attest_port = int(env["NARWHAL_ATTEST_PORT"])
    if (
        any(not 1 <= p <= 65535 for p in (port, side_port, attest_port))
        or len({port, side_port, attest_port}) != 3
    ):
        raise ValueError(
            f"engine, attestation and {engine.side_channel} ports must be distinct valid TCP ports"
        )
    source = POD_ADDRESS if pod else env[f"NARWHAL_NODE_{node}_IP"]
    endpoint = urlsplit(env[f"NARWHAL_NODE_{node}_URL"])
    if endpoint.scheme != "http" or endpoint.port != port or not endpoint.hostname:
        raise ValueError("the engine URL must select HTTP and NARWHAL_ENGINE_PORT")
    bind_host = (
        endpoint.hostname if not in_image else ("::" if ":" in endpoint.hostname else "0.0.0.0")
    )
    values = dict(runtime.get("environment", {}))
    if not pod:
        values.update(record["environment"])
    visibility = record["gpu_visibility_env"]
    gpu_tls = gpu_transport(role, transfer, visibility)
    rdma = transfer["transport"] == "ucx_rdma"
    values.update(
        {
            "UCX_TLS": f"{'rc' if rdma else 'tcp'},sm,self,{gpu_tls}",
            # Engines on one host then reach each other only through their HCAs.
            **({"UCX_SHM_DEVICES": "none"} if rdma else {}),
            "UCX_TCP_PORT_RANGE": env["NARWHAL_UCX_TCP_PORT_RANGE"],
            **engine.engine_env(
                source, side_port, env.get("NARWHAL_ENGINE_API_KEY", ""), bind_host
            ),
        }
    )
    if any(any(c in value for c in "\r\n\0") for value in values.values()):
        raise ValueError("engine environment values must fit one line")
    hook_root = HOOK_MOUNT if in_image else str(output / "hook")
    values["PYTHONPATH"] = hook_root + (
        ":" + values["PYTHONPATH"] if values.get("PYTHONPATH") else ""
    )
    model_dir = str(Path(env["NARWHAL_MODEL_DIR"]).resolve())
    model_file = env.get("NARWHAL_MODEL_PATH")
    model_path = os.path.abspath(model_file or model_dir)
    if not in_image or model_file:
        if isinstance(engine, ModelFormats):
            engine.check_model(runtime, model_path)
        elif in_image or Path(model_path).is_file():
            label = engine_backend(runtime.get("backend")).label
            raise ValueError(f"{label} launches take a model directory, not a model file")
    if in_image:
        model_ref = container_model_file(model_dir, model_path) if model_file else MODEL_MOUNT
    else:
        model_ref = model_path
    if backend == "container" and ("," in model_dir or "," in str(output)):
        raise ValueError("container bind-mount paths must use comma-free names")
    name = f"narwhal-{role}-{uuid.uuid4().hex[:12]}"
    # A pod engine binds its sockets in a volume of its own pod.
    socket_dir = Path(KV_EVENTS_MOUNT) if pod else KV_EVENTS_ROOT / f"narwhal-{os.geteuid()}" / name
    kv_events = kv_events_policy(
        engine,
        runtime,
        socket_dir,
        KV_EVENTS_MOUNT if in_image else str(socket_dir),
    )
    # CUDA IPC peers keep this engine's KV memory mapped until the engine evicts it.
    ipc_peers = gpu_tls == "cuda" and (
        record.get("shared_device") is not None
        or len(values.get("CUDA_VISIBLE_DEVICES", "").split(",")) > len(record["gpu_ids"])
    )
    common = []
    if backend == "container":
        common = [
            "--network",
            "host",
            "--ipc",
            "host",
            "--ulimit",
            "memlock=-1:-1",
            "--env-file",
            str(output / "container.env"),
            "--mount",
            f"type=bind,src={model_dir},dst={MODEL_MOUNT},readonly",
            "--mount",
            f"type=bind,src={output / 'cache'},dst=/root/.cache",
            "--mount",
            f"type=bind,src={output / 'hook'},dst={HOOK_MOUNT},readonly",
        ]
        if kv_events is not None:
            common.extend(["--mount", f"type=bind,src={socket_dir},dst={KV_EVENTS_MOUNT}"])
        for device in record["accelerator_devices"] + transfer["devices"]:
            common.extend(["--device", device])
        if visibility == "CUDA_VISIBLE_DEVICES":
            common.extend(["--gpus", "all"])
            # UCX CUDA IPC reaches a colocated engine only from the same PID namespace, and tells
            # peers apart by host PID. In separate namespaces, UCX 1.23 sends KV over TCP.
            if ipc_peers:
                common.extend(["--pid", "host"])
        else:
            common.extend(["--security-opt", "seccomp=unconfined"])
    # With the UCX IPC cache off, eviction releases a stopped peer's memory.
    evict_peers = ipc_peers and ipc_cache_off(values.get("UCX_CUDA_IPC_CACHE"))
    args, connector = engine.serve(
        record,
        model=model_ref,
        served_name=env["NARWHAL_ENGINE_MODEL_NAME"],
        host=bind_host,
        port=port,
        kv_events=kv_events,
        evict_peers=evict_peers,
    )
    config_files, config_args = engine.config_files(record, hook_root)
    reserved = sorted(HOOK_FILES)
    for file_name in config_files:
        if file_name in HOOK_FILES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", file_name):
            raise ValueError(
                f"engine configuration file name {file_name!r} must start with an ASCII letter or "
                "digit, contain only ASCII letters, digits, '.', '_' or '-', and differ from "
                f"{', '.join(reserved[:-1])} or {reserved[-1]}"
            )
    args = [*args, *config_args]
    shared = record.get("shared_device")
    if shared is not None:
        if (
            record["gpu_visibility_env"] != "CUDA_VISIBLE_DEVICES"
            or len(record["gpu_ids"]) != 1
            or record["tensor_parallel_size"] != 1
        ):
            raise ValueError(f"{role}: shared allocation requires one CUDA GPU and TP=1")
        check_shared_memory(engine, role, args, shared["gpu_memory_utilization"])
    plan = {
        "role": role,
        **({"engine": runtime["backend"]} if "backend" in runtime else {}),
        "image": image,
        "model_dir": model_dir,
        "name": name,
        "common": common,
        "args": args,
        **({"config_files": config_files} if config_files else {}),
        "connector": connector,
        "kv_events": kv_events,
        "expected_packages": runtime["expected_packages"],
        **({"runtime_pins": pins} if (pins := engine.runtime_pins(runtime)) else {}),
        "endpoint": endpoint.geturl(),
        "attestation_port": attest_port,
        "side_channel_port": side_port,
        "ucx_tls": values["UCX_TLS"],
        "cuda_ipc_peers": ipc_peers,
        **({"shared_device": shared} if shared is not None else {}),
        "revision": env["NARWHAL_DEPLOYMENT_REVISION"],
    }
    if in_image and model_file:
        plan["model_path"] = model_path
    if pod:
        plan["backend"] = "kubernetes"
    if backend == "native":
        model_revision = env.get("NARWHAL_MODEL_REVISION", "")
        if not re.fullmatch(r"[0-9a-f]{40}|sha256:[0-9a-f]{64}", model_revision):
            raise ValueError(
                "NARWHAL_MODEL_REVISION must be a 40-character commit or SHA-256 digest"
            )
        plan.update(
            backend="native",
            model_path=model_ref,
            model_config_path=f"{model_dir}/config.json",
            model_revision=model_revision,
            python_executable=sys.executable,
        )
        if attestation_url := env.get(f"NARWHAL_NODE_{node}_ATTESTATION_URL"):
            plan["attestation_url"] = attestation_url
    return plan, values


def check_model_file(plan: dict, env: dict[str, str], backend: str) -> bool:
    """Check the planned model file before `prepare` hashes it, and return whether one is named.

    A launch from an image requires NARWHAL_MODEL_SHA256; a native launch checks it when it is
    set.
    """
    if "model_path" not in plan:
        return False
    path = Path(plan["model_path"])
    container = backend != "native"
    pinned = env.get("NARWHAL_MODEL_SHA256", "")
    if not path.is_file():
        if container:
            raise ValueError("NARWHAL_MODEL_PATH must name a file in NARWHAL_MODEL_DIR")
        if pinned:
            raise ValueError(
                "NARWHAL_MODEL_SHA256 pins a model file; set NARWHAL_MODEL_PATH to that file"
            )
        return False
    if container and path.is_symlink():
        raise ValueError(
            "NARWHAL_MODEL_PATH must name a regular file, not a symbolic link, because the "
            "container sees only NARWHAL_MODEL_DIR"
        )
    if container and not pinned:
        raise ValueError("set NARWHAL_MODEL_SHA256 to the SHA-256 of NARWHAL_MODEL_PATH")
    return True


def prepare(output: Path, env: dict[str, str], *, backend: str = "container") -> None:
    source = Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"])
    record = json.loads(source.read_text())
    plan, values = build(record, env, output.resolve(), backend=backend)
    if digest(Path(env["NARWHAL_MODEL_DIR"]) / "config.json") != env["NARWHAL_MODEL_CONFIG_SHA256"]:
        raise ValueError("model config differs from its supplied hash")
    names_file = check_model_file(plan, env, backend)
    files = model_files(plan)
    pinned = env.get("NARWHAL_MODEL_TREE_SHA256")
    if pinned and files.directory != pinned:
        raise ValueError("model directory files differ from NARWHAL_MODEL_TREE_SHA256")
    supplied = env.get("NARWHAL_MODEL_SHA256")
    if names_file and supplied and files.model_file != supplied:
        raise ValueError("model file differs from its supplied hash")
    plan["model_tree_sha256"] = files.tree
    pod = backend == "kubernetes"
    if pod:
        # The engine pod serves its own copy of the model, which an init container hashes.
        from . import kubernetes

        plan["kubernetes"] = kubernetes.pod_settings(env, record, plan)
        probe = kubernetes.runtime_probe(output.resolve(), plan, values)
    else:
        # Later stages compare file status instead of reading the model files again.
        plan["model_status_sha256"] = files.status
    engine = plan_launcher(plan)
    capture = cache_capture(engine) is not None
    if capture:
        hook_source = Path(env["NARWHAL_CACHE_CAPTURE_HOOK"])
        hook_sha = env["NARWHAL_CACHE_CAPTURE_HOOK_SHA256"]
        if digest(hook_source) != hook_sha:
            raise ValueError("cache capture hook differs from its delivered hash")
    script = engine.runtime_script
    output.mkdir(mode=0o700, parents=True)
    if not pod:
        (output / "cache").mkdir(mode=0o700)
    (output / "hook").mkdir(mode=0o700)
    kv_events_directory(plan)
    if capture:
        write_private(output / "hook/sitecustomize.py", hook_source.read_text())
    write_private(output / "hook/launch_engine.py", script.read_text())
    for file_name, content in plan.get("config_files", {}).items():
        write_private(output / "hook" / file_name, content)
        (output / "hook" / file_name).chmod(0o644)
    env_file = env_file_name(plan)
    write_private(output / env_file, "".join(f"{k}={v}\n" for k, v in sorted(values.items())))
    plan.update(
        env_sha256=digest(output / env_file),
        launch_sha256=digest(source),
        launcher_sha256=digest(script),
        launcher_code_sha256=code_digest(script),
    )
    if capture:
        plan.update(
            cache_capture_sha256=hook_sha, cache_capture_code_sha256=code_digest(hook_source)
        )
    plan["model_config_sha256"] = env["NARWHAL_MODEL_CONFIG_SHA256"]
    if names_file:
        plan["model_sha256"] = files.model_file
    write_private(output / "launch.json", json.dumps(plan, indent=2) + "\n")
    write_private(output / "hook/launch.json", (output / "launch.json").read_text())
    for name in ("sitecustomize.py", "launch_engine.py", "launch.json"):
        if (output / "hook" / name).exists():
            (output / "hook" / name).chmod(0o644)
    (output / "hook").chmod(0o755)
    if pod:
        kubernetes.write_manifests(output, plan, probe)
        step = "review launch.json and kubernetes.json, then check the engine pod's records"
    else:
        check_name = "native runtime" if backend == "native" else "image"
        step = f"review launch.json and run the {check_name} check"
    print(f"Prepared {record['role']}; {step}.")


def _outside_model_dir(plan: dict) -> Path | None:
    """Return a native launch's model file or directory when it lies outside `model_dir`."""
    if plan.get("backend") != "native" or not plan.get("model_path"):
        return None
    path = Path(os.path.abspath(plan["model_path"]))
    # A Hugging Face snapshot entry is a symbolic link out of the snapshot directory.
    roots = {Path(plan["model_dir"]), Path(plan["model_dir"]).resolve()}
    inside = any(
        candidate.is_relative_to(root) for candidate in (path, path.resolve()) for root in roots
    )
    return None if inside else path


def _hashed_file(path: Path) -> tuple[str, str]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    content = regular_file_digest(resolved)
    status = file_status(path.name, resolved.stat())
    if status != file_status(path.name, before):
        raise ValueError(f"model file changed during inspection: {path}")
    return content, json_digest(status)


def _contained(plan: dict) -> bool:
    """Whether every model directory entry must resolve inside it.

    A container serves only the mounted directory. A native engine follows Hugging Face
    snapshot links into the cache.
    """
    return plan.get("backend") != "native"


def _combined(model_dir: str, outside: str | None) -> str:
    return model_dir if outside is None else json_digest([model_dir, outside])


class ModelFiles(NamedTuple):
    """The digests `prepare` records for a launch's model files."""

    # The digest of every model file the launch reads.
    tree: str
    # The status fingerprint that later stages compare instead of reading the files.
    status: str
    # The tree digest of `model_dir` alone, which discovery computes.
    directory: str
    # The SHA-256 of the model file the plan names, or None for a model directory.
    model_file: str | None


def _manifest_file(manifest: dict, root: str, path: Path) -> str | None:
    """Return the manifest SHA-256 of `path`, or None when the manifest omits it."""
    roots = {Path(root), Path(root).resolve()}
    names = {
        candidate.relative_to(base).as_posix()
        for candidate in (path, path.resolve())
        for base in roots
        if candidate.is_relative_to(base)
    }
    return next((row["sha256"] for row in manifest["files"] if row["path"] in names), None)


def model_files(plan: dict) -> ModelFiles:
    """Hash the launch's model files once.

    The files are those under `model_dir`, plus a native launch's model file or directory
    outside it. A model file in `model_dir` takes its SHA-256 from the directory's manifest.
    """
    manifest = inspect_checkpoint(Path(plan["model_dir"]), contained=_contained(plan))
    directory = manifest["model_tree_sha256"]
    path = Path(os.path.abspath(plan["model_path"])) if plan.get("model_path") else None
    outside = _outside_model_dir(plan)
    if outside is None:
        model_file = None
        if path is not None and path.is_file():
            model_file = _manifest_file(manifest, plan["model_dir"], path)
            if model_file is None:
                raise ValueError(f"model file is excluded from the model file digest: {path}")
        return ModelFiles(directory, manifest["status_sha256"], directory, model_file)
    if outside.is_dir():
        extra = inspect_checkpoint(outside, contained=False)
        content, status = extra["model_tree_sha256"], extra["status_sha256"]
        model_file = None
    else:
        content, status = _hashed_file(outside)
        model_file = content
    return ModelFiles(
        _combined(directory, content),
        _combined(manifest["status_sha256"], status),
        directory,
        model_file,
    )


def _model_status(plan: dict) -> str:
    status = checkpoint_status(Path(plan["model_dir"]), contained=_contained(plan))
    outside = _outside_model_dir(plan)
    if outside is None:
        return status
    if outside.is_dir():
        return _combined(status, checkpoint_status(outside, contained=False))
    return _combined(status, json_digest(file_status(outside.name, outside.stat())))


def check_model_files(plan: dict) -> None:
    """Refuse a launch whose model files changed since `prepare` hashed them.

    The check compares file status, so it reads no model file.
    """
    if "model_status_sha256" not in plan:
        raise ValueError("launch plan predates model file digests; prepare a fresh launch plan")
    try:
        status = _model_status(plan)
    except (OSError, ValueError) as exc:
        raise ValueError(f"model files unreadable: {exc}; prepare a fresh launch plan") from exc
    if status != plan["model_status_sha256"]:
        raise ValueError(
            "model files changed after launch preparation; prepare a fresh launch plan"
        )


def config_files_changed(run: Path, plan: dict) -> str | None:
    """Name the first engine configuration file whose hook copy differs from the plan."""
    for file_name, content in plan.get("config_files", {}).items():
        path = run / "hook" / file_name
        if not path.is_file() or path.read_text() != content:
            return file_name
    return None


def load(run: Path) -> dict:
    plan = json.loads((run / "launch.json").read_text())
    if not isinstance(plan, dict):
        raise ValueError(f"{run / 'launch.json'}: expected a JSON object")
    if digest(run / env_file_name(plan)) != plan.get("env_sha256"):
        raise ValueError("engine environment changed; prepare a fresh launch directory")
    return plan
