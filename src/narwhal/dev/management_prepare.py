"""Inspect local dev inputs and bind plans to their exact installed implementation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from narwhal import contracts, provenance
from narwhal.config.loading import load as load_fleet
from narwhal.deployment import native_engine
from narwhal.deployment.management_access import AccessError, directory, read_input
from narwhal.deployment.management_adapters import AdapterManifest, PreparedPlan
from narwhal.deployment.management_executor import StageContext
from narwhal.deployment.management_plans import PlanStore, canonical, digest
from narwhal.deployment.management_records import OperationError, encode_record, utc_now
from narwhal.deployment.management_registry import ManagementTarget

from . import management_settings, template

ACTIONS = ("dev_init", "dev_up", "dev_verify", "dev_down")
MAX_ROUTER_IDLE_BYTES = 262_144
ROUTER_IDLE_TIMEOUT_S = 5.0
_INSTANCE_INPUTS = ("instance.json", "template.json", "fleet.json", "engine-launch.json")
_RUNTIME_CHECK = """import json, sys
from importlib import metadata
from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
expected = json.loads(sys.argv[1])
versions = {name: metadata.version(name) for name in expected}
if versions != expected:
    raise ValueError('Runtime package versions differ from the registered template')
config = KVTransferConfig(kv_connector='NixlConnector', kv_role='kv_both',
    kv_connector_extra_config={'backends': ['UCX'], 'enforce_handshake_compat': True})
