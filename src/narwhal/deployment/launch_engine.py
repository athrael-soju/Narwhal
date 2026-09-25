"""Prepare, inspect and launch pinned vLLM/NIXL engines with container or native backends."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

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
    model = json.loads((model_dir / "config.json").read_text())
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


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)


def append_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(data)


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
        for device in record["accelerator_devices"] + record["transfer"]["devices"]:
            common.extend(["--device", device])
        if record["gpu_visibility_env"] == "CUDA_VISIBLE_DEVICES":
            common.extend(["--gpus", "all"])
        else:
            common.extend(["--security-opt", "seccomp=unconfined"])
    else:
        common = []
    connector = {
        "kv_connector": "NixlConnector",
        "kv_role": "kv_both",
        "kv_load_failure_policy": "fail",
        "kv_connector_extra_config": {"backends": ["UCX"], "enforce_handshake_compat": True},
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
        "--no-enable-prefix-caching",
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
        "name": f"narwhal-{role}-{uuid.uuid4().hex[:12]}",
        "common": common,
        "args": args,
        "connector": connector,
        "expected_packages": runtime["expected_packages"],
        "endpoint": endpoint.geturl(),
        "attestation_port": attest_port,
        "side_channel_port": side_port,
        "ucx_tls": values["UCX_TLS"],
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
    write_private(output / "hook/sitecustomize.py", hook_source.read_text())
    write_private(output / "hook/launch_engine.py", Path(__file__).read_text())
    env_file = "container.env" if backend == "container" else "engine.env"
    write_private(output / env_file, "".join(f"{k}={v}\n" for k, v in sorted(values.items())))
    plan.update(
        env_sha256=digest(output / env_file),
        launch_sha256=digest(source),
        launcher_sha256=digest(Path(__file__)),
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


def _docker_owner(run: Path) -> str:
    marker = run / "docker-owner.json"
    if not marker.exists():
        with contextlib.suppress(FileExistsError):
            write_private(marker, json.dumps({"label": uuid.uuid4().hex}))
    owner = json.loads(marker.read_text())["label"]
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("docker-owner.json requires a valid ownership token")
    return owner


def reconcile_docker(
    run: Path,
    owner: str,
    *,
    remove: bool,
    operation: str | None = None,
    targets: tuple[str, ...] = (),
) -> dict:
    """Inspect launch resources and remove only the interrupted operation's containers."""
    from narwhal.deployment import stages

    budget = stages.seconds("NARWHAL_DOCKER_RECONCILE_SECONDS", 30)
    started = time.monotonic()
    report: dict = {
        "budget_seconds": budget,
        "owner": owner,
        "operation": operation,
        "targets": list(targets),
        "removed": [],
        "preserved_resources": [],
        "surviving_resources": [],
        "status": "inspection_required",
    }
    recorded = set()
    for name in ("container.id", "cache-probe.id"):
        path = run / name
        if path.exists():
            cid = path.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{64}", cid):
                recorded.add(cid)

    def invoke(args: list[str]) -> str:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            raise ValueError("Docker reconciliation budget exhausted")
        result = stages.run(
            ["docker", *args],
            stage="docker-reconcile-" + args[0],
            log=run / "docker-reconcile.log",
            timeout=remaining,
        )
        if result.returncode:
            raise ValueError(f"Docker reconciliation {args[0]} exited {result.returncode}")
        return result.stdout.strip()

    try:
        listed = invoke(["ps", "-aq", "--no-trunc", "--filter", f"label=io.narwhal.launch={owner}"])
        ids = recorded | {cid for cid in listed.splitlines() if re.fullmatch(r"[0-9a-f]{64}", cid)}
        report["candidates"] = sorted(ids)
        # The all-container listing permits an absent recorded ID after an earlier cleanup.
        present = set(invoke(["ps", "-aq", "--no-trunc"]).splitlines()) & ids
        owned = []
        cleanup = []
        if present:
            records = json.loads(invoke(["inspect", *sorted(present)]))
            for record in records:
                cid = record["Id"]
                labels = record.get("Config", {}).get("Labels") or {}
                if cid in present and (cid in recorded or labels.get("io.narwhal.launch") == owner):
                    owned.append(cid)
                    if remove and (
                        cid in targets
                        or (
                            operation is not None
                            and labels.get("io.narwhal.operation") == operation
                        )
                    ):
                        cleanup.append(cid)
                else:
                    report.setdefault("refused_resources", []).append(cid)
        report["surviving_resources"] = sorted(owned)
        report["preserved_resources"] = sorted(set(owned) - set(cleanup))
        if cleanup:
            invoke(["rm", "--force", *cleanup])
        remaining = set(invoke(["ps", "-aq", "--no-trunc"]).splitlines())
        report["surviving_resources"] = sorted(set(owned) & remaining)
        if remove:
            report["removed"] = sorted(set(cleanup) - remaining)
        report["status"] = "observed"
    except (OSError, ValueError, KeyboardInterrupt) as error:
        report["error"] = str(error)
    report.update(
        elapsed_seconds=time.monotonic() - started,
        observed_at=time.time(),
        recovery="inspect docker-reconcile-*.json and recorded IDs/ownership label before retrying",
    )
    destination = run / ("docker-reconcile-" + uuid.uuid4().hex[:12] + ".json")
    write_private(destination, json.dumps(report, indent=2) + "\n")
    report["evidence"] = str(destination)
    return report


