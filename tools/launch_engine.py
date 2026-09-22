"""Prepare, inspect and launch one pinned vLLM/NIXL container from its engine record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

VALUE_OPTIONS = {
    "--max-model-len",
    "--gpu-memory-utilization",
    "--max-num-batched-tokens",
    "--max-num-seqs",
    "--reasoning-parser",
    "--attention-backend",
}
FLAG_OPTIONS = {
    "--trust-remote-code",
    "--language-model-only",
    "--enforce-eager",
    "--async-scheduling",
    "--no-disable-hybrid-kv-cache-manager",
    "--disable-hybrid-kv-cache-manager",
}
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


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)


def build(record: dict, env: dict[str, str], output: Path) -> tuple[dict, dict[str, str]]:
    runtime = record["runtime"]
    validate_runtime(runtime)
    role = record["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("select an engine role")
    node = role.split("-")[1]
    image = env["NARWHAL_ENGINE_IMAGE"]
    if not re.fullmatch(r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", image):
        raise ValueError("NARWHAL_ENGINE_IMAGE must be an immutable image ID or registry digest")
    port = int(env["NARWHAL_ENGINE_PORT"])
    side_port = int(env["NARWHAL_NIXL_SIDE_CHANNEL_PORT"])
    if any(not 1 <= p <= 65535 for p in (port, side_port)) or port == side_port:
        raise ValueError("engine and NIXL ports must be distinct valid TCP ports")
    source = env[f"NARWHAL_NODE_{node}_IP"]
    endpoint = urlsplit(env[f"NARWHAL_NODE_{node}_URL"])
    if endpoint.scheme != "http" or endpoint.port != port or not endpoint.hostname:
        raise ValueError("the engine URL must select HTTP and NARWHAL_ENGINE_PORT")
    values = dict(runtime.get("environment", {}))
    values.update(record["environment"])
    gpu_transport = "rocm" if record["gpu_visibility_env"] == "ROCR_VISIBLE_DEVICES" else "cuda"
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
        raise ValueError("container environment values must fit one line")
    model_dir = str(Path(env["NARWHAL_MODEL_DIR"]).resolve())
    if "," in model_dir or "," in str(output):
        raise ValueError("container bind-mount paths must use comma-free names")
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
    ]
    for device in record["accelerator_devices"] + record["transfer"]["devices"]:
        common.extend(["--device", device])
    if record["gpu_visibility_env"] == "CUDA_VISIBLE_DEVICES":
        common.extend(["--gpus", "all"])
    else:
        common.extend(["--security-opt", "seccomp=unconfined"])
    connector = {
        "kv_connector": "NixlConnector",
        "kv_role": "kv_both",
        "kv_load_failure_policy": "fail",
        "kv_connector_extra_config": {"backends": ["UCX"]},
    }
    args = [
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        "/model",
        "--served-model-name",
        env["NARWHAL_ENGINE_MODEL_NAME"],
        "--host",
        "::" if ":" in endpoint.hostname else "0.0.0.0",
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
        "--no-enable-prefix-caching",
        "--kv-transfer-config",
        json.dumps(connector),
        *runtime.get("extra_args", []),
    ]
    return {
        "role": role,
        "image": image,
        "name": f"narwhal-{role}-{uuid.uuid4().hex[:12]}",
        "common": common,
        "args": args,
        "connector": connector,
        "expected_packages": runtime["expected_packages"],
        "endpoint": endpoint.geturl(),
        "revision": env["NARWHAL_DEPLOYMENT_REVISION"],
    }, values


def prepare(output: Path, env: dict[str, str]) -> None:
    source = Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"])
    record = json.loads(source.read_text())
    plan, values = build(record, env, output.resolve())
    if digest(Path(env["NARWHAL_MODEL_DIR"]) / "config.json") != env["NARWHAL_MODEL_CONFIG_SHA256"]:
        raise ValueError("model config differs from its supplied hash")
    output.mkdir(mode=0o700, parents=True)
    (output / "cache").mkdir(mode=0o700)
    write_private(
        output / "container.env", "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    )
    plan.update(
        env_sha256=digest(output / "container.env"),
        launch_sha256=digest(source),
        launcher_sha256=digest(Path(__file__)),
    )
    write_private(output / "launch.json", json.dumps(plan, indent=2) + "\n")
    print(f"Prepared {record['role']}; review launch.json and run the image check.")


def docker(command: list[str], run: Path, log: str) -> str:
    result = subprocess.run(["docker", *command], capture_output=True, text=True)
    with (run / log).open("a") as output:
        output.write(
            json.dumps({"command": ["docker", *command], "exit": result.returncode}) + "\n"
        )
        output.write(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError(f"Docker command failed; inspect {log}")
    return result.stdout.strip()


def load(run: Path) -> dict:
    plan = json.loads((run / "launch.json").read_text())
    if digest(run / "container.env") != plan["env_sha256"]:
        raise ValueError("container environment changed; prepare a fresh launch directory")
    return plan


def check(run: Path, plan: dict) -> None:
    inspection = json.loads(docker(["image", "inspect", plan["image"]], run, "image-check.log"))[0]
    expected = plan["image"]
    if expected.startswith("sha256:"):
        matches = inspection["Id"] == expected
    else:
        matches = expected in inspection.get("RepoDigests", [])
    if not matches:
        raise ValueError("local image identity differs from the launch plan")
    script = """import importlib.metadata as m, json
expected = json.loads(__import__('sys').argv[1])
observed = {name: m.version(name) for name in expected}
print(json.dumps(observed))
assert observed == expected, 'image package versions differ'
from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
config = KVTransferConfig(**json.loads(__import__('sys').argv[2]))
connector = KVConnectorFactory.get_connector_class(config)
print(json.dumps({'connector': connector.__module__ + '.' + connector.__name__}))
"""
    docker(
        [
            "run",
            "--rm",
            *plan["common"],
            "--entrypoint",
            "python3",
            plan["image"],
            "-c",
            script,
            json.dumps(plan["expected_packages"]),
            json.dumps(plan["connector"]),
        ],
        run,
        "image-check.log",
    )
    marker = run / "checked.json"
    value = json.dumps({"plan_sha256": digest(run / "launch.json"), "image_id": inspection["Id"]})
    if marker.exists():
        if marker.read_text() != value:
            raise ValueError("image check changed; prepare a fresh launch directory")
    else:
        write_private(marker, value)
    print("Image identity, package pins and connector import passed.")


def start(run: Path, plan: dict) -> None:
    checked = json.loads((run / "checked.json").read_text())
    if checked["plan_sha256"] != digest(run / "launch.json"):
        raise ValueError("launch plan changed after its image check")
    if (run / "container.id").exists():
        raise ValueError("launch already has a container; inspect its recorded ID before recovery")
    cid = docker(
        [
            "create",
            "--name",
            plan["name"],
            *plan["common"],
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
    write_private(run / "container.id", cid + "\n")
    docker(["start", cid], run, "launch.log")
    print("Container started; follow its logs and verify the HTTP endpoints.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare").add_argument("--out", type=Path, required=True)
    for command in ("check", "start"):
        sub.add_parser(command).add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "prepare":
            prepare(args.out, dict(os.environ))
        else:
            run = args.run.resolve()
            plan = load(run)
            (check if args.command == "check" else start)(run, plan)
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, FileExistsError):
            parser.exit(1, "Launch directory exists; choose a fresh output path.\n")
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            parser.exit(1, f"{error}\n")
        parser.exit(
            1, "Check the role environment, runtime fields, artifact paths and preceding gate.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