KVConnectorFactory.get_connector_class(config)
print(json.dumps({'packages': versions}))
"""


def manifest() -> AdapterManifest:
    """Bind the enabled adapter to verified installed code and packaged schemas."""
    try:
        source = provenance.verified_source()
    except ValueError:
        raise OperationError(
            "adapter_unavailable", "Local dev actions require verified installed source provenance"
        ) from None
    root = Path(__file__).resolve().parents[1]
    names = (
        "dev/management_adapter.py",
        "dev/management_prepare.py",
        "dev/management_settings.py",
        "dev/lifecycle.py",
        "dev/template.py",
        "deployment/native_engine.py",
        "dev/local-dev-settings-v1.schema.json",
        "dev/dev-template-v1.schema.json",
        "dev/small-cuda-v1.json",
        "dev/reference-v1.json",
    )
    try:
        assets = {name: digest((root / name).read_bytes()) for name in names}
    except OSError:
        raise OperationError(
            "adapter_unavailable", "Local dev adapter assets are missing"
        ) from None
    return AdapterManifest(
        id="local-dev-v1",
        version="1",
        assets_sha256=digest(canonical(assets)),
        source=source,
        actions={action: frozenset({action.replace("_", ".", 1)}) for action in ACTIONS},
    )


def _object(data: bytes) -> dict[str, Any]:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    encode_record(value)
    return value


def _read(path: Path, hashes: dict[str, str]) -> dict[str, Any]:
    data = read_input(path)
    hashes[str(path)] = digest(data)
    return _object(data)


def file_identity(path: Path, context: StageContext) -> dict[str, Any]:
    """Hash a regular model file, allowing pinned Hugging Face cache symlinks."""
    context.assert_current()
    resolved = path.resolve(strict=True)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OperationError("invalid_input", "Model inputs must be regular files")
        value = hashlib.sha256()
        size = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            context.assert_current()
            value.update(block)
            size += len(block)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        any(getattr(before, name) != getattr(after, name) for name in fields)
        or size != before.st_size
        or path.resolve(strict=True) != resolved
        or (resolved.stat().st_dev, resolved.stat().st_ino) != (after.st_dev, after.st_ino)
    ):
        raise OperationError("stale_plan", "Model input changed during inspection")
    return {
        "path": str(path),
        "resolved_path": str(resolved),
        "size": size,
        "sha256": value.hexdigest(),
    }


def _model(context: StageContext, spec: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    selected = file_identity(Path(config["model_path"]), context)
    model = spec["model"]
    if selected["sha256"] != model["sha256"] or Path(selected["path"]).name != model["filename"]:
        raise OperationError("prerequisite_failed", "GGUF model differs from the registered recipe")
    root = Path(config["model_dir"]).resolve(strict=True)
    if not root.is_dir():
        raise OperationError("invalid_input", "Tokenizer directory is missing")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative == Path("README.md") or relative.parts[:2] == (".cache", "huggingface"):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        if len(entries) >= 4096:
            raise OperationError("invalid_input", "Model manifest exceeds its file limit")
        identity = (
            selected
            if path.resolve(strict=True) == Path(selected["resolved_path"])
            else file_identity(path, context)
        )
        entries.append(
            {"path": relative.as_posix(), "size": identity["size"], "sha256": identity["sha256"]}
        )
    hashes = {entry["path"]: entry["sha256"] for entry in entries}
    if "config.json" not in hashes:
        raise OperationError("invalid_input", "Tokenizer directory requires config.json")
    for name, expected in model.get("tokenizer_sha256", {}).items():
        if hashes.get(name) != expected:
            raise OperationError(
                "prerequisite_failed", "Tokenizer differs from the registered recipe"
            )
    if not re.fullmatch(r"[0-9a-f]{40}|sha256:[0-9a-f]{64}", model["revision"]):
        raise OperationError("invalid_input", "Model revision must be immutable")
    files = [
        {"path": "model.gguf", "size": selected["size"], "sha256": selected["sha256"]},
        *[{**entry, "path": "tokenizer/" + entry["path"]} for entry in entries],
    ]
    return {
        "source": model["repository"],
        "revision": model["revision"],
        "served_name": model["served_name"],
        "configuration_sha256": hashes["config.json"],
        "model_path": selected["path"],
        "model_dir": str(root),
        "model_tree_sha256": digest(canonical(files)),
        "files": files,
    }


def _command(context: StageContext, arguments: list[str], env: dict[str, str]) -> str:
    result = context.run_command(arguments, cwd=context.target.working_directory, env=env)
    if result.returncode:
        raise OperationError("prerequisite_failed", "Local dev prerequisite inspection failed")
    return result.stdout


def _observe(
    context: StageContext, spec: dict[str, Any], config: dict[str, Any], env: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    executable = shutil.which("nvidia-smi", path=os.defpath) or "/usr/lib/wsl/lib/nvidia-smi"
    output = _command(
        context,
        [
            executable,
            "--query-gpu=name,uuid,memory.total,memory.used",
            "--format=csv,noheader,nounits",
        ],
        env,
    )
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 4 or not fields[1].startswith("GPU-"):
            raise ValueError("Invalid GPU observation")
        rows.append(
            {
                "name": fields[0],
                "uuid": fields[1],
                "total_mib": int(fields[2]),
                "used_mib": int(fields[3]),
            }
        )
    selected = [
        row for row in rows if config.get("gpu_uuid") is None or row["uuid"] == config["gpu_uuid"]
    ]
    if len(selected) != 1:
        raise OperationError("prerequisite_failed", "Local dev requires one selected NVIDIA GPU")
    gpu = selected[0]
    if spec["gpu"].get("product", gpu["name"]) != gpu["name"]:
        raise OperationError(
            "prerequisite_failed", "GPU product differs from the registered recipe"
        )
    if gpu["total_mib"] < spec["gpu"].get("minimum_total_mib", 0):
        raise OperationError(
            "prerequisite_failed", "GPU memory is below the registered recipe minimum"
        )
    interface = config["fabric_interface"]
    output = _command(
        context,
        [
            shutil.which("ip", path=os.defpath) or "/usr/sbin/ip",
            "-json",
            "address",
            "show",
            "dev",
            interface,
        ],
        env,
    )
    addresses = [
        entry["local"]
        for device in json.loads(output)
        for entry in device.get("addr_info", [])
        if entry.get("family") == "inet"
    ]
    if len(addresses) != 1:
        raise OperationError("prerequisite_failed", "Fabric interface requires one IPv4 address")
    runtime = _object(
        _command(
            context,
            [
                sys.executable,
                "-I",
                "-c",
                _RUNTIME_CHECK,
                json.dumps(spec["runtime"]["expected_packages"]),
            ],
            {**env, **spec["runtime"].get("environment", {})},
        ).encode()
    )
    template.check_plugin(spec["runtime"])
    identity = {
        "gpu": {key: gpu[key] for key in ("name", "uuid", "total_mib")},
        "interface": interface,
        "address": addresses[0],
        "runtime_packages": runtime["packages"],
    }
    return identity, {
        "gpu_used_mib": gpu["used_mib"],
        "gpu_free_mib": gpu["total_mib"] - gpu["used_mib"],
    }


def _ownership(root: Path, config: dict[str, Any], hashes: dict[str, str]) -> dict[str, Any]:
    try:
        state = _read(root / "lifecycle.json", hashes)
    except AccessError as error:
        if error.code != "input_missing":
            raise
        return {"run": None, "phase": "initialized", "processes": []}
    run = Path(state["run"])
    if run.parent != root or not re.fullmatch(r"run-[A-Za-z0-9-]+", run.name):
        raise OperationError("invalid_input", "Lifecycle run is outside the registered instance")
    with directory(run, private=True):
        pass
    owners = {}
    try:
        owners["run"] = _read(run / "management-owner.json", hashes)
    except AccessError as error:
        if error.code != "input_missing":
            raise
    records = state["processes"]
    if not isinstance(records, list) or len(records) > 32:
        raise ValueError("Invalid process records")
    records = copy.deepcopy(records)
    count = config["engine_count"]
    if type(count) is not int or not 2 <= count <= 8:
        raise ValueError("Invalid engine count")
    for index in range(count):
        try:
            owners[f"engine-{index + 1}"] = _read(
                run / f"engine-{index + 1}" / "management-owner.json", hashes
            )
        except AccessError as error:
            if error.code != "input_missing":
                raise
        path = run / f"engine-{index + 1}" / "native-process.json"
        try:
            identity = _read(path, hashes)
        except AccessError as error:
            if error.code != "input_missing":
                raise
        else:
            records.append({"name": f"engine-{index + 1}", "identity": identity})
    for record in records:
        identity = record["identity"]
        if (
            not isinstance(record["name"], str)
            or type(identity["pid"]) is not int
            or identity["pid"] <= 0
            or not isinstance(identity["boot_id"], str)
            or type(identity["start_ticks"]) is not int
            or identity["start_ticks"] < 0
        ):
            raise ValueError("Invalid process identity")
        try:
            current = native_engine.process_identity(identity["pid"])
        except (FileNotFoundError, ProcessLookupError):
            observed = "unknown" if native_engine._group_members(identity) else "absent"
        except ValueError:
            observed = "unknown" if native_engine._group_members(identity) else "mismatch"
        else:
            observed = "present" if current == identity else "mismatch"
        record["observed_state"] = observed
    return {"run": str(run), "phase": state["phase"], "processes": records, "owners": owners}


def _ports(config: dict[str, Any]) -> set[int]:
    """Reserve all fixed listeners and the registered UCX TCP range."""
    result: set[int] = set()
    count = config["engine_count"]
    if type(count) is not int or not 2 <= count <= 8:
        raise ValueError("Invalid engine count")
    for key in ("router", "engine_first", "attestation_first", "nixl_first"):
        first = config["ports"][key]
        width = 1 if key == "router" else count
        if type(first) is not int or not 1 <= first <= 65536 - width:
            raise ValueError("Invalid instance port")
        result.update(range(first, first + width))
    match = re.fullmatch(r"([0-9]+)-([0-9]+)", config["ucx_range"])
    if match is None:
        raise ValueError("Invalid UCX TCP range")
    first, last = map(int, match.groups())
    if not 1 <= first <= last <= 65535 or last - first > 4093:
        raise ValueError("UCX range exceeds reservation bounds")
    result.update(range(first, last + 1))
    if len(result) + 2 > 4096:
        raise ValueError("Port reservations exceed operation resource limit")
    return result


def inspect_ownership(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Validate bounded instance ownership paths without checking the GPU runtime."""
    try:
        return _ownership(root, config, {})
    except OperationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise OperationError("invalid_input", "Dev ownership inputs are invalid") from None


