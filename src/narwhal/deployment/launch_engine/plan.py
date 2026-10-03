"""Build and load pinned engine launch plans for container or native backends."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .runtime import LAUNCHER, digest, write_private

VALUE_OPTIONS = {
    "--max-model-len",
    "--gpu-memory-utilization",
    "--max-num-batched-tokens",
    "--max-num-seqs",
    "--reasoning-parser",
    "--attention-backend",
    "--tokenizer",
    "--hf-config-path",
    "--load-format",
    "--kv-events-config",
}
FLAG_OPTIONS = {
    "--trust-remote-code",
    "--language-model-only",
    "--enforce-eager",
    "--async-scheduling",
    "--no-disable-hybrid-kv-cache-manager",
    "--disable-hybrid-kv-cache-manager",
    "--enable-prefix-caching",
    "--no-enable-prefix-caching",
}
# Short root for the per-plan sockets vLLM binds and host subscribers connect to;
# launch directories can exceed the socket path limit.
KV_EVENTS_ROOT = Path("/tmp")
KV_EVENTS_MOUNT = "/narwhal-kv-events"
KV_EVENTS_SOCKETS = {"endpoint": "events.sock", "replay_endpoint": "replay.sock"}
# sockaddr_un holds 108 bytes, including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 107
# vLLM's NIXL engine_ttl for CUDA IPC peers with the UCX IPC cache off; the router's first
# peer release round follows it.
ENGINE_TTL_S = 60
# First UCX release that unmaps CUDA IPC rkeys when NIXL removes a remote agent.
UCX_PEER_RELEASE = (1, 22)
MANAGED_ENV = {
    "ROCR_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES",
    "UCX_NET_DEVICES",
    "UCX_TLS",
    "UCX_TCP_PORT_RANGE",
    "NIXL_HOST_IP",
    "VLLM_NIXL_SIDE_CHANNEL_HOST",
    "VLLM_NIXL_SIDE_CHANNEL_PORT",
    "VLLM_API_KEY",
}
ENV_PREFIXES = (
    "VLLM_",
    "UCX_",
    "NIXL_",
    "ROCM_",
    "HIP_",
    "HSA_",
    "AITER_",
    "PYTORCH_",
    "SAFETENSORS_",
)


def validate_runtime(runtime: dict) -> None:
    packages = runtime.get("expected_packages", {})
    if not packages.get("vllm") or not any(name in packages for name in ("nixl", "nixl-rocm")):
        raise ValueError("runtime.expected_packages requires pinned vLLM and NIXL versions")
    if any(not isinstance(v, str) or not v or "<" in v for v in packages.values()):
        raise ValueError("runtime.expected_packages requires resolved version strings")
    if (
        runtime.get("model_dtype") not in ("bfloat16", "float16")
        or runtime.get("kv_cache_dtype") != "auto"
    ):
        raise ValueError("this launcher uses a two-byte model dtype with kv_cache_dtype=auto")
    if type(runtime.get("block_size")) is not int or runtime["block_size"] < 1:
        raise ValueError("runtime.block_size must be positive")
    args = runtime.get("extra_args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("runtime.extra_args must be an argument list")
    index = 0
    while index < len(args):
        option = args[index]
        if (
            option in VALUE_OPTIONS
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
        ):
            index += 2
        elif option in FLAG_OPTIONS:
            index += 1
        else:
            raise ValueError(f"unsupported or incomplete runtime option: {option}")
    for name, value in runtime.get("environment", {}).items():
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            or name in MANAGED_ENV
            or not (name.startswith(ENV_PREFIXES) or name in ("LD_LIBRARY_PATH", "PYTHONPATH"))
            or any(
                word in name
                for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "SSH", "SKIP_COMPAT")
            )
            or not isinstance(value, str)
            or any(c in value for c in "\r\n\0")
        ):
            raise ValueError(f"unsupported runtime environment field: {name}")


def ipc_cache_off(ipc_cache: str | None) -> bool:
    """Return whether UCX reads this UCX_CUDA_IPC_CACHE value as false."""
    return ipc_cache is not None and (ipc_cache.lower() in ("n", "no") or ipc_cache == "0")


def releases_peers(ucx_version: str | None, ipc_cache: str | None) -> bool:
    """Return whether UCX unmaps a removed peer's CUDA IPC memory with this cache setting."""
    try:
        version = tuple(int(part) for part in (ucx_version or "").split(".")[:2])
    except ValueError:
        return False
    return version >= UCX_PEER_RELEASE and ipc_cache_off(ipc_cache)


