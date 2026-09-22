"""Derive private deployment records from the loaded environment and remote inspection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.deploy_hosts import SSH, Host, load_hosts, write_private
from tools.engine_launch import load_launches
from tools.launch_engine import ENV_PREFIXES, MANAGED_ENV, validate_runtime
from tools.prepare_host_env import select_values, write_environment

PROBE = r'''
import hashlib, json, re, subprocess, sys
from pathlib import Path

inputs = json.load(sys.stdin)


def command(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Inspection failed: " + args[0])
    return result.stdout


image = json.loads(command(["docker", "image", "inspect", inputs["image"]]))[0]
model_path = Path(inputs["model_dir"]) / "config.json"
model_bytes = model_path.read_bytes()
model = json.loads(model_bytes)
tokenizer_path = Path(inputs["model_dir"]) / "tokenizer_config.json"
tokenizer = json.loads(tokenizer_path.read_text()) if tokenizer_path.is_file() else {}


def has_custom_code(value):
    if isinstance(value, dict):
        return bool(value.get("auto_map")) or any(has_custom_code(item) for item in value.values())
    if isinstance(value, list):
        return any(has_custom_code(item) for item in value)
    return False


requires_trust_remote_code = has_custom_code(model) or has_custom_code(tokenizer)
packages_code = """
import json
from importlib.metadata import distributions
wanted = {'vllm', 'torch', 'nixl', 'nixl-rocm'}
print(json.dumps({d.metadata['Name'].lower(): d.version for d in distributions()
                  if d.metadata['Name'].lower() in wanted}))