def docker(command: list[str], run: Path, log: str, *, include_stderr: bool = False) -> str:
    from narwhal.deployment import stages

    owner = _docker_owner(run)
    invocation = list(command)
    operation = uuid.uuid4().hex if command[0] in {"create", "run"} else None
    targets = (
        tuple(value for value in command[1:] if re.fullmatch(r"[0-9a-f]{64}", value))
        if command[0] == "start"
        else ()
    )
    if operation is not None:
        invocation[1:1] = [
            "--label",
            f"io.narwhal.launch={owner}",
            "--label",
            f"io.narwhal.operation={operation}",
        ]
    try:
        result = stages.run(["docker", *invocation], stage="docker-" + command[0], log=run / log)
    except (stages.StageTimeout, stages.StageCancelled) as error:
        try:
            report = reconcile_docker(
                run,
                owner,
                remove=command[0] in {"create", "run", "start"},
                operation=operation,
                targets=targets,
            )
        except OSError as cleanup_error:
            report = {
                "status": "inspection_required",
                "error": str(cleanup_error),
                "recovery": "inspect the recorded container IDs and ownership label",
            }
        error.context["docker_reconciliation"] = report
        error.context["recovery"] = report["recovery"]
        with contextlib.suppress(OSError):
            stages.write_evidence(Path(error.context["evidence"]), error.context)
        raise
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError(
            f"Docker command exited {result.returncode}: "
            f"{detail[-1] if detail else 'empty subprocess output'}; inspect {run / log}"
        )
    return (result.stdout + result.stderr if include_stderr else result.stdout).strip()


