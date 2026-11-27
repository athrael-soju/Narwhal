"""The vLLM attestation inspections, which run with the Python of the engine image.

A container capture runs each one in the live container or a container of its image through
Docker. A Kubernetes engine pod runs them in its engine container before the engine starts and
records their output for its attestation sidecar, so this module imports only the standard
library.
"""

from __future__ import annotations

import json

# The launch plan and model configuration as the engine image's containers mount them.
PLAN_PATH = "/narwhal-hooks/launch.json"
MODEL_CONFIG_PATH = "/model/config.json"

NIXL_CAPTURE_TAG = "NARWHAL_NIXL_CAPTURE_V1:"
NIXL_CAPTURE = f"""import contextlib, hashlib, importlib, json, sys
from pathlib import Path
with contextlib.redirect_stdout(sys.stderr):
    module = importlib.import_module("vllm.distributed.kv_transfer.kv_connector.v1.nixl.metadata")
version = module.NIXL_CONNECTOR_VERSION
if type(version) is not int or version < 1:
    raise ValueError("NIXL_CONNECTOR_VERSION must be a positive integer")
source = Path(module.__file__)
record = {{"nixl_connector_version": version, "module": module.__name__,
          "constant": "NIXL_CONNECTOR_VERSION", "module_file": str(source),
          "module_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}
print("\\n" + {NIXL_CAPTURE_TAG!r} + json.dumps(record), flush=True)
"""

MODEL_DIMENSIONS_CAPTURE_TAG = "NARWHAL_LIVE_MODEL_DIMENSIONS_V1:"
MODEL_DIMENSIONS_CAPTURE = f"""import hashlib, json, sys
from pathlib import Path
import launch_engine
plan_path = Path(sys.argv[2])
plan_data = plan_path.read_bytes()
plan_hash = hashlib.sha256(plan_data).hexdigest()
if plan_hash != sys.argv[1]:
    raise ValueError("mounted launch plan differs from the checked serving plan")
plan = json.loads(plan_data)
launcher_hash = hashlib.sha256(Path(launch_engine.__file__).read_bytes()).hexdigest()
if launcher_hash != plan["launcher_sha256"]:
    raise ValueError("mounted launcher differs from the checked serving plan")
model_hash = hashlib.sha256(Path(sys.argv[3]).read_bytes()).hexdigest()
if model_hash != plan["model_config_sha256"]:
    raise ValueError("mounted model configuration differs from the checked serving plan")
model = launch_engine.runtime_config(plan).model_config
methods = {{"head_size": "get_head_size", "kv_heads": "get_total_num_kv_heads",
           "hidden_layers": "get_total_num_hidden_layers"}}
values = {{field: getattr(model, method)() for field, method in methods.items()}}
if any(type(value) is not int or value < 1 for value in values.values()):
    raise ValueError("model contract getters must return positive integers")
architecture = model.architecture
if not isinstance(architecture, str) or not architecture.strip():
    raise ValueError("runtime model architecture is empty")
record = {{"contract": values,
          "sources": {{field: f"ModelConfig.{{method}}()" for field, method in methods.items()}},
          "model_architecture": architecture, "use_mla": model.use_mla,
          "model_config_sha256": model_hash, "plan_sha256": plan_hash,
          "launcher_sha256": launcher_hash, "image": plan["image"],
          "revision": plan["revision"]}}
print("\\n" + {MODEL_DIMENSIONS_CAPTURE_TAG!r} + json.dumps(record), flush=True)
"""

CACHE_REGISTRATION_TAG = "NARWHAL_CACHE_REGISTRATION="
# Records each layout it names, or every layout the installed vLLM defines when it names none.
CACHE_REGISTRATION = f"""import hashlib, inspect, json, sys
from pathlib import Path
from vllm.v1.kv_cache_layout import KVCacheLayout
source = Path(inspect.getfile(KVCacheLayout))
source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
for layout in [KVCacheLayout[name] for name in sys.argv[1:]] or list(KVCacheLayout):
    print({CACHE_REGISTRATION_TAG!r} + json.dumps({{
        'cross_layers_blocks': layout.is_block_outermost,
        'kv_cache_layout': layout.name,
        'source': 'KVCacheLayout.' + layout.name + '.is_block_outermost',
        'module_file': str(source),
        'module_sha256': source_sha256,
    }}))
"""

HANDSHAKE_POLICY_TAG = "NARWHAL_HANDSHAKE_POLICY="
HANDSHAKE_POLICY = f"""import ast, hashlib, inspect, json, sys, textwrap
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
print({HANDSHAKE_POLICY_TAG!r} + json.dumps({{
    'enforce_handshake_compat': effective,
    'configured': 'enforce_handshake_compat' in config.kv_connector_extra_config,
    'installed_default': defaults[0],
    'source': 'NixlBaseConnectorWorker.__init__: self.enforce_compat_hash',
    'source_code': source,
    'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
}}))
"""


def connector_config(plan: dict) -> dict:
    """Return the transfer configuration the serving arguments pass, which must be the plan's."""
    arguments = plan["args"]
    connector = json.loads(arguments[arguments.index("--kv-transfer-config") + 1])
    if connector != plan["connector"]:
        raise ValueError("serving arguments differ from the recorded connector configuration")
    return connector


# The interpreter arguments of each inspection, from the plan and its SHA-256.
_ARGUMENTS = {
    "connector-version": lambda plan, plan_sha256: ["-c", NIXL_CAPTURE],
    "model-dimensions": lambda plan, plan_sha256: [
        "-c",
        MODEL_DIMENSIONS_CAPTURE,
        plan_sha256,
        PLAN_PATH,
        MODEL_CONFIG_PATH,
    ],
    "cache-registration": lambda plan, plan_sha256: ["-c", CACHE_REGISTRATION],
    "handshake-policy": lambda plan, plan_sha256: [
        "-c",
        HANDSHAKE_POLICY,
        json.dumps(connector_config(plan)),
    ],
}


def inspection(plan: dict, plan_sha256: str, name: str) -> list[str]:
    """Return the interpreter arguments of one inspection of an engine served from its image."""
    return _ARGUMENTS[name](plan, plan_sha256)


def inspections(plan: dict, plan_sha256: str) -> dict[str, list[str]]:
    """Return the interpreter arguments of every inspection, which a Kubernetes engine pod runs
    before its engine starts, by the attestation capture that reads it."""
    return {name: inspection(plan, plan_sha256, name) for name in _ARGUMENTS}
