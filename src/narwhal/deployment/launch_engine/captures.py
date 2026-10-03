"""Capture model dimensions, cache layouts, cache registration and handshake policy."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .check import require_checked
from .docker import docker, run_runtime_script
from .plan import container_options
from .runtime import LAUNCHER, digest, write_private


def model_dimensions(run: Path, plan: dict) -> None:
    require_checked(run, plan)
    if digest(LAUNCHER) != plan["launcher_sha256"]:
        raise ValueError("launcher changed; prepare and check a fresh launch plan")
    destination = run / "model-dimensions.json"
    if destination.exists():
        raise ValueError("model dimensions exist; retain the capture and use a fresh plan")
    output = docker(
        [
            "run",
            "--rm",
            *container_options(plan),
            "--mount",
            f"type=bind,src={LAUNCHER.resolve()},dst=/narwhal-inspect.py,readonly",
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
        launcher_sha256=digest(LAUNCHER),
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
        launcher_sha256=digest(LAUNCHER),
    )
    write_private(destination, json.dumps(result, indent=2) + "\n")
    print("Compatibility-check configuration captured in handshake-policy.json.")


def measure_cache(run: Path, plan: dict) -> None:
    require_checked(run, plan)
    if digest(LAUNCHER) != plan["launcher_sha256"]:
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
            *container_options(plan),
            "--mount",
            f"type=bind,src={LAUNCHER.resolve()},dst=/narwhal-probe.py,readonly",
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