def kv_events_policy(args: list[str], socket_dir: Path, engine_dir: str) -> dict | None:
    """Select cache-event publication from the backend's own launch settings.

    Publication follows prefix caching unless `--kv-events-config` disables it.
    """
    if {"--enable-prefix-caching", "--no-enable-prefix-caching"} <= set(args):
        raise ValueError("runtime.extra_args must select prefix caching at most once")
    supplied = [args[i + 1] for i, arg in enumerate(args) if arg == "--kv-events-config"]
    if len(supplied) > 1:
        raise ValueError("runtime.extra_args must supply --kv-events-config at most once")
    if supplied:
        try:
            value = json.loads(supplied[0])
        except ValueError:
            value = None
        if value != {"enable_kv_cache_events": False}:
            raise ValueError(
                "runtime.extra_args --kv-events-config may only set "
                '{"enable_kv_cache_events": false}; the launcher selects event endpoints'
            )
        return None
    if "--no-enable-prefix-caching" in args:
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
    """Create or reuse the plan's socket directory and its parent, private to this user."""
    if plan.get("kv_events") is None:
        return
    directory = Path(plan["kv_events"]["socket_dir"])
    for path in (directory.parent, directory):
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError(f"{path} must be a directory private to the launching user")


def container_options(plan: dict) -> list[str]:
    """Create the plan's socket directory and return its shared Docker options."""
    kv_events_directory(plan)
    return list(plan["common"])


def remove_kv_events_directory(plan: dict) -> None:
    """Remove the plan's socket directory once its engine process has stopped."""
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


def requires_ds_conv_state_layout(model_dir: Path) -> bool:
    """Detect checkpoint metadata that uses convolutional SSM transfer state."""
    return ds_conv_state_layout_required(json.loads((model_dir / "config.json").read_text()))


def ds_conv_state_layout_required(model: dict) -> bool:
    """Detect convolutional SSM transfer state in parsed checkpoint configuration."""
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


def append_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(data)


def env_file_name(plan: dict) -> str:
    """Return the launch directory's environment file for the plan's backend."""
    return "engine.env" if plan.get("backend") == "native" else "container.env"


