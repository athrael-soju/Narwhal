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

from .backend import EngineLauncher, launcher, plan_launcher
from .runtime import digest, write_private

# Short root for per-plan event sockets; launch directories can exceed the socket path limit.
KV_EVENTS_ROOT = Path("/tmp")
KV_EVENTS_MOUNT = "/narwhal-kv-events"
KV_EVENTS_SOCKETS = {"endpoint": "events.sock", "replay_endpoint": "replay.sock"}
# sockaddr_un holds 108 bytes, including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 107
# First UCX release that unmaps CUDA IPC rkeys when the transfer library removes a remote agent.
UCX_PEER_RELEASE = (1, 22)


def validate_runtime(runtime: dict) -> None:
    launcher(runtime.get("backend")).validate_runtime(runtime)


def ipc_cache_off(ipc_cache: str | None) -> bool:
    return ipc_cache is not None and (ipc_cache.lower() in ("n", "no") or ipc_cache == "0")


def releases_peers(ucx_version: str | None, ipc_cache: str | None) -> bool:
    try:
        version = tuple(int(part) for part in (ucx_version or "").split(".")[:2])
    except ValueError:
        return False
    return version >= UCX_PEER_RELEASE and ipc_cache_off(ipc_cache)


def kv_events_policy(
    engine: EngineLauncher, args: list[str], socket_dir: Path, engine_dir: str
) -> dict | None:
    if not engine.publishes_kv_events(args):
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


def append_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(data)


def env_file_name(plan: dict) -> str:
    return "engine.env" if plan.get("backend") == "native" else "container.env"


def read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def build(
    record: dict, env: dict[str, str], output: Path, *, backend: str = "container"
) -> tuple[dict, dict[str, str]]:
    if backend not in {"container", "native"}:
        raise ValueError(f"unsupported engine launch backend: {backend}")
    runtime = record["runtime"]
    engine = launcher(runtime.get("backend"))
    engine.validate_runtime(runtime)
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
    side_port = int(env[engine.side_channel_port_env])
    attest_port = int(env["NARWHAL_ATTEST_PORT"])
    if (
        any(not 1 <= p <= 65535 for p in (port, side_port, attest_port))
        or len({port, side_port, attest_port}) != 3
    ):
        raise ValueError(
            f"engine, attestation and {engine.side_channel} ports must be distinct valid TCP ports"
        )
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
            "UCX_TCP_PORT_RANGE": env["NARWHAL_UCX_TCP_PORT_RANGE"],
            **engine.engine_env(source, side_port, env.get("NARWHAL_ENGINE_API_KEY", "")),
        }
    )
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
    if backend == "native":
        engine.check_model(runtime, model_ref)
    if backend == "container" and ("," in model_dir or "," in str(output)):
        raise ValueError("container bind-mount paths must use comma-free names")
    name = f"narwhal-{role}-{uuid.uuid4().hex[:12]}"
    socket_dir = KV_EVENTS_ROOT / f"narwhal-{os.geteuid()}" / name
    kv_events = kv_events_policy(
        engine,
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
    # CUDA IPC peers keep this engine's KV memory mapped until the engine evicts it.
    ipc_peers = gpu_transport == "cuda" and (
        record.get("shared_device") is not None
        or len(values.get("CUDA_VISIBLE_DEVICES", "").split(",")) > len(record["gpu_ids"])
    )
    # With the UCX IPC cache off, eviction releases a stopped peer's memory.
    evict_peers = ipc_peers and ipc_cache_off(values.get("UCX_CUDA_IPC_CACHE"))
    args, connector = engine.serve(
        record,
        model=model_ref,
        served_name=env["NARWHAL_ENGINE_MODEL_NAME"],
        host=endpoint.hostname
        if backend == "native"
        else ("::" if ":" in endpoint.hostname else "0.0.0.0"),
        port=port,
        kv_events=kv_events,
        evict_peers=evict_peers,
    )
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
    source = Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"])
    record = json.loads(source.read_text())
    plan, values = build(record, env, output.resolve(), backend=backend)
    if digest(Path(env["NARWHAL_MODEL_DIR"]) / "config.json") != env["NARWHAL_MODEL_CONFIG_SHA256"]:
        raise ValueError("model config differs from its supplied hash")
    hook_source = Path(env["NARWHAL_CACHE_CAPTURE_HOOK"])
    hook_sha = env["NARWHAL_CACHE_CAPTURE_HOOK_SHA256"]
    if digest(hook_source) != hook_sha:
        raise ValueError("cache capture hook differs from its delivered hash")
    script = plan_launcher(plan).runtime_script
    output.mkdir(mode=0o700, parents=True)
    (output / "cache").mkdir(mode=0o700)
    (output / "hook").mkdir(mode=0o700)
    kv_events_directory(plan)
    write_private(output / "hook/sitecustomize.py", hook_source.read_text())
    write_private(output / "hook/launch_engine.py", script.read_text())
    env_file = "container.env" if backend == "container" else "engine.env"
    write_private(output / env_file, "".join(f"{k}={v}\n" for k, v in sorted(values.items())))
    plan.update(
        env_sha256=digest(output / env_file),
        launch_sha256=digest(source),
        launcher_sha256=digest(script),
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