"""
packages = json.loads(
    command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            image["Id"],
            "-c",
            packages_code,
        ]
    )
)
gpus = []
if Path("/dev/kfd").exists():
    runtime = "rocm"
    agents = re.split(r"(?m)^\s*Agent\s+\d+\s*$", command(["rocminfo"]))
    agents = [a for a in agents if re.search(r"Device Type:\s+GPU", a)]
    renders = {}
    for node in sorted(
        Path("/sys/class/kfd/kfd/topology/nodes").glob("*"), key=lambda p: int(p.name)
    ):
        properties = dict(
            line.split(maxsplit=1)
            for line in (node / "properties").read_text().splitlines()
            if " " in line
        )
        minor = int(properties.get("drm_render_minor", "0"))
        if minor >= 128:
            location = int(properties["location_id"])
            renders.setdefault(location, []).append("/dev/dri/renderD" + str(minor))
    if sum(len(v) for v in renders.values()) != len(agents):
        raise RuntimeError("ROCm agents and KFD render-device counts differ")
    for index, agent in enumerate(agents):
        name = re.search(r"Marketing Name:\s*([^\n]+)", agent)
        if not name:
            raise RuntimeError("rocminfo GPU marketing name is missing")
        bdf = re.search(r"BDFID:\s+(\d+)", agent)
        matches = renders.get(int(bdf[1]), []) if bdf else []
        if len(matches) != 1:
            raise RuntimeError("Match each ROCm GPU PCI BDFID to one KFD render device")
        gpus.append({"id": str(index), "name": name[1].strip(), "device": matches[0]})
    common_devices = ["/dev/kfd"]
else:
    runtime = "cuda"
    rows = command(
        ["nvidia-smi", "--query-gpu=index,name,uuid", "--format=csv,noheader"]
    ).splitlines()
    for row in rows:
        index, name, uuid = (part.strip() for part in row.split(","))
        gpus.append({"id": index, "name": name, "uuid": uuid, "device": "/dev/nvidia" + index})
    common_devices = [
        str(p) for p in (Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm")) if p.exists()
    ]
if not gpus:
    raise RuntimeError("GPU inspection returned an empty allocation")
if any(not Path(g["device"]).exists() for g in gpus):
    raise RuntimeError("A detected GPU device is unavailable")
interfaces = json.loads(command(["ip", "-json", "address", "show", "dev", inputs["interface"]]))
if not interfaces:
    raise RuntimeError("Selected fabric interface is unavailable")
if not Path(inputs["run_dir"]).is_dir():
    raise RuntimeError("Selected run directory is unavailable")
allowed = tuple(inputs["environment_prefixes"])
environment = {}
for entry in image.get("Config", {}).get("Env", []) or []:
    name, _, value = entry.partition("=")
    if (
        name.startswith(allowed) or name in ("LD_LIBRARY_PATH", "PYTHONPATH")
    ) and name not in inputs["managed_environment"]:
        if not any(
            word in name
            for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "SSH", "SKIP_COMPAT")
        ):
            environment[name] = value
text_model = model.get("text_config", model)
linear = text_model.get("linear_attn_config", {})
requires_ds_conv_state_layout = (
    isinstance(linear, dict)
    and bool(linear.get("kda_layers"))
    and bool(linear.get("short_conv_kernel_size"))
) or bool(text_model.get("mamba_d_conv") or text_model.get("mamba_d_state")) or any(
    isinstance(layer, str) and ("mamba" in layer.lower() or "ssm" in layer.lower())
    for layer in text_model.get("layer_types", [])
)
print(
    json.dumps(
        {
            "hostname": command(["hostname"]).strip(),
            "image_id": image["Id"],
            "model_sha256": hashlib.sha256(model_bytes).hexdigest(),
            "requires_trust_remote_code": requires_trust_remote_code,
            "requires_ds_conv_state_layout": requires_ds_conv_state_layout,
            "model_dtype": text_model.get(
                "dtype", text_model.get("torch_dtype", model.get("torch_dtype"))
            ),
            "max_model_len": text_model.get("max_position_embeddings", 16384),
            "packages": packages,
            "runtime": runtime,
            "gpus": gpus,
            "common_devices": common_devices,
            "interfaces": interfaces,
            "image_environment": environment,
        }
    )
)
'''


def value(env: dict[str, str], node: int, field: str, default: str = "") -> str:
    return env.get(f"NARWHAL_NODE_{node}_{field}", env.get(f"NARWHAL_{field}", default))


def derive_hosts(env: dict[str, str]) -> list[Host]:
    """Group equal management destinations before any SSH connection."""
    nodes = sorted(
        int(match[1])
        for name in env
        if (match := re.fullmatch(r"NARWHAL_NODE_([1-9][0-9]*)_SSH", name))
    )
    if not nodes:
        raise ValueError("Set NARWHAL_NODE_<n>_SSH for each engine")
    groups: dict[str, Host] = {}
    for role, name in [
        *((f"engine-{n}", f"NARWHAL_NODE_{n}_SSH") for n in nodes),
        ("router", "NARWHAL_ROUTER_SSH"),
    ]:
        destination = env.get(name, "")
        if not destination or destination.startswith("-") or any(c.isspace() for c in destination):
            raise ValueError(f"Set {name} to an SSH alias or user@host")
        if destination in groups:
            old = groups[destination]
            password = old.password_env
            if password is None and env.get(name + "_PASSWORD"):
                password = name + "_PASSWORD"
            groups[destination] = Host(old.id, old.ssh_env, password, (*old.roles, role))
        else:
            password = name + "_PASSWORD" if env.get(name + "_PASSWORD") else None
            identifier = role.replace("engine-", "node-")
            groups[destination] = Host(identifier, name, password, (role,))
    return list(groups.values())


def build_records(hosts: list[Host], env: dict[str, str], observations: dict, out: Path):
    """Bind detected devices and image metadata to environment-selected deployment policy."""
    launches, engines, sources, derived, shapes = {}, [], {}, {}, set()
    for host in hosts:
        roles = [r for r in host.roles if r.startswith("engine-")]
        allocated: set[str] = set()
        for role in roles:
            node = int(role.split("-")[1])
            observed = observations[role]
            digest = value(env, node, "MODEL_CONFIG_SHA256")
            if digest and digest != observed["model_sha256"]:
                raise ValueError(f"{role}: model-config hash differs from the environment pin")
            gpu_ids = value(env, node, "GPU_IDS")
            if len(roles) > 1 and not gpu_ids:
                raise ValueError(f"Set NARWHAL_NODE_{node}_GPU_IDS for colocated engine roles")
            ids = gpu_ids.split(",") if gpu_ids else [g["id"] for g in observed["gpus"]]
            selected = [g for g in observed["gpus"] if g["id"] in ids or g.get("uuid") in ids]
            if len(selected) != len(ids) or len(set(ids)) != len(ids):
                raise ValueError(f"{role}: GPU_IDS must select distinct detected GPUs")
            physical = {g["id"] for g in selected}
            if allocated & physical:
                raise ValueError(f"{role}: colocated GPU allocations overlap")
            allocated |= physical
            tp = int(value(env, node, "TENSOR_PARALLEL_SIZE", str(len(selected))))
            products = {g["name"] for g in selected}
            if len(products) != 1:
                raise ValueError(f"{role}: allocated GPUs must share one product")
            product = products.pop()
            shapes.add((product, len(selected), tp))
            dtype = value(env, node, "MODEL_DTYPE", observed.get("model_dtype") or "bfloat16")
            if dtype not in {"bfloat16", "float16"}:
                raise ValueError(f"{role}: set MODEL_DTYPE to bfloat16 or float16 in .env")
            max_len = min(int(observed.get("max_model_len") or 16384), 16384)
            args = json.loads(
                value(
                    env,
                    node,
                    "ENGINE_ARGS",
                    json.dumps(
                        [
                            "--max-model-len",
                            str(max_len),
                            "--max-num-seqs",
                            "8",
                            "--gpu-memory-utilization",
                            "0.9",
                            "--enforce-eager",
                        ]
                    ),
                )
            )
            if not isinstance(args, list):
                raise ValueError(f"{role}: ENGINE_ARGS must be a JSON array")
            if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
                args.append("--trust-remote-code")
            environment = dict(observed["image_environment"])
            overrides = json.loads(value(env, node, "ENGINE_ENV", "{}"))
            if not isinstance(overrides, dict):
                raise ValueError(f"{role}: ENGINE_ENV must be a JSON object")
            environment.update(overrides)
            if observed.get("requires_ds_conv_state_layout"):
                if (
                    "VLLM_SSM_CONV_STATE_LAYOUT" in overrides
                    and overrides["VLLM_SSM_CONV_STATE_LAYOUT"] != "DS"
                ):
                    raise ValueError(
                        f"{role}: convolutional SSM transfer requires VLLM_SSM_CONV_STATE_LAYOUT=DS"
                    )
                environment["VLLM_SSM_CONV_STATE_LAYOUT"] = "DS"
            runtime = {
                "expected_packages": observed["packages"],
                "model_dtype": dtype,
                "kv_cache_dtype": "auto",
                "block_size": int(value(env, node, "BLOCK_SIZE", "128")),
                "environment": environment,
                "extra_args": args,
            }
            validate_runtime(runtime)
            transport = value(env, node, "TRANSFER_TRANSPORT", "ucx_tcp")
            net = value(env, node, "TRANSFER_NET_DEVICES", value(env, node, "FABRIC_INTERFACE"))
            devices = json.loads(value(env, node, "TRANSFER_DEVICES", "[]"))
            sources[role] = {
                "inspection": str(out / f"{role}.json"),
                "policy": "workstation .env and documented discovery defaults",
            }
            launches[role] = {
                "accelerator": product,
                "gpu_ids": ids,
                "tensor_parallel_size": tp,
                "gpu_visibility_env": "ROCR_VISIBLE_DEVICES"
                if observed["runtime"] == "rocm"
                else "CUDA_VISIBLE_DEVICES",
                "accelerator_devices": observed["common_devices"] + [g["device"] for g in selected],
                "network_mode": "host",
                "transfer": {"transport": transport, "net_devices": net, "devices": devices},
                "sources": {
                    "allocation": sources[role]["inspection"],
                    "devices": sources[role]["inspection"],
                    "transfer": sources[role]["policy"],
                    "runtime": sources[role]["inspection"],
                },
                "runtime": runtime,
            }
            for field, content in (
                ("ENGINE_IMAGE", observed["image_id"]),
                ("MODEL_CONFIG_SHA256", observed["model_sha256"]),
            ):
                derived[f"NARWHAL_NODE_{node}_{field}"] = content
            for field in ("URL", "ATTESTATION_URL"):
                name = f"NARWHAL_NODE_{node}_{field}"
                if not env.get(name):
                    raise ValueError(f"Set {name} in .env")
            engines.append(
                {
                    "iid": f"n{node}",
                    "url": "${NARWHAL_NODE_" + str(node) + "_URL}",
                    "attestation_url": "${NARWHAL_NODE_" + str(node) + "_ATTESTATION_URL}",
                    "role": "decode",
                }
            )
    if len(shapes) != 1 or len(engines) < 2:
        raise ValueError(
            "This fleet requires at least two replicas with matching GPU and TP shapes"
        )
    engines[0]["role"] = "prefill"
    product, count, tp = shapes.pop()
    model = env.get("NARWHAL_ENGINE_MODEL_NAME")
    if not model:
        raise ValueError("Set NARWHAL_ENGINE_MODEL_NAME in .env")
    fleet = {
        "schema": "narwhal.fleet",
        "schema_version": 1,
        "model": model,
        "hardware": {
            "accelerator": product,
            "accelerators_per_engine": count,
            "tensor_parallel": tp,
        },
        "engines": engines,
        "slo": {
            "ttft_s": float(env.get("NARWHAL_TTFT_S", "10")),
            "tpot_s": float(env.get("NARWHAL_TPOT_S", "0.125")),
        },
        "controller": {
            "min_prefill": 1,
            "min_decode": 1,
            "thresholds": {"expand": 1.0, "shrink": 0.5},
        },
        "profiles": {"path": "runs/profiles.json"},
    }
    if env.get("NARWHAL_ENGINE_KEY"):
        fleet["engine"] = {"engine_api_key_env": "NARWHAL_ENGINE_KEY"}
    return (
        fleet,
        {"schema": "narwhal.engine-launch", "schema_version": 1, "engines": launches},
        sources,
        derived,
    )


def discover(env: dict[str, str], out: Path) -> None:
    hosts = derive_hosts(env)
    paths = {
        name: Path(env.get(name, default))
        for name, default in (
            ("NARWHAL_HOSTS", "config/hosts.local.json"),
            ("NARWHAL_FLEET", "config/fleet.json"),
            ("NARWHAL_LAUNCH_CONFIG", "config/engine-launch.local.json"),
            ("NARWHAL_SSH_KNOWN_HOSTS", "config/ssh.known_hosts"),
        )
    }
    source_path = paths["NARWHAL_LAUNCH_CONFIG"].with_name("engine-launch.sources.json")
    outputs = [paths[n] for n in paths if n != "NARWHAL_SSH_KNOWN_HOSTS"] + [source_path]
    if len({p.resolve() for p in [*outputs, paths["NARWHAL_SSH_KNOWN_HOSTS"]]}) != 5:
        raise ValueError("Select distinct private configuration output paths")
    if out.exists() or any(p.exists() for p in outputs):
        raise ValueError(
            "Archive previous generated configuration and choose a fresh discovery directory"
        )
    env = {**env, **{name: str(path) for name, path in paths.items()}}
    out.mkdir(mode=0o700, parents=True)
    trust = paths["NARWHAL_SSH_KNOWN_HOSTS"]
    trust.parent.mkdir(parents=True, exist_ok=True)
    if not trust.exists():
        write_private(trust, b"")
    ssh = SSH(env, out / "logs", enroll_hosts=True)
    observations = {}
    for host in hosts:
        ssh.run(host, "enroll management host", "hostname")
        print(f"{host.id}: authenticated; SSH host key recorded", flush=True)
        for role in host.roles:
            if role == "router":
                continue
            node = int(role.split("-")[1])
            fields = {
                "image": "ENGINE_IMAGE",
                "model_dir": "MODEL_DIR",
                "run_dir": "RUN_DIR",
                "interface": "FABRIC_INTERFACE",
            }
            inputs = {name: value(env, node, field) for name, field in fields.items()}
            if not all(inputs.values()):
                raise ValueError(
                    f"{role}: set image, model, run directory and fabric interface in .env"
                )
            if not re.fullmatch(r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", inputs["image"]):
                raise ValueError(f"{role}: pin ENGINE_IMAGE to an image ID or registry digest")
            inputs.update(
                environment_prefixes=ENV_PREFIXES, managed_environment=sorted(MANAGED_ENV)
            )
            payload = out / f"{role}-inputs.json"
            write_private(payload, json.dumps(inputs).encode())
            observed = json.loads(
                ssh.run(
                    host,
                    "inspect GPU and image",
                    "python3 -c " + shlex.quote(PROBE),
                    payload=payload,
                )
            )
            write_private(out / f"{role}.json", json.dumps(observed, indent=2).encode())
            observations[role] = observed
            print(f"{role}: GPU devices, image packages and model inspected", flush=True)
    fleet, launches, sources, derived = build_records(hosts, env, observations, out)
    derived.update({name: str(path) for name, path in paths.items()})
    role_env = {
        r: select_values("engine", int(r.split("-")[1]), fleet, {**env, **derived})
        for r in launches["engines"]
    }
    select_values("router", None, fleet, {**env, **derived})
    candidate = out / "engine-launch.json"
    write_private(candidate, json.dumps(launches, indent=2).encode())
    load_launches(candidate, list(launches["engines"]), role_env)
    inventory = {"hosts": [asdict(h) for h in hosts]}
    candidate_hosts = out / "hosts.json"
    write_private(candidate_hosts, json.dumps(inventory, indent=2).encode())
    load_hosts(candidate_hosts, env)
    for path, document in (
        (paths["NARWHAL_HOSTS"], inventory),
        (paths["NARWHAL_FLEET"], fleet),
        (paths["NARWHAL_LAUNCH_CONFIG"], launches),
        (source_path, sources),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private(path, (json.dumps(document, indent=2) + "\n").encode())
    write_environment(out / "derived.env", derived)
    manifest = {
        "files": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [*outputs, trust]},
        "roles": list(launches["engines"]),
    }
    write_private(out / "manifest.json", json.dumps(manifest, indent=2).encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        discover(dict(os.environ), args.out)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if type(error) is ValueError:
            parser.exit(1, f"{error}\n")
        parser.exit(1, "Discovery failed; inspect its private logs and the named .env fields.\n")
    print(f"Generated private configuration. Load {args.out / 'derived.env'} before preparation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
