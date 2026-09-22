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
        model_config_sha256=env["NARWHAL_MODEL_CONFIG_SHA256"],
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
from vllm.version import __version__ as api_version
config = KVTransferConfig(**json.loads(__import__('sys').argv[2]))
connector = KVConnectorFactory.get_connector_class(config)
print(json.dumps({'connector': connector.__module__ + '.' + connector.__name__}))
print('NARWHAL_IMAGE_RUNTIME=' + json.dumps({'vllm_api_version': api_version}))
"""
    output = docker(
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
    prefix = "NARWHAL_IMAGE_RUNTIME="
    records = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(records) != 1:
        raise ValueError("image check requires one runtime version record; inspect image-check.log")
    api_version = records[0].get("vllm_api_version")
    if not isinstance(api_version, str) or not api_version.strip():
        raise ValueError("image check returned an invalid API version; inspect image-check.log")
    marker = run / "checked.json"
    value = json.dumps(
        {
            "plan_sha256": digest(run / "launch.json"),
            "image_id": inspection["Id"],
            "vllm_api_version": api_version,
        }
    )
    if marker.exists():
        if marker.read_text() != value:
            raise ValueError("image check changed; prepare a fresh launch directory")
    else:
        write_private(marker, value)
    print("Image identity, package pins and connector import passed.")


def require_checked(run: Path, plan: dict) -> None:
    checked = json.loads((run / "checked.json").read_text())
    if checked["plan_sha256"] != digest(run / "launch.json"):
        raise ValueError("launch plan changed after its image check")


def cache_groups(groups: list) -> list[dict]:
    """Retain padded per-layer pages, including state and boundary block allowances."""
    result = []
    for group in groups:
        spec = group.kv_cache_spec
        layers = getattr(spec, "kv_cache_specs", None)
        layers = layers if layers is not None else dict.fromkeys(group.layer_names, spec)
        for name, layer in layers.items():
            kind = type(layer).__name__
            extra = 0
            if kind == "MambaSpec":
                # Bound an aligned state's previous/current pages and checkpoint slots.
                extra = (
                    1
                    + layer.num_speculative_blocks
                    + getattr(layer, "num_prefill_checkpoint_blocks", 0)
                )
            elif kind in ("SlidingWindowSpec", "ChunkedLocalAttentionSpec"):
                extra = 1
            elif kind not in ("FullAttentionSpec", "MLAAttentionSpec", "AttentionSpec"):
                raise ValueError(f"Cache sizing requires a page bound for {kind}")
            result.append(
                {
                    "layer": name,
                    "kind": kind,
                    "block_tokens": layer.block_size,
                    "page_bytes": layer.page_size_bytes,
                    "extra_blocks": extra,
                }
            )
    return result


def runtime_config(plan: dict):
    """Resolve the serving arguments inside the image before creating model workers."""
    from vllm.engine.arg_utils import EngineArgs
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    if digest(Path("/model/config.json")) != plan["model_config_sha256"]:
        raise ValueError("model config changed since launch preparation")
    # EngineArgs owns the model options; HTTP listener options belong to the API server.
    arguments = list(plan["args"][2:])
    for option in ("--host", "--port"):
        index = arguments.index(option)
        del arguments[index : index + 2]
    parser = EngineArgs.add_cli_args(FlexibleArgumentParser())
    return EngineArgs.from_cli_args(parser.parse_args(arguments)).create_engine_config()


def runtime_model_dimensions(plan_path: Path) -> dict:
    """Capture the model getters consumed by the NIXL compatibility hash."""
    plan = json.loads(plan_path.read_text())
    model = runtime_config(plan).model_config
    methods = {
        "head_size": "get_head_size",
        "kv_heads": "get_total_num_kv_heads",
        "hidden_layers": "get_total_num_hidden_layers",
    }
    values = {field: getattr(model, method)() for field, method in methods.items()}
    if any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("model contract getters must return positive integers")
    return {
        "contract": values,
        "sources": {field: f"ModelConfig.{method}()" for field, method in methods.items()},
        "use_mla": model.use_mla,
        "model_config_sha256": plan["model_config_sha256"],
        "plan_sha256": digest(plan_path),
        "image": plan["image"],
        "revision": plan["revision"],
    }


def model_dimensions(run: Path, plan: dict) -> None:
    require_checked(run, plan)
    if digest(Path(__file__)) != plan["launcher_sha256"]:
        raise ValueError("launcher changed; prepare and check a fresh launch plan")
    destination = run / "model-dimensions.json"
    if destination.exists():
        raise ValueError("model dimensions exist; retain the capture and use a fresh plan")
    output = docker(
        [
            "run",
            "--rm",
            *plan["common"],
            "--mount",
            f"type=bind,src={Path(__file__).resolve()},dst=/narwhal-inspect.py,readonly",
            "--mount",
            f"type=bind,src={run / 'launch.json'},dst=/narwhal-launch.json,readonly",
            "--entrypoint",
            "python3",
            plan["image"],
            "/narwhal-inspect.py",
            "_model-dimensions",
            "--plan",
            "/narwhal-launch.json",
        ],
        run,
        "model-dimensions.log",
    )
    prefix = "NARWHAL_MODEL_DIMENSIONS="
    records = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(records) != 1 or records[0]["plan_sha256"] != digest(run / "launch.json"):
        raise ValueError(
            "model dimension capture must match this plan; inspect model-dimensions.log"
        )
    write_private(destination, json.dumps(records[0], indent=2) + "\n")
    print("Model contract dimensions captured in model-dimensions.json.")


def runtime_cache_probe(plan_path: Path) -> None:
    """Run inside the pinned image until vLLM resolves each worker's cache allocation."""
    from vllm.v1.engine.core import EngineCore
    from vllm.v1.executor.abstract import Executor

    plan = json.loads(plan_path.read_text())
    config = runtime_config(plan)
    workers = []
    captured = []

    class SizingComplete(Exception):
        pass

    class ProbeExecutor(Executor.get_class(config)):
        def __init__(self, vllm_config):
            workers.append(self)
            super().__init__(vllm_config)

        def initialize_from_config(self, configs):
            for rank, allocation in enumerate(configs):
                captured.append({"rank": rank, "layers": cache_groups(allocation.kv_cache_groups)})
            # EngineCore has loaded/profiled the model and resolved padding at this point.
            raise SizingComplete

    try:
        EngineCore(config, executor_class=ProbeExecutor, log_stats=False)
        raise ValueError("runtime cache allocation hook was skipped")
    except SizingComplete:
        pass
    finally:
        for worker in workers:
            worker.shutdown()
    if len(captured) != config.parallel_config.tensor_parallel_size:
        raise ValueError("cache sizing requires one allocation record per TP rank")
    value = {
        "schema_version": 1,
        "sizing": "runtime_padded_page_upper_bound",
        "image": plan["image"],
        "expected_packages": plan["expected_packages"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
        "launch_config_sha256": plan["launch_sha256"],
        "plan_sha256": digest(plan_path),
        "launcher_sha256": plan["launcher_sha256"],
        "ranks": captured,
    }
    write_private(
        plan_path.parent / "cache-layout.pending.json", json.dumps(value, indent=2) + "\n"
    )


def measure_cache(run: Path, plan: dict) -> None:
    require_checked(run, plan)
    if digest(Path(__file__)) != plan["launcher_sha256"]:
        raise ValueError("launcher changed; prepare and check a fresh launch plan")
    if any(
        (run / name).exists() for name in ("container.id", "cache-probe.id", "cache-layout.json")
    ):
        raise ValueError("launch directory has a container or sizing record; use a fresh plan")
    cid = docker(
        [
            "create",
            "--name",
            plan["name"] + "-cache-probe",
            *plan["common"],
            "--mount",
            f"type=bind,src={Path(__file__).resolve()},dst=/narwhal-probe.py,readonly",
            "--mount",
            f"type=bind,src={run},dst=/narwhal-probe",
            "--entrypoint",
            "python3",
            plan["image"],
            "/narwhal-probe.py",
            "_cache-probe",
            "--plan",
            "/narwhal-probe/launch.json",
        ],
        run,
        "cache-probe.log",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("Docker returned an invalid probe ID; inspect cache-probe.log")
    write_private(run / "cache-probe.id", cid + "\n")
    docker(["start", "--attach", cid], run, "cache-probe.log")
    state = json.loads(
        docker(["inspect", "--format", "{{json .State}}", cid], run, "cache-probe.log")
    )
    if state["Running"] or state["ExitCode"] != 0:
        raise ValueError("cache probe failed; inspect cache-probe.log and the recorded container")
    value = json.loads((run / "cache-layout.pending.json").read_text())
    if value["plan_sha256"] != digest(run / "launch.json"):
        raise ValueError("cache probe output differs from the launch plan")
    docker(["rm", cid], run, "cache-probe.log")
    write_private(run / "cache-layout.json", json.dumps(value, indent=2) + "\n")
    print("Runtime cache pages captured in cache-layout.json; sizing container removed.")


def start(run: Path, plan: dict) -> None:
    require_checked(run, plan)
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
    sub.add_parser("_cache-probe", help=argparse.SUPPRESS).add_argument(
        "--plan", type=Path, required=True
    )
    sub.add_parser("_model-dimensions", help=argparse.SUPPRESS).add_argument(
        "--plan", type=Path, required=True
    )
    for command in ("check", "measure-cache", "model-dimensions", "start"):
        sub.add_parser(command).add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "prepare":
            prepare(args.out, dict(os.environ))
        elif args.command == "_cache-probe":
            runtime_cache_probe(args.plan)
        elif args.command == "_model-dimensions":
            print("NARWHAL_MODEL_DIMENSIONS=" + json.dumps(runtime_model_dimensions(args.plan)))
        else:
            run = args.run.resolve()
            plan = load(run)
            {
                "check": check,
                "measure-cache": measure_cache,
                "model-dimensions": model_dimensions,
                "start": start,
            }[args.command](run, plan)
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
