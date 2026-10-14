from __future__ import annotations

import inspect
import json
from pathlib import Path

from ...deployment.launch_engine.check import CheckContext, ProbeResult
from ...deployment.launch_engine.plan import (
    model_conv_state_layers,
    releases_peers,
    requires_remote_code,
)
from .runtime import engine_arguments


def probe(context: CheckContext) -> list[str]:
    plan = context.plan
    ds_required = model_conv_state_layers(Path(plan["model_dir"]))
    if ds_required and context.environment.get("VLLM_SSM_CONV_STATE_LAYOUT") != "DS":
        raise ValueError("Convolutional SSM transfer requires VLLM_SSM_CONV_STATE_LAYOUT=DS")
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
    return [
        "-c",
        script,
        json.dumps(plan["expected_packages"]),
        json.dumps(plan["connector"]),
        json.dumps(context.trust_remote_code),
        json.dumps(ds_required),
        context.tokenizer,
        json.dumps(engine_arguments(plan)),
    ]


def result(context: CheckContext, output: str) -> ProbeResult:
    plan = context.plan
    prefix = "NARWHAL_IMAGE_RUNTIME="
    records = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(records) != 1:
        raise context.failure("runtime check requires one runtime version record")
    if output.splitlines().count("NARWHAL_TOKENIZER_READY=1") != 1:
        raise context.failure("runtime check requires one tokenizer confirmation")
    ucx = records[0].get("ucx_version")
    if ucx is not None and not isinstance(ucx, str):
        raise context.failure("runtime check returned an invalid UCX version")
    prefix = "NARWHAL_CACHE_SETTINGS="
    settings = [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]
    if len(settings) != 1:
        raise context.failure("runtime check requires one resolved cache setting")
    ipc_cache = context.environment.get("UCX_CUDA_IPC_CACHE")
    ipc_peers = plan.get(
        "cuda_ipc_peers", "engine_ttl" in plan["connector"]["kv_connector_extra_config"]
    )
    peer_release = not ipc_peers or releases_peers(ucx, ipc_cache)
    notes = []
    if ipc_peers and peer_release:
        notes.append(
            "UCX_CUDA_IPC_CACHE is off: peers release a stopped engine's GPU memory, and "
            "transfers from a restarted or idle-evicted producer map its KV memory per transfer."
        )
    if not peer_release:
        notes.append(
            f"UCX {ucx or 'version unknown'} with UCX_CUDA_IPC_CACHE={ipc_cache or 'y'} keeps "
            "a stopped CUDA IPC peer's GPU memory mapped; recover a crashed engine with a "
            "whole-wave restart."
        )
    return ProbeResult(
        version=records[0].get("vllm_api_version"),
        prefix_caching=settings[0].get("prefix_caching"),
        kv_events=settings[0].get("kv_events"),
        fields={"ucx_version": ucx, "peer_release": peer_release},
        notes=tuple(notes),
    )