def read_env(path: Path) -> dict[str, str]:
    """Read a KEY=VALUE environment file."""
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def build(
    record: dict, env: dict[str, str], output: Path, *, backend: str = "container"
) -> tuple[dict, dict[str, str]]:
    """Resolve one engine record into the same vLLM contract for either launch backend."""
    if backend not in {"container", "native"}:
        raise ValueError(f"unsupported engine launch backend: {backend}")
    runtime = record["runtime"]
    validate_runtime(runtime)
    role = record["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("select an engine role")
    node = role.split("-")[1]
    image = env["NARWHAL_ENGINE_IMAGE"] if backend == "container" else ""
    if backend == "container" and not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", image
    ):
        raise ValueError("NARWHAL_ENGINE_IMAGE must be an immutable image ID or registry digest")
    port = int(env["NARWHAL_ENGINE_PORT"])
    side_port = int(env["NARWHAL_NIXL_SIDE_CHANNEL_PORT"])
    attest_port = int(env["NARWHAL_ATTEST_PORT"])
    if (
        any(not 1 <= p <= 65535 for p in (port, side_port, attest_port))
        or len({port, side_port, attest_port}) != 3
    ):
        raise ValueError("engine, attestation and NIXL ports must be distinct valid TCP ports")
    source = env[f"NARWHAL_NODE_{node}_IP"]
    endpoint = urlsplit(env[f"NARWHAL_NODE_{node}_URL"])
    if endpoint.scheme != "http" or endpoint.port != port or not endpoint.hostname:
        raise ValueError("the engine URL must select HTTP and NARWHAL_ENGINE_PORT")
    values = dict(runtime.get("environment", {}))
    values.update(record["environment"])
    gpu_transport = record["transfer"].get(
        "gpu_tls", "rocm" if record["gpu_visibility_env"] == "ROCR_VISIBLE_DEVICES" else "cuda"
    )
    allowed_gpu_tls = (
        {"rocm"}
        if record["gpu_visibility_env"] == "ROCR_VISIBLE_DEVICES"
        else {"cuda", "cuda_copy"}
    )
    if gpu_transport not in allowed_gpu_tls:
        raise ValueError(f"{role}: transfer.gpu_tls is incompatible with the GPU runtime")
    net_transport = "tcp" if record["transfer"]["transport"] == "ucx_tcp" else "rc"
    values.update(
        {
            "UCX_TLS": f"{net_transport},sm,self,{gpu_transport}",
            "NIXL_HOST_IP": source,
            "VLLM_NIXL_SIDE_CHANNEL_HOST": source,
            "VLLM_NIXL_SIDE_CHANNEL_PORT": str(side_port),
            "UCX_TCP_PORT_RANGE": env["NARWHAL_UCX_TCP_PORT_RANGE"],
        }
    )
    if env.get("NARWHAL_ENGINE_API_KEY"):
        values["VLLM_API_KEY"] = env["NARWHAL_ENGINE_API_KEY"]
    if any(any(c in value for c in "\r\n\0") for value in values.values()):
        raise ValueError("engine environment values must fit one line")
    hook_root = "/narwhal-hooks" if backend == "container" else str(output / "hook")
    values["PYTHONPATH"] = hook_root + (
        ":" + values["PYTHONPATH"] if values.get("PYTHONPATH") else ""
    )
    model_dir = str(Path(env["NARWHAL_MODEL_DIR"]).resolve())
    model_ref = (
        os.path.abspath(env.get("NARWHAL_MODEL_PATH", model_dir))
        if backend == "native"
        else "/model"
    )
    if (
        backend == "native"
        and model_ref.endswith(".gguf")
        and not runtime["expected_packages"].get("vllm-gguf-plugin")
    ):
        raise ValueError("GGUF launch requires a pinned vllm-gguf-plugin package")
    if backend == "container" and ("," in model_dir or "," in str(output)):
        raise ValueError("container bind-mount paths must use comma-free names")
    name = f"narwhal-{role}-{uuid.uuid4().hex[:12]}"
    socket_dir = KV_EVENTS_ROOT / f"narwhal-{os.geteuid()}" / name
    kv_events = kv_events_policy(
        runtime.get("extra_args", []),
        socket_dir,
        KV_EVENTS_MOUNT if backend == "container" else str(socket_dir),
    )
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
        f"type=bind,src={model_dir},dst=/model,readonly",
        "--mount",
        f"type=bind,src={output / 'cache'},dst=/root/.cache",
        "--mount",
        f"type=bind,src={output / 'hook'},dst=/narwhal-hooks,readonly",
    ]
    if backend == "container":
        if kv_events is not None:
            common.extend(["--mount", f"type=bind,src={socket_dir},dst={KV_EVENTS_MOUNT}"])
        for device in record["accelerator_devices"] + record["transfer"]["devices"]:
            common.extend(["--device", device])
        if record["gpu_visibility_env"] == "CUDA_VISIBLE_DEVICES":
            common.extend(["--gpus", "all"])
            # UCX CUDA IPC tells peers apart by PID; colocated engines need distinct host PIDs.
            visible = record["environment"]["CUDA_VISIBLE_DEVICES"].split(",")
            if len(visible) > len(record["gpu_ids"]):
                common.extend(["--pid", "host"])
        else:
            common.extend(["--security-opt", "seccomp=unconfined"])
    else:
        common = []
    # CUDA IPC peers keep this engine's KV memory mapped until vLLM evicts it.
    ipc_peers = gpu_transport == "cuda" and (
        record.get("shared_device") is not None
        or len(values.get("CUDA_VISIBLE_DEVICES", "").split(",")) > len(record["gpu_ids"])
    )
    # With the UCX IPC cache off, vLLM's eviction releases a stopped peer's memory.
    evict_peers = ipc_peers and ipc_cache_off(values.get("UCX_CUDA_IPC_CACHE"))
    connector = {
        "kv_connector": "NixlConnector",
        "kv_role": "kv_both",
        "kv_load_failure_policy": "fail",
        "kv_connector_extra_config": {
            "backends": ["UCX"],
            "enforce_handshake_compat": True,
            **({"engine_ttl": ENGINE_TTL_S} if evict_peers else {}),
        },
    }
    args = [
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        model_ref,
        "--served-model-name",
        env["NARWHAL_ENGINE_MODEL_NAME"],
        "--host",
        endpoint.hostname
        if backend == "native"
        else ("::" if ":" in endpoint.hostname else "0.0.0.0"),
        "--port",
        str(port),
        "--tensor-parallel-size",
        str(record["tensor_parallel_size"]),
        "--dtype",
        runtime["model_dtype"],
        "--kv-cache-dtype",
        runtime["kv_cache_dtype"],
        "--block-size",
        str(runtime["block_size"]),
        *(
            []
            if kv_events is None
            else [
                "--kv-events-config",
                json.dumps(
                    {
                        "enable_kv_cache_events": True,
                        "publisher": "zmq",
                        "endpoint": kv_events["endpoint"],
                        "replay_endpoint": kv_events["replay_endpoint"],
                    }
                ),
            ]
        ),
        "--kv-transfer-config",
        json.dumps(connector),
        *runtime.get("extra_args", []),
    ]
    shared = record.get("shared_device")
    if shared is not None:
        if (
            record["gpu_visibility_env"] != "CUDA_VISIBLE_DEVICES"
            or len(record["gpu_ids"]) != 1
            or record["tensor_parallel_size"] != 1
        ):
            raise ValueError(f"{role}: shared allocation requires one CUDA GPU and TP=1")
        memory_args = [i for i, arg in enumerate(args) if arg == "--gpu-memory-utilization"]
        if len(memory_args) != 1 or memory_args[0] + 1 >= len(args):
            raise ValueError(f"{role}: shared GPU launch requires one vLLM memory setting")
        try:
            budget_matches = float(args[memory_args[0] + 1]) == shared["gpu_memory_utilization"]
        except (ValueError, TypeError):
            budget_matches = False
        if not budget_matches:
            raise ValueError(f"{role}: vLLM memory setting differs from shared GPU budget")
    plan = {
        "role": role,
        "image": image,
        "model_dir": model_dir,
        "name": name,
        "common": common,
        "args": args,
        "connector": connector,
        "kv_events": kv_events,
        "expected_packages": runtime["expected_packages"],
        "endpoint": endpoint.geturl(),
        "attestation_port": attest_port,
        "side_channel_port": side_port,
        "ucx_tls": values["UCX_TLS"],
        "cuda_ipc_peers": ipc_peers,
        **({"shared_device": shared} if shared is not None else {}),
        "revision": env["NARWHAL_DEPLOYMENT_REVISION"],
    }
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