def run_runtime_script(run: Path, plan: dict, script: str, arguments: list[str], log: str) -> str:
    """Inspect the checked vLLM environment through its selected backend."""
    from narwhal.deployment import stages

    if plan.get("backend") != "native":
        return docker(
            [
                "run",
                "--rm",
                *plan["common"],
                "--entrypoint",
                "python3",
                plan["image"],
                "-c",
                script,
                *arguments,
            ],
            run,
            log,
        )
    values = dict(line.split("=", 1) for line in (run / "engine.env").read_text().splitlines())
    if (run / log).exists():
        raise FileExistsError(17, "inspection log already exists", str(run / log))
    result = stages.run(
        [plan["python_executable"], "-c", script, *arguments],
        env={**os.environ, **values, "NARWHAL_CAPTURE_CACHE": "0"},
        stage="native-runtime-inspection",
        log=run / log,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise ValueError(
            f"native runtime inspection exited {result.returncode}: "
            f"{detail[-1] if detail else 'empty stderr'}; inspect {run / log}"
        )
    return result.stdout


def load(run: Path) -> dict:
    plan = json.loads((run / "launch.json").read_text())
    if not isinstance(plan, dict):
        raise ValueError(f"{run / 'launch.json'}: expected a JSON object")
    env_file = "engine.env" if plan.get("backend") == "native" else "container.env"
    if digest(run / env_file) != plan.get("env_sha256"):
        raise ValueError("engine environment changed; prepare a fresh launch directory")
    return plan


def check(run: Path, plan: dict) -> None:
    """Check the pinned runtime, connector and tokenizer for the selected backend."""
    from narwhal.deployment import stages

    native = plan.get("backend") == "native"
    env_file = "engine.env" if native else "container.env"
    log = "runtime-check.log" if native else "image-check.log"
    if (run / "checked.json").exists():
        require_checked(run, plan)
    append_private(
        run / log,
        "\n"
        + json.dumps(
            {"check_attempt": uuid.uuid4().hex, "plan_sha256": digest(run / "launch.json")}
        )
        + "\n",
    )
    default_tokenizer = plan["model_dir"] if native else "/model"
    tokenizer_path = default_tokenizer
    for index, argument in enumerate(plan["args"]):
        if argument == "--tokenizer":
            tokenizer_path = plan["args"][index + 1]
    trust_remote_code = "--trust-remote-code" in plan["args"]
    if (
        requires_remote_code(
            Path(plan["model_dir"]), include_tokenizer=tokenizer_path == default_tokenizer
        )
        and not trust_remote_code
    ):
        raise ValueError("Model metadata requires --trust-remote-code in the launch record")
    ds_required = requires_ds_conv_state_layout(Path(plan["model_dir"]))
    if (
        ds_required
        and "VLLM_SSM_CONV_STATE_LAYOUT=DS" not in (run / env_file).read_text().splitlines()
    ):
        raise ValueError("Convolutional SSM transfer requires VLLM_SSM_CONV_STATE_LAYOUT=DS")
    if native:
        model_path = Path(plan["model_path"])
        if not model_path.exists():
            raise ValueError(f"native model path is missing: {model_path}")
        if plan.get("model_sha256") and digest(model_path) != plan["model_sha256"]:
            raise ValueError("native model file changed after launch preparation")
        inspection = None
    else:
        inspection = json.loads(
            docker(["image", "inspect", plan["image"]], run, "image-check.log")
        )[0]
        expected = plan["image"]
        if expected.startswith("sha256:"):
            matches = inspection["Id"] == expected
        else:
            matches = expected in inspection.get("RepoDigests", [])
        if not matches:
            raise ValueError("local image identity differs from the launch plan")
    # Resolve tokenizer metadata in the serving namespace, including image-local paths.
    script = (
        "from pathlib import Path\n"
        + inspect.getsource(requires_remote_code)
        + """import importlib.metadata as m, json
expected = json.loads(__import__('sys').argv[1])
observed = {name: m.version(name) for name in expected}
print(json.dumps(observed))
assert observed == expected, 'image package versions differ'
from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
from vllm.version import __version__ as api_version
from transformers import AutoTokenizer
config = KVTransferConfig(**json.loads(__import__('sys').argv[2]))
connector = KVConnectorFactory.get_connector_class(config)
if json.loads(__import__('sys').argv[4]):
    from vllm.model_executor.layers.mamba.mamba_utils import get_conv_state_layout
    assert get_conv_state_layout() == 'DS', 'NIXL convolutional state requires DS layout'
tokenizer_path = __import__('sys').argv[5]
trust_remote_code = json.loads(__import__('sys').argv[3])
if requires_remote_code(Path(tokenizer_path)) and not trust_remote_code:
    raise ValueError('Tokenizer metadata requires --trust-remote-code in the launch record')
tokenizer = AutoTokenizer.from_pretrained(
    tokenizer_path,
    trust_remote_code=trust_remote_code,
    local_files_only=True
)
assert tokenizer is not None, 'checkpoint tokenizer did not initialise'
print(json.dumps({'connector': connector.__module__ + '.' + connector.__name__}))
print('NARWHAL_TOKENIZER_READY=1')
print('NARWHAL_IMAGE_RUNTIME=' + json.dumps({'vllm_api_version': api_version}))
"""
    )
    arguments = [
        "-c",
        script,
        json.dumps(plan["expected_packages"]),
        json.dumps(plan["connector"]),
        json.dumps(trust_remote_code),
        json.dumps(ds_required),
        tokenizer_path,
    ]
    if native:
        values = dict(line.split("=", 1) for line in (run / env_file).read_text().splitlines())
        result = stages.run(
            [plan["python_executable"], *arguments],
            env={**os.environ, **values},
            stage="native-runtime-check",
            log=run / log,
        )
        if result.returncode:
            detail = result.stderr.strip().splitlines()
            raise ValueError(
                f"native runtime check exited {result.returncode}: "
                f"{detail[-1] if detail else 'empty stderr'}; inspect {run / log}"
            )
        output = result.stdout
    else:
        output = docker(
            [
                "run",
                "--rm",
                *plan["common"],
                "--entrypoint",
                "python3",
                plan["image"],
                *arguments,
            ],
            run,
            "image-check.log",
        )
    prefix = "NARWHAL_IMAGE_RUNTIME="
    records = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(records) != 1:
        raise ValueError(f"runtime check requires one runtime version record; inspect {run / log}")
    if output.splitlines().count("NARWHAL_TOKENIZER_READY=1") != 1:
        raise ValueError(f"runtime check requires one tokenizer confirmation; inspect {run / log}")
    api_version = records[0].get("vllm_api_version")
    if not isinstance(api_version, str) or not api_version.strip():
        raise ValueError(f"runtime check returned an invalid API version; inspect {run / log}")
    marker = run / "checked.json"
    evidence = {
        "plan_sha256": digest(run / "launch.json"),
        "vllm_api_version": api_version,
    }
    if native:
        evidence.update(
            backend="native",
            python_executable=plan["python_executable"],
            expected_packages=plan["expected_packages"],
        )
    else:
        assert inspection is not None
        evidence["image_id"] = inspection["Id"]
    value = json.dumps(evidence)
    if marker.exists():
        if marker.read_text() != value:
            raise ValueError("runtime identity changed; prepare a fresh launch directory")
    else:
        write_private(marker, value)
    print("Runtime identity, package pins, connector import and tokenizer passed.")


def require_checked(run: Path, plan: dict) -> None:
    checked = json.loads((run / "checked.json").read_text())
    if checked["plan_sha256"] != digest(run / "launch.json"):
        raise ValueError("launch plan changed after its runtime check")
    if plan.get("backend") == "native" and (
        checked.get("backend") != "native"
        or checked.get("python_executable") != plan["python_executable"]
        or checked.get("expected_packages") != plan["expected_packages"]
    ):
        raise ValueError("native runtime check differs from the launch plan")


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


def runtime_config(plan: dict) -> Any:
    """Resolve the serving arguments inside the image before creating model workers."""
    from vllm.engine.arg_utils import EngineArgs  # type: ignore[import-not-found]
    from vllm.utils.argparse_utils import FlexibleArgumentParser  # type: ignore[import-not-found]

    model_config = Path(plan.get("model_config_path", "/model/config.json"))
    if digest(model_config) != plan["model_config_sha256"]:
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
        "model_architecture": model.architecture,
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
    from vllm.v1.engine.core import EngineCore  # type: ignore[import-not-found]
    from vllm.v1.executor.abstract import Executor  # type: ignore[import-not-found]

    plan = json.loads(plan_path.read_text())
    config = runtime_config(plan)
    workers = []
    captured = []

    class SizingComplete(Exception):
        pass

    class ProbeExecutor(Executor.get_class(config)):  # type: ignore[misc]
        def __init__(self, vllm_config: Any) -> None:
            workers.append(self)
            super().__init__(vllm_config)

        def initialize_from_config(self, configs: Any) -> None:
            for rank, allocation in enumerate(configs):
                captured.append(
                    {
                        "rank": rank,
                        "layers": cache_groups(allocation.kv_cache_groups),
                        "kv_cache_layout": allocation.kv_cache_layout,
                    }
                )
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


def registration_layout(run: Path, plan: dict, source: Path, from_runtime: bool) -> None:
    """Map an explicitly resolved runtime layout to the contract's block grouping flag."""
    require_checked(run, plan)
    destination = run / "cache-registration.json"
    if destination.exists():
        raise ValueError(
            "cache registration capture exists; retain it and use a fresh inspection plan"
        )
    data = source.read_bytes()
    if from_runtime:
        record = json.loads(data)
        for field, expected in (
            ("image", plan["image"]),
            ("model_config_sha256", plan["model_config_sha256"]),
            ("launch_config_sha256", plan["launch_sha256"]),
        ):
            if record[field] != expected:
                raise ValueError(
                    "cache layout record differs from this image, model or launch input"
                )
        tp = int(plan["args"][plan["args"].index("--tensor-parallel-size") + 1])
        if sorted(rank["rank"] for rank in record["ranks"]) != list(range(tp)):
            raise ValueError("cache layout record requires every TP rank exactly once")
        names = {rank.get("kv_cache_layout") for rank in record["ranks"]}
    else:
        names = set(re.findall(r"\bUsing ([A-Z]+) KV cache layout\.", data.decode()))
    if len(names) != 1 or None in names:
        raise ValueError(
            "Capture one resolved KV cache layout from the serving log "
            "or updated measure-cache output"
        )
    name = next(iter(names))
    script = """import hashlib, inspect, json, sys
from pathlib import Path
from vllm.v1.kv_cache_layout import KVCacheLayout
layout = KVCacheLayout[sys.argv[1]]
source = Path(inspect.getfile(KVCacheLayout))
print('NARWHAL_CACHE_REGISTRATION=' + json.dumps({
    'cross_layers_blocks': layout.is_block_outermost,
    'kv_cache_layout': layout.name,
    'source': 'KVCacheLayout.' + layout.name + '.is_block_outermost',
    'module_file': str(source),
    'module_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
}))
"""
    output = run_runtime_script(run, plan, script, [name], "cache-registration.log")
    prefix = "NARWHAL_CACHE_REGISTRATION="
    records = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(records) != 1 or type(records[0].get("cross_layers_blocks")) is not bool:
        raise ValueError(
            "cache registration inspection requires one boolean result; inspect its log"
        )
    result = records[0]
    if result["kv_cache_layout"] != name:
        raise ValueError("cache registration inspection returned a different layout")
    result.update(
        image=plan["image"],
        plan_sha256=digest(run / "launch.json"),
        input_path=str(source.resolve()),
        input_sha256=hashlib.sha256(data).hexdigest(),
        input_kind="runtime_cache_layout" if from_runtime else "serving_startup_log",
        launcher_sha256=digest(Path(__file__)),
    )
    write_private(destination, json.dumps(result, indent=2) + "\n")
    print("Cache block grouping captured in cache-registration.json.")


def handshake_policy(run: Path, plan: dict) -> None:
    """Retain the installed worker's default and effective compatibility-check setting."""
    require_checked(run, plan)
    destination = run / "handshake-policy.json"
    if destination.exists():
        raise ValueError(
            "handshake policy capture exists; retain it and use a fresh inspection plan"
        )
    arguments = plan["args"]
    connector = json.loads(arguments[arguments.index("--kv-transfer-config") + 1])
    if connector != plan["connector"]:
        raise ValueError("serving arguments differ from the recorded connector configuration")
    script = """import ast, hashlib, inspect, json, sys, textwrap
from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.v1.nixl.base_worker import NixlBaseConnectorWorker
source = textwrap.dedent(inspect.getsource(NixlBaseConnectorWorker.__init__))
defaults = []
for node in ast.walk(ast.parse(source)):
    if not isinstance(node, ast.Assign):
        continue
    if not any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
               and t.value.id == 'self' and t.attr == 'enforce_compat_hash' for t in node.targets):
        continue
    call = node.value
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        and call.func.attr == 'get_from_extra_config'
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == 'kv_transfer_config'
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == 'self' and len(call.args) == 2
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 'enforce_handshake_compat'):
        defaults.append(ast.literal_eval(call.args[1]))
if len(defaults) != 1 or type(defaults[0]) is not bool:
    raise ValueError('Inspect the installed worker compatibility policy before declaring it')
config = KVTransferConfig(**json.loads(sys.argv[1]))
effective = config.get_from_extra_config('enforce_handshake_compat', defaults[0])
if effective is not True:
    raise ValueError('The resolved handshake compatibility setting must be boolean true')
print('NARWHAL_HANDSHAKE_POLICY=' + json.dumps({
    'enforce_handshake_compat': effective,
    'configured': 'enforce_handshake_compat' in config.kv_connector_extra_config,
    'installed_default': defaults[0],
    'source': 'NixlBaseConnectorWorker.__init__: self.enforce_compat_hash',
    'source_code': source,
    'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
}))
"""
    output = run_runtime_script(run, plan, script, [json.dumps(connector)], "handshake-policy.log")
    prefix = "NARWHAL_HANDSHAKE_POLICY="
    records = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(records) != 1 or records[0].get("enforce_handshake_compat") is not True:
        raise ValueError("handshake policy inspection requires boolean true; inspect its log")
    result = records[0]
    result.update(
        image=plan["image"],
        plan_sha256=digest(run / "launch.json"),
        connector_config=connector,
        launcher_sha256=digest(Path(__file__)),
    )
    write_private(destination, json.dumps(result, indent=2) + "\n")
    print("Compatibility-check configuration captured in handshake-policy.json.")


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
            *plan["common"],
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
    env_file = "engine.env" if plan.get("backend") == "native" else "container.env"
    values = dict(line.split("=", 1) for line in (run / env_file).read_text().splitlines())
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
    # Resolve CUDA ordinals in the serving runtime; NVML indices can use another order.
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
    from narwhal.deployment import stages

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
                # Retain ownership before filesystem or Docker start failures can intervene.
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


def capture_cache(run: Path, plan: dict) -> None:
    """Retain the cache pages emitted by this live serving process."""
    require_checked(run, plan)
    destination = run / "cache-layout.json"
    pending = run / "cache-layout.pending.json"
    if destination.exists() or pending.exists():
        raise ValueError("cache layout capture exists; retain it and use a fresh plan")
    cid = (run / "container.id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("container.id must contain the recorded serving container")
    state = json.loads(
        docker(["inspect", "--format", "{{json .State}}", cid], run, "cache-capture.log")
    )
    if not state["Running"]:
        raise ValueError("serving container exited; inspect its launch log before cache capture")
    docker(["cp", f"{cid}:/tmp/narwhal-cache-layout.json", str(pending)], run, "cache-capture.log")
    pending.chmod(0o600)
    record = json.loads(pending.read_text())
    expected = {
        "image": plan["image"],
        "expected_packages": plan["expected_packages"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
        "launch_config_sha256": plan["launch_sha256"],
        "plan_sha256": digest(run / "launch.json"),
        "launcher_sha256": plan["launcher_sha256"],
        "cache_capture_sha256": plan["cache_capture_sha256"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("live cache capture differs from the checked serving plan")
    tp = int(plan["args"][plan["args"].index("--tensor-parallel-size") + 1])
    ranks = record["ranks"]
    if sorted(rank["rank"] for rank in ranks) != list(range(tp)):
        raise ValueError("live cache capture requires every TP rank exactly once")
    write_private(destination, pending.read_text())
    pending.unlink()
    print("Live serving cache pages captured in cache-layout.json; container remains running.")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Delivered launcher snapshots and container probes also run before package install.
    standalone = __name__ == "__main__" and not any(
        argument == "--format" or argument.startswith("--format=") for argument in arguments
    )
    if standalone or (arguments and arguments[0].startswith("_")):
        return _main(arguments, structured=False)
    from narwhal import command_results as results

    return results.invoke("narwhal-engine", arguments, _main, operation="engine")


def _main(argv: list[str], *, structured: bool = True) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    # Runtime probes execute a standalone copy inside the engine image.
    if __package__:
        from narwhal.cli_support import add_version_argument

        add_version_argument(parser)
    descriptions = {
        "prepare": "Pin launch inputs from NARWHAL_ENGINE_LAUNCH_CONFIG and the deployment "
        "environment into a fresh directory; supports container and native backends.",
        "check": "Check the prepared model, tokenizer, runtime packages and connector; "
        "write checked.json for the plan's container or native backend.",
        "measure-cache": "Run a temporary sizing container from a checked, unused plan; "
        "write cache-layout.json and remove the completed sizing container (container only).",
        "model-dimensions": "Inspect model dimensions using a checked plan; "
        "write model-dimensions.json (container only).",
        "handshake-policy": "Inspect the installed NIXL compatibility policy using a checked "
        "plan; write handshake-policy.json (container and native).",
        "start": "Start one serving container from a checked plan and record container.id; "
        "verify HTTP readiness separately (container only).",
        "capture-cache": "Capture cache-layout.json from the running container recorded by "
        "a checked plan (container only).",
        "cache-registration": "Resolve cache block grouping from a checked plan and a startup "
        "log or runtime layout; write cache-registration.json (container and native).",
        "start-shared": "Start two to eight checked plans on one declared GPU, verify each "
        "engine's readiness and memory allowance, and record shared-start.json "
        "(container and native).",
        "stop-native": "Stop owned process groups after validating the recorded native "
        "process identities in the run directory (native only).",
    }
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{" + ",".join(descriptions) + "}"
    )
    public = {
        name: sub.add_parser(name, help=description, description=description)
        for name, description in descriptions.items()
    }
    preparation = public["prepare"]
    preparation.add_argument("--out", type=Path, required=True, help="fresh launch directory")
    preparation.add_argument(
        "--backend",
        choices=("container", "native"),
        default="container",
        help="launch backend (default: %(default)s)",
    )
    # Omitting help hides the choice rows; the explicit metavar hides internal names in usage.
    sub.add_parser("_cache-probe").add_argument("--plan", type=Path, required=True)
    sub.add_parser("_model-dimensions").add_argument("--plan", type=Path, required=True)
    for command in (
        "check",
        "measure-cache",
        "model-dimensions",
        "handshake-policy",
        "start",
        "capture-cache",
    ):
        public[command].add_argument(
            "--run", type=Path, required=True, help="prepared launch directory"
        )
    registration = public["cache-registration"]
    registration.add_argument("--run", type=Path, required=True, help="checked launch directory")
    source = registration.add_mutually_exclusive_group(required=True)
    source.add_argument("--startup-log", type=Path, help="serving log with a resolved KV layout")
    source.add_argument("--runtime-layout", type=Path, help="captured runtime cache layout JSON")
    shared = public["start-shared"]
    shared.add_argument(
        "--run",
        type=Path,
        action="append",
        required=True,
        help="checked launch directory; repeat for two to eight engines on the same GPU; "
        "sum of per-engine memory fractions must be <= device_allowance",
    )
    shared.add_argument(
        "--ready-seconds",
        type=int,
        default=180,
        help="readiness budget in seconds per engine, at least 1 (default: %(default)s)",
    )
    shared.add_argument(
        "--backend",
        choices=("container", "native"),
        default="container",
        help="backend shared by every selected plan (default: %(default)s)",
    )
    public["stop-native"].add_argument(
        "--run", type=Path, required=True, help="native launch directory with process records"
    )
    args = parser.parse_args(argv)
    if args.command == "start-shared" and args.ready_seconds < 1:
        parser.error(f"--ready-seconds must be positive, got {args.ready_seconds}")
    if structured:
        from narwhal import command_results as results

        results.set_operation(args.command)
        roots = [args.out] if args.command == "prepare" else args.run
        if not isinstance(roots, list):
            roots = [roots]
        results.set_data({"runs": [str(root.resolve()) for root in roots]})
        for root in roots:
            for name in (
                "launch.json",
                "checked.json",
                "runtime-check.log",
                "container.id",
                "native-process.json",
                "shared-start.json",
                "cache-layout.json",
                "model-dimensions.json",
                "cache-registration.json",
                "handshake-policy.json",
            ):
                results.add_artifact(name.removesuffix(".json"), root / name)
    os.umask(0o077)
    target = getattr(args, "run", getattr(args, "out", getattr(args, "plan", "")))
    context = f"narwhal-engine: {args.command}"
    try:
        if args.command == "prepare":
            try:
                prepare(args.out, dict(os.environ), backend=args.backend)
            except (KeyError, TypeError) as error:
                if structured and results.json_mode():
                    raise
                input_source = os.environ.get("NARWHAL_ENGINE_LAUNCH_CONFIG", "role environment")
                parser.exit(2, f"{context}: invalid input in {input_source}: {error}\n")
        elif args.command == "start-shared":
            runs = [run.resolve() for run in args.run]
            if args.backend == "native":
                from narwhal.deployment.native_engine import start_shared as start_native_shared

                start_native_shared(runs, args.ready_seconds)
            else:
                start_shared(runs, args.ready_seconds)
        elif args.command == "stop-native":
            from narwhal.deployment.native_engine import stop as stop_native

            stop_native(args.run.resolve())
        elif args.command == "_cache-probe":
            runtime_cache_probe(args.plan)
        elif args.command == "_model-dimensions":
            print("NARWHAL_MODEL_DIMENSIONS=" + json.dumps(runtime_model_dimensions(args.plan)))
        else:
            run = args.run.resolve()
            try:
                plan = load(run)
            except (OSError, ValueError) as error:
                if structured and results.json_mode():
                    raise
                parser.exit(2, f"{context}: load run {run}: {error}\n")
            if plan.get("backend") == "native" and args.command not in {
                "check",
                "cache-registration",
                "handshake-policy",
            }:
                raise ValueError(
                    f"{args.command} is container-only; use native shared start or stop"
                )
            if args.command == "cache-registration":
                registration_layout(
                    run,
                    plan,
                    args.runtime_layout or args.startup_log,
                    args.runtime_layout is not None,
                )
                return 0
            {
                "check": check,
                "measure-cache": measure_cache,
                "capture-cache": capture_cache,
                "model-dimensions": model_dimensions,
                "handshake-policy": handshake_policy,
                "start": start,
            }[args.command](run, plan)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as error:
        if structured and results.json_mode():
            raise
        if isinstance(error, FileExistsError):
            parser.exit(1, f"{context}: artifact already exists: {error.filename or error}\n")
        status = 2 if args.command == "prepare" else 1
        if isinstance(error, subprocess.TimeoutExpired):
            parser.exit(status, f"{context}: {target}: timed out after {error.timeout} seconds\n")
        parser.exit(status, f"{context}: {target}: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
