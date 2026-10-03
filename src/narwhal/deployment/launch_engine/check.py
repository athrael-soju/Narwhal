"""Check a prepared plan's runtime identity, packages, connector and tokenizer."""

from __future__ import annotations

import inspect
import json
import os
import uuid
from pathlib import Path

from .. import stages
from .docker import docker
from .plan import (
    append_private,
    container_options,
    env_file_name,
    kv_events_directory,
    read_env,
    releases_peers,
    requires_ds_conv_state_layout,
    requires_remote_code,
)
from .runtime import digest, engine_arguments, write_once


def check(run: Path, plan: dict) -> None:
    """Check the pinned runtime, connector and tokenizer for the selected backend."""
    native = plan.get("backend") == "native"
    log = "runtime-check.log" if native else "image-check.log"
    if (run / "checked.json").exists():
        require_checked(run, plan)
    kv_events_directory(plan)
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
    environment = read_env(run / env_file_name(plan))
    ds_required = requires_ds_conv_state_layout(Path(plan["model_dir"]))
    if ds_required and environment.get("VLLM_SSM_CONV_STATE_LAYOUT") != "DS":
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
    # Tokenizer metadata resolves in the serving namespace, including image-local paths.
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
from vllm.engine.arg_utils import EngineArgs
from vllm.utils.argparse_utils import FlexibleArgumentParser
parser = EngineArgs.add_cli_args(FlexibleArgumentParser())
engine_args = parser.parse_args(json.loads(__import__('sys').argv[6]))
engine = EngineArgs.from_cli_args(engine_args).create_engine_config()
events = engine.kv_events_config
published = events is not None and events.enable_kv_cache_events and events.publisher == 'zmq'
print('NARWHAL_CACHE_SETTINGS=' + json.dumps({
    'prefix_caching': bool(engine.cache_config.enable_prefix_caching),
    'kv_events': {'endpoint': events.endpoint, 'replay_endpoint': events.replay_endpoint}
    if published else None,
}))
def ucx_version():
    import ctypes, ctypes.util, glob, os
    try:
        import nixl._api as api
    except ImportError:
        return None
    package = api.__name__.split('.')[0]
    root = os.path.dirname(os.path.dirname(api.__file__))
    paths = sorted(glob.glob(os.path.join(root, package + '.libs', 'libucp*.so*')))
    paths += [ctypes.util.find_library('ucp') or 'libucp.so.0']
    for path in paths:
        try:
            library = ctypes.CDLL(path)
        except OSError:
            continue
        library.ucp_get_version_string.restype = ctypes.c_char_p
        return library.ucp_get_version_string().decode()
    return None
print('NARWHAL_IMAGE_RUNTIME=' + json.dumps({
    'vllm_api_version': api_version,
    'ucx_version': ucx_version(),
}))
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
        json.dumps(engine_arguments(plan)),
    ]
    if native:
        result = stages.run(
            [plan["python_executable"], *arguments],
            env={**os.environ, **environment},
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
                *container_options(plan),
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
    ucx = records[0].get("ucx_version")
    if ucx is not None and not isinstance(ucx, str):
        raise ValueError(f"runtime check returned an invalid UCX version; inspect {run / log}")
    prefix = "NARWHAL_CACHE_SETTINGS="
    settings = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(settings) != 1 or type(settings[0].get("prefix_caching")) is not bool:
        raise ValueError(f"runtime check requires one resolved cache setting; inspect {run / log}")
    planned = plan.get("kv_events")
    expected = (
        None if planned is None else {key: planned[key] for key in ("endpoint", "replay_endpoint")}
    )
    if settings[0].get("kv_events") != expected:
        raise ValueError(
            f"runtime cache-event endpoints differ from the launch plan; inspect {run / log}"
        )
    ipc_cache = environment.get("UCX_CUDA_IPC_CACHE")
    ipc_peers = plan.get(
        "cuda_ipc_peers", "engine_ttl" in plan["connector"]["kv_connector_extra_config"]
    )
    evidence = {
        "plan_sha256": digest(run / "launch.json"),
        "vllm_api_version": api_version,
        "prefix_caching": settings[0]["prefix_caching"],
        "kv_events": settings[0]["kv_events"],
        "ucx_version": ucx,
        "peer_release": not ipc_peers or releases_peers(ucx, ipc_cache),
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
    write_once(
        run / "checked.json",
        json.dumps(evidence),
        "runtime identity changed; prepare a fresh launch directory",
    )
    print("Runtime identity, package pins, connector import and tokenizer passed.")
    if ipc_peers and evidence["peer_release"]:
        print(
            "UCX_CUDA_IPC_CACHE is off: peers release a stopped engine's GPU memory, and "
            "transfers from a restarted or idle-evicted producer map its KV memory per transfer."
        )
    if not evidence["peer_release"]:
        print(
            f"UCX {ucx or 'version unknown'} with UCX_CUDA_IPC_CACHE={ipc_cache or 'y'} keeps "
            "a stopped CUDA IPC peer's GPU memory mapped; recover a crashed engine with a "
            "whole-wave restart."
        )
    if not evidence["prefix_caching"]:
        print("Prefix caching is off; the engine publishes no cache events.")
    elif planned is None or evidence["kv_events"] is None:
        print("Prefix caching is on; cache-event publication is disabled.")
    else:
        print(f"Prefix caching is on; cache events publish under {planned['socket_dir']}.")


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