def prepare(output: Path, env: dict[str, str], *, backend: str = "container") -> None:
    """Pin model, hook and launch inputs before starting an engine."""
    source = Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"])
    record = json.loads(source.read_text())
    plan, values = build(record, env, output.resolve(), backend=backend)
    if digest(Path(env["NARWHAL_MODEL_DIR"]) / "config.json") != env["NARWHAL_MODEL_CONFIG_SHA256"]:
        raise ValueError("model config differs from its supplied hash")
    hook_source = Path(env["NARWHAL_CACHE_CAPTURE_HOOK"])
    hook_sha = env["NARWHAL_CACHE_CAPTURE_HOOK_SHA256"]
    if digest(hook_source) != hook_sha:
        raise ValueError("cache capture hook differs from its delivered hash")
    output.mkdir(mode=0o700, parents=True)
    (output / "cache").mkdir(mode=0o700)
    (output / "hook").mkdir(mode=0o700)
    kv_events_directory(plan)
    write_private(output / "hook/sitecustomize.py", hook_source.read_text())
    write_private(output / "hook/launch_engine.py", LAUNCHER.read_text())
    env_file = "container.env" if backend == "container" else "engine.env"
    write_private(output / env_file, "".join(f"{k}={v}\n" for k, v in sorted(values.items())))
    plan.update(
        env_sha256=digest(output / env_file),
        launch_sha256=digest(source),
        launcher_sha256=digest(LAUNCHER),
        cache_capture_sha256=hook_sha,
        model_config_sha256=env["NARWHAL_MODEL_CONFIG_SHA256"],
    )
    if backend == "native" and Path(plan["model_path"]).is_file():
        plan["model_sha256"] = digest(Path(plan["model_path"]))
    write_private(output / "launch.json", json.dumps(plan, indent=2) + "\n")
    write_private(output / "hook/launch.json", (output / "launch.json").read_text())
    for name in ("sitecustomize.py", "launch_engine.py", "launch.json"):
        (output / "hook" / name).chmod(0o644)
    (output / "hook").chmod(0o755)
    check_name = "image" if backend == "container" else "native runtime"
    print(f"Prepared {record['role']}; review launch.json and run the {check_name} check.")


def load(run: Path) -> dict:
    plan = json.loads((run / "launch.json").read_text())
    if not isinstance(plan, dict):
        raise ValueError(f"{run / 'launch.json'}: expected a JSON object")
    if digest(run / env_file_name(plan)) != plan.get("env_sha256"):
        raise ValueError("engine environment changed; prepare a fresh launch directory")
    return plan