async def _idle_response(context: StageContext, url: str, seconds: float) -> bytes:
    """Bound the complete local HTTP exchange, including slow response streams."""
    payload = bytearray()
    async with asyncio.timeout(seconds):
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=seconds
        ) as client:
            async with client.stream(
                "GET", url, headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code != 200:
                    raise ValueError("Router idle response did not succeed")
                async for chunk in response.aiter_raw():
                    context.assert_current()
                    if len(payload) + len(chunk) > MAX_ROUTER_IDLE_BYTES:
                        raise ValueError("Router idle evidence exceeds its byte limit")
                    payload.extend(chunk)
    return bytes(payload)


def _router_idle(
    context: StageContext,
    config: dict[str, Any],
    ownership: dict[str, Any],
    fleet: dict[str, Any],
) -> dict[str, Any]:
    """Observe an owned loopback router's idle counters without changing admission."""
    try:
        port = config["ports"]["router"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid router port")
        origin = f"http://127.0.0.1:{port}"
        if config["router_url"] != origin:
            raise ValueError("Router URL differs from its recorded loopback port")
        routers = [row for row in ownership["processes"] if row["name"] == "router"]
        if len(routers) != 1 or routers[0]["observed_state"] != "present":
            raise ValueError("Router has no current owned process")
        engine_ids = {row["iid"] for row in fleet["engines"]}
        if not engine_ids or len(engine_ids) != config["engine_count"]:
            raise ValueError("Fleet engine identities are incomplete")
        context.assert_current()
        deadline = min(context.deadline, time.monotonic() + ROUTER_IDLE_TIMEOUT_S)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Idle observation has no remaining budget")
        payload = asyncio.run(_idle_response(context, origin + "/narwhal/state", remaining))
        document = _object(payload)
        contracts.validate_document(document, contracts.STATE)
        admission = {
            name: document["admission"][name]
            for name in ("inflight", "queued", "waiting_prefill", "waiting_decode")
        }
        serving = {"http_retained": document["serving"]["http_retained"]}
        resident = document["resident"]
        if not isinstance(resident, dict) or not engine_ids <= resident.keys():
            raise ValueError("Router resident observations are incomplete")
        occupancy = {}
        for engine_id, values in resident.items():
            if not isinstance(engine_id, str) or not isinstance(values, dict):
                raise ValueError("Router resident observation is invalid")
            occupancy[engine_id] = {name: values[name] for name in ("prefill", "decode")}
        counters = [
            *admission.values(),
            *serving.values(),
            *(value for values in occupancy.values() for value in values.values()),
        ]
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("Router idle counters must be nonnegative integers")
        ha = {name: document["ha"][name] for name in ("standby", "epoch")}
        if type(ha["standby"]) is not bool or type(ha["epoch"]) is not int or ha["epoch"] < 0:
            raise ValueError("Router lease observation is invalid")
        if ha["standby"] or ha["epoch"] > 0:
            raise OperationError(
                "prerequisite_failed", "Local dev verification does not coordinate HA leases"
            )
        if any(counters):
            raise OperationError("fleet_busy", "Router admission or resident work is not idle")
        return {
            "observed_at": utc_now(),
            "admission": admission,
            "serving": serving,
            "resident": occupancy,
            "ha": ha,
        }
    except OperationError:
        raise
    except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise OperationError(
            "prerequisite_failed", "Router idle evidence is unavailable or invalid"
        ) from None


def _discover(
    context: StageContext, target: ManagementTarget, action: str, parameters: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    if action not in ACTIONS or target.kind != "dev" or target.instance_dir is None:
        raise OperationError("invalid_input", "Unsupported local dev target or action")
    context.assert_current()
    settings = management_settings.load(target)
    root = target.instance_dir
    hashes: dict[str, str] = {}
    if target.adapter.settings_path is not None:
        hashes[str(target.adapter.settings_path)] = digest(read_input(target.adapter.settings_path))
    with directory(root.parent):
        pass
    documents = {}
    try:
        with directory(root, private=True):
            pass
    except AccessError as error:
        if error.code != "input_missing" or action != "dev_init":
            raise
    else:
        for name in _INSTANCE_INPUTS:
            try:
                documents[name] = _read(root / name, hashes)
            except AccessError as error:
                if action != "dev_down" or name == "instance.json" or error.code != "input_missing":
                    raise
    config = documents.get("instance.json")
    if config is not None:
        if (
            config.get("schema") != "narwhal.dev-instance"
            or config.get("schema_version") != 1
            or config.get("backend") != "native"
        ):
            raise ValueError("Unsupported instance")
        expected = Path(config["python_executable"])
        if expected.parent.resolve() != Path(
            sys.executable
        ).parent.resolve() or not expected.samefile(sys.executable):
            raise OperationError(
                "prerequisite_failed", "Instance requires its original Python environment"
            )
    recipe_document = None
    if action == "dev_init":
        recipe_document, _ = management_settings.resolve_init(target, parameters["recipe_id"])
        selected_recipe = next(row for row in target.recipes if row.id == parameters["recipe_id"])
        hashes[str(selected_recipe.path)] = digest(read_input(selected_recipe.path))
        spec = copy.deepcopy(recipe_document)
        for key in ("engine_count", "gpu_memory_utilization", "device_allowance"):
            value = getattr(settings.init, key)
            if value is not None:
                spec["allocation"][key] = value
        if settings.init.port_base is not None:
            base = settings.init.port_base
            spec["ports"].update(
                router=base,
                engine_first=base + 1,
                attestation_first=base + 101,
                nixl_first=base + 201,
            )
        if config is not None:
            if template._template_differences(spec, documents["template.json"]):
                raise OperationError(
                    "prerequisite_failed", "Existing instance differs from the registered recipe"
                )
            for key, value in settings.init.model_dump(exclude_none=True).items():
                if key in config and str(config[key]) != str(value):
                    raise OperationError(
                        "prerequisite_failed", "Existing instance differs from registered settings"
                    )
        else:
            hub = Path.home() / ".cache/huggingface/hub"
            model = spec["model"]
            config = {
                "model_path": settings.init.model_path
                or str(
                    hub
                    / ("models--" + model["repository"].replace("/", "--"))
                    / "snapshots"
                    / model["revision"]
                    / model["filename"]
                ),
                "model_dir": settings.init.model_dir
                or str(
                    hub
                    / ("models--" + model["tokenizer_repository"].replace("/", "--"))
                    / "snapshots"
                    / model["tokenizer_revision"]
                ),
                "gpu_uuid": settings.init.gpu_uuid,
                "fabric_interface": settings.init.fabric_interface or "eth0",
            }
    elif action != "dev_down":
        spec = documents["template.json"]
    else:
        spec = {}
    if config is None:
        raise OperationError("input_missing", "Dev instance configuration is missing")
    ownership = (
        _ownership(root, config, hashes)
        if documents
        else {"run": None, "phase": "absent", "processes": []}
    )
    host = Path("/etc/machine-id").read_text().strip()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    netns = os.stat("/proc/self/ns/net").st_ino
    host_id = digest(host.encode())
    resources = [f"host:{host_id}:instance:{digest(str(root).encode())}"]
    identity: dict[str, Any] = {
        "host_id": host_id,
        "boot_id": boot,
        "netns": netns,
        "instance_dir": str(root),
        "exists": bool(documents),
        "ownership": ownership,
        "python_executable": str(Path(sys.executable).absolute()),
        "python_version": list(sys.version_info[:3]),
    }
    observations: dict[str, Any] = {}
    fleet = documents.get("fleet.json", {})
    if action == "dev_verify":
        if ownership["run"] is None:
            raise OperationError("prerequisite_failed", "Verification requires a launched instance")
        run = Path(ownership["run"])
        fleet = _read(run / "fleet.json", hashes)
        profiles = run / "profiles.json"
        if fleet.get("profiles", {}).get("path") != str(profiles):
            raise OperationError(
                "prerequisite_failed", "Verification profiles must belong to the active run"
            )
        _read(profiles, hashes)
    env = context.access.environment(target, fleet)
    inputs: dict[str, bytes] = {"resource_ownership": encode_record(ownership)}
    if action == "dev_verify":
        if ownership["phase"] not in {"launched", "ready", "degraded"}:
            raise OperationError("prerequisite_failed", "Verification requires a launched instance")
        if not ownership["processes"] or any(
            row["observed_state"] != "present" for row in ownership["processes"]
        ):
            raise OperationError(
                "prerequisite_failed", "Verification requires current owned processes"
            )
        observations["router_idle"] = _router_idle(context, config, ownership, fleet)
    if action != "dev_down":
        management_settings.validate_recipe(spec)
        model = _model(context, spec, config)
        observed, host_observations = _observe(context, spec, config, env)
        observations.update(host_observations)
        count = spec["allocation"]["engine_count"]
        _, ports = template._port_layout(spec, count)
        fraction = Decimal(str(spec["allocation"]["gpu_memory_utilization"]))
        allowance = Decimal(str(spec["allocation"]["device_allowance"]))
        if not 0 < fraction <= 1 or not fraction * count <= allowance <= 1:
            raise OperationError(
                "prerequisite_failed", "Engine memory budgets exceed the device allowance"
            )
        if action == "dev_up" or (action == "dev_init" and not documents):
            if action == "dev_up" and ownership["phase"] not in {"initialized", "stopped"}:
                raise OperationError(
                    "prerequisite_failed", "Stop the existing instance before starting it"
                )
            template._check_free_ports(ports, "127.0.0.1")
            required = int(observed["gpu"]["total_mib"] * allowance) + spec["gpu"]["reserve_mib"]
            if observations["gpu_free_mib"] < required:
                raise OperationError(
                    "prerequisite_failed", "GPU free memory is below the registered reserve"
                )
        if not documents:
            documents = template.render_documents(
                root,
                spec=spec,
                model_dir=Path(config["model_dir"]).resolve(),
                model_path=Path(config["model_path"]),
                fabric_interface=config["fabric_interface"],
                address=observed["address"],
                gpu=observed["gpu"],
                engine_key_env=bool(env.get("NARWHAL_ENGINE_API_KEY")),
            )
            config = documents["instance.json"]
            fleet = documents["fleet.json"]
        elif (
            config["gpu_uuid"] != observed["gpu"]["uuid"]
            or config["fabric_address"] != observed["address"]
        ):
            raise OperationError("stale_plan", "Instance GPU or fabric identity changed")
        with tempfile.TemporaryDirectory(prefix="narwhal-managed-fleet-") as temporary:
            trial = Path(temporary) / "fleet.json"
            trial.write_bytes(encode_record(fleet))
            load_fleet(trial)
        identity.update(
            model_tree_sha256=model["model_tree_sha256"],
            runtime=observed,
            allocation={key: str(value) for key, value in spec["allocation"].items()},
        )
        resources.append(f"host:{host_id}:gpu:{observed['gpu']['uuid']}")
        resources.extend(
            f"host:{host_id}:netns:{netns}:tcp:{port}" for port in sorted(_ports(config))
        )
        inputs.update(
            model_identity=encode_record(model),
            host_inventory=encode_record(
                {"local": {"host_id": host_id, "boot_id": boot, "gpu": observed["gpu"]}}
            ),
            network=encode_record(
                {
                    "netns": netns,
                    "interface": observed["interface"],
                    "address": observed["address"],
                    "ports": sorted(_ports(config)),
                }
            ),
            service_policy=encode_record({"slo": spec["slo"], "controller": spec["controller"]}),
            measurement_recipe=encode_record(spec["profile"]),
        )
    else:
        gpu = config.get("gpu_uuid")
        if isinstance(gpu, str):
            resources.append(f"host:{host_id}:gpu:{gpu}")
        resources.extend(
            f"host:{host_id}:netns:{netns}:tcp:{port}" for port in sorted(_ports(config))
        )
        inputs["cleanup_selection"] = encode_record(ownership)
    arguments = ["dev", action.removeprefix("dev_"), "--instance", str(root)]
    if action == "dev_init":
        arguments.extend(settings.init_arguments())
    execution = {
        "arguments": arguments,
        "input_hashes": hashes,
        "template": recipe_document,
        "fleet_document": fleet,
    }
    inputs["runtime_identity"] = encode_record(
        {
            "execution": execution,
            "python": identity["python_executable"],
            "packages": spec.get("runtime", {}).get("expected_packages", {}),
        }
    )
    if "fleet.json" in documents:
        inputs["fleet_config"] = encode_record(fleet)
        if "engine-launch.json" in documents:
            inputs["launch_config"] = encode_record(documents["engine-launch.json"])
    inputs["credential_refs"] = encode_record({"environment": list(target.credential_env)})
    identity["input_hashes"] = hashes
    identity["resources"] = sorted(set(resources))
    for registered_path, expected_hash in hashes.items():
        if digest(read_input(Path(registered_path))) != expected_hash:
            raise OperationError("stale_plan", "Registered input changed during preparation")
    canonical(identity)
    return identity, observations, inputs


def snapshot(
    context: StageContext, target: ManagementTarget, action: str, parameters: dict[str, Any]
) -> PreparedPlan:
    """Discover a read-only snapshot and one fixed, finite lifecycle stage."""
    try:
        identity, observations, inputs = _discover(context, target, action, parameters)
        budget = management_settings.load(target).budget(action)
    except OperationError:
        raise
    except AccessError as error:
        raise OperationError(error.code, error.message) from None
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        raise OperationError(
            "invalid_input", "Local dev preparation inputs are invalid or unavailable"
        ) from None
    stage = {
        "stage_id": action.replace("_", "-"),
        "gate": None,
        "operation": action.replace("_", ".", 1),
        "depends_on": [],
        "subjects": ["local"],
        "input_names": sorted(inputs),
        "timeout_ms": budget.timeout_ms,
        "cleanup": {
            "policy": "temporary_only",
            "term_grace_ms": budget.term_grace_ms,
            "kill_grace_ms": budget.kill_grace_ms,
            "reconcile_ms": budget.reconcile_ms,
        },
        "retain_on_success": ["engine", "attestation", "router"] if action == "dev_up" else [],
    }
    return PreparedPlan(
        identity=identity,
        observations=observations,
        inputs=inputs,
        stages=[stage],
        parameters=parameters,
    )


prepare = snapshot


def check(context: StageContext, target: ManagementTarget, plan: dict[str, Any]) -> None:
    """Reject changed stable identities before invoking a lifecycle command."""
    payload = plan["payload"]
    if manifest().source != payload["binding"]["source"]:
        raise OperationError("stale_plan", "Installed source provenance changed")
    for path, expected in execution_config(context, plan)["input_hashes"].items():
        try:
            current_hash = digest(read_input(Path(path)))
        except AccessError:
            raise OperationError("stale_plan", "A prepared dev input is unavailable") from None
        if current_hash != expected:
            raise OperationError("stale_plan", "A prepared dev input changed")
    current = snapshot(context, target, payload["action"], payload["parameters"])
    if digest(canonical(current.identity)) != payload["binding"]["identity_sha256"]:
        raise OperationError("stale_plan", "Local dev prerequisites changed after preparation")


def execution_config(context: StageContext, plan: dict[str, Any]) -> dict[str, Any]:
    """Read only retained execution inputs; the adapter chooses the executable."""
    inputs = plan["payload"]["binding"]["inputs"]
    selected = next(row for row in inputs if row["name"] == "runtime_identity")
    data = PlanStore(context.registry, context.target_id).blob(selected["sha256"])
    result = _object(data)["execution"]
    return {
        **result,
        "template": encode_record(result["template"]) if result["template"] is not None else None,
    }
