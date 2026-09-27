"""Bind SSH plans to registered inputs and observed physical host identities."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from narwhal import contracts, provenance

from . import launch_engine, ssh_plan, ssh_settings
from .engine_launch import selected_launch
from .management_access import AccessError, read_input, read_object
from .management_adapters import AdapterManifest, PreparedPlan
from .management_executor import StageContext
from .management_plans import PlanStore, canonical, digest
from .management_records import MAX_RESOURCES, OperationError, encode_record
from .management_registry import ManagementTarget


def manifest() -> AdapterManifest:
    """Identify the installed implementation that accepts SSH fleet plans."""
    try:
        source = provenance.verified_source()
        root = Path(__file__).parent
        names = (
            "ssh_adapter.py",
            "ssh_prepare.py",
            "ssh_plan.py",
            "ssh_settings.py",
            "ssh_transport.py",
            "ssh_worker.py",
            "ssh_gates.py",
            "ssh_files.py",
            "ssh_fabric.py",
            "ssh_install.py",
            "ssh_workflow.py",
            "ssh_monitoring.py",
            "ssh_tunnel.py",
            "ssh_workload.py",
            "ssh-settings-v1.schema.json",
            "ssh-recipe-v1.schema.json",
            "launch_engine.py",
        )
        hashes = {name: digest((root / name).read_bytes()) for name in names}
        hashes["profiling/management.py"] = digest(
            (root.parent / "profiling/management.py").read_bytes()
        )
    except (OSError, ValueError):
        raise OperationError(
            "adapter_unavailable", "SSH actions require verified installed adapter assets"
        ) from None
    return AdapterManifest(
        id="ssh-v1",
        version="1",
        assets_sha256=digest(canonical(hashes)),
        source=source,
        actions=ssh_plan.action_operations(),
    )


def asset(settings: ssh_settings.SSHSettings, name: str) -> ModuleType:
    """Load a declared helper after the caller verifies the registered source tree."""
    if name not in ssh_settings.ASSETS or not name.endswith(".py"):
        raise OperationError("invalid_input", "Undeclared SSH adapter helper")
    path = Path(settings.source_root) / name
    spec = importlib.util.spec_from_file_location("_narwhal_ssh_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise OperationError("adapter_unavailable", "Registered helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployment_state(target: ManagementTarget, *, registry_id: str | None = None) -> dict[str, Any]:
    """Read the adapter's last recorded deployment generation, when present."""
    try:
        value = read_object(target.artifact_root / "deployment-state.json")
    except AccessError as error:
        if error.code == "input_missing":
            return {}
        raise
    if (
        value.get("schema") != "narwhal.ssh-deployment"
        or value.get("schema_version") != 1
        or value.get("target_id") != target.id
        or (registry_id is not None and value.get("registry_id") != registry_id)
    ):
        raise OperationError("recovery_required", "Recorded SSH deployment state is invalid")
    return value


def environment(
    context: StageContext,
    target: ManagementTarget,
    recipe: ssh_settings.SSHRecipe,
    fleet: dict[str, Any],
) -> dict[str, str]:
    """Resolve registered credentials and literal deployment fields at dispatch."""
    values = context.access.environment(target, fleet)
    values.update(recipe.environment)
    for field, reference in recipe.environment_refs.items():
        # Fixed deployment fields (addresses, image IDs, paths and ports) are
        # public plan inputs. Credentials must be explicitly registered separately.
        try:
            ssh_settings.SSHRecipe.nonsecret({field: "reference"})
            nonsecret = True
        except ValueError:
            nonsecret = False
        if not nonsecret and reference not in target.credential_env:
            raise OperationError(
                "permission_denied", "Recipe environment reference is not registered"
            )
        value = os.environ.get(reference)
        if not value:
            raise OperationError(
                "prerequisite_failed", "A registered environment reference is unset"
            )
        values[field] = value
    return values


def _host_environment(
    context: StageContext,
    target: ManagementTarget,
    settings: ssh_settings.SSHSettings,
    recipe: ssh_settings.SSHRecipe,
    fleet: dict[str, Any],
) -> tuple[dict[str, str], tuple[ssh_settings.SSHHost, ...]]:
    values = environment(context, target, recipe, fleet)
    # Destination and authentication variable names come from the registered inventory.
    document = read_object(Path(settings.hosts_path))
    for host in document.get("hosts", []):
        for field in ("ssh_env", "password_env"):
            name = host.get(field)
            if name is None:
                continue
            if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise OperationError("invalid_input", "SSH access reference is invalid")
            if field == "password_env" and name not in target.credential_env:
                raise OperationError(
                    "permission_denied", "SSH credential reference is not registered"
                )
            if name in os.environ:
                values[name] = os.environ[name]
    return values, ssh_settings.load_hosts(settings, values)


def _roles(
    settings: ssh_settings.SSHSettings,
    hosts: tuple[ssh_settings.SSHHost, ...],
    fleet: dict[str, Any],
    values: dict[str, str],
    document: dict[str, Any],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    select = asset(settings, "tools/deployment/prepare_host_env.py").select_values
    values = {**values, "NARWHAL_DEPLOYMENT_REVISION": settings.source_commit}
    roles = {}
    launches = {}
    for host in hosts:
        for role in host.roles:
            node = int(role.split("-")[1]) if role.startswith("engine-") else None
            roles[role] = select("engine" if node else "router", node, fleet, values)
            if node:
                launches[role] = selected_launch(document, role, roles[role])
                launch_engine.validate_runtime(launches[role]["runtime"])
                if not re.fullmatch(
                    r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}",
                    roles[role]["NARWHAL_ENGINE_IMAGE"],
                ):
                    raise OperationError(
                        "prerequisite_failed", "Each engine requires an immutable container image"
                    )
    expected = {f"n{role.removeprefix('engine-')}" for role in launches}
    if expected != {entry["iid"] for entry in fleet["engines"]} or len(expected) < 2:
        raise OperationError(
            "prerequisite_failed", "Fleet IDs must match at least two numbered engine roles"
        )
    shapes = {
        (entry["accelerator"], entry["tensor_parallel_size"], entry["transfer"]["transport"])
        for entry in launches.values()
    }
    if len(shapes) != 1 or any("shared_device" in entry for entry in launches.values()):
        raise OperationError(
            "prerequisite_failed",
            "SSH v1 requires homogeneous engines with disjoint GPU allocations",
        )
    return roles, launches


def _physical_gpus(inventory: dict[str, Any], selected: list[str]) -> list[str]:
    """Resolve indices and UUID aliases to one stable physical GPU identifier."""
    aliases: dict[str, str] = {}
    physical: dict[str, str] = {}
    for gpu in inventory["gpus"]:
        identity = gpu.get("uuid")
        if identity in {None, "", "0"}:
            identity = gpu.get("pci")
        if not isinstance(identity, str) or not identity:
            raise OperationError("prerequisite_failed", "GPU inventory lacks a physical identity")
        pci = str(gpu.get("pci", "")).lower()
        if identity in physical and physical[identity] != pci:
            raise OperationError(
                "prerequisite_failed", "GPU identity resolves to different PCI devices"
            )
        physical[identity] = pci
        for value in (gpu.get("index"), gpu.get("uuid"), gpu.get("pci")):
            if value is not None:
                key = str(value).lower()
                if key in aliases and aliases[key] != identity:
                    raise OperationError(
                        "prerequisite_failed", "GPU inventory contains an ambiguous alias"
                    )
                aliases[key] = identity
    try:
        result = [aliases[value.lower()] for value in selected]
    except KeyError:
        raise OperationError(
            "prerequisite_failed", "A selected GPU is absent from the host inventory"
        ) from None
    if len(result) != len(set(result)):
        raise OperationError("resource_conflict", "GPU aliases select the same physical device")
    return sorted(result)


def _ports(role: dict[str, str]) -> list[int]:
    """Reserve engine, sidecar and NIXL ports, including the configured UCX range."""
    numbers = [
        int(role[f"NARWHAL_{name}"])
        for name in (
            "ENGINE_PORT",
            "ATTEST_PORT",
            "NIXL_SIDE_CHANNEL_PORT",
        )
    ]
    match = re.fullmatch(r"([0-9]+)-([0-9]+)", role["NARWHAL_UCX_TCP_PORT_RANGE"])
    if match is None:
        raise ValueError("Invalid UCX port range")
    first, last = map(int, match.groups())
    if not 1 <= first <= last <= 65535 or last - first > 2048:
        raise ValueError("UCX port range exceeds reservation bounds")
    numbers.extend(range(first, last + 1))
    if any(not 1 <= number <= 65535 for number in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("Engine ports overlap")
    return sorted(numbers)


def _url(value: str, values: dict[str, str]) -> str:
    reference = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", value)
    if reference:
        value = values.get(reference[1], "")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise OperationError("invalid_input", "Fleet endpoint must be a nonsecret HTTP origin")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((parsed.scheme, f"{authority}:{port}", parsed.path.rstrip("/"), "", ""))


def _endpoints(
    target: ManagementTarget,
    fleet: dict[str, Any],
    roles: dict[str, dict[str, str]],
    values: dict[str, str],
) -> dict[str, Any]:
    endpoints: dict[str, Any] = {"engines": {}}
    for engine in fleet["engines"]:
        role = "engine-" + engine["iid"].removeprefix("n")
        number = role.removeprefix("engine-")
        fields = roles[role]
        urls = {name: _url(engine[name], values) for name in ("url", "attestation_url")}
        for name, port_name, variable in (
            ("url", "ENGINE_PORT", "URL"),
            ("attestation_url", "ATTEST_PORT", "ATTESTATION_URL"),
        ):
            if urlsplit(urls[name]).port != int(fields[f"NARWHAL_{port_name}"]):
                raise OperationError("invalid_input", "Fleet endpoint and launch port disagree")
            field = f"NARWHAL_NODE_{number}_{variable}"
            if field in fields and _url(fields[field], values) != urls[name]:
                raise OperationError("invalid_input", "Fleet and role endpoint bindings disagree")
            fields[field] = urls[name]
        fields.setdefault(f"NARWHAL_NODE_{number}_IP", urlsplit(urls["url"]).hostname or "")
        endpoints["engines"][engine["iid"]] = urls
    router = roles["router"]
    defaults = {
        "router": router.get("NARWHAL_ROUTER_URL", "http://127.0.0.1:8000"),
        "prometheus": "http://" + router.get("NARWHAL_PROMETHEUS_LISTEN_ADDRESS", "127.0.0.1:9090"),
        "grafana": "http://" + router.get("NARWHAL_GRAFANA_BIND_ADDRESS", "127.0.0.1") + ":3000",
    }
    for name, default in defaults.items():
        selected = _url(default, values)
        if name == "router":
            origin = urlsplit(selected)
            if origin.scheme != "http" or origin.hostname != "127.0.0.1" or origin.path:
                raise OperationError(
                    "prerequisite_failed", "SSH v1 router requires a loopback HTTP origin"
                )
            router["NARWHAL_ROUTER_URL"] = selected
        reference = getattr(target.endpoints, name + "_env")
        registered = _url(os.environ.get(reference, ""), values) if reference else None
        endpoints[name] = {"service": selected, "registered": registered}
    return endpoints


def _namespace(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and (match := re.fullmatch(r"net:\[([1-9][0-9]*)\]", value)):
        return int(match[1])
    raise OperationError("prerequisite_failed", "Host network namespace identity is unavailable")


def _host_identity(
    inventory: dict[str, Any], host: ssh_settings.SSHHost, values: dict[str, str]
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", inventory["machine_id"]):
        raise OperationError("prerequisite_failed", "Host machine identity is invalid")
    return {
        "host_id": digest(inventory["machine_id"].encode()),
        "boot_id": inventory["boot_id"],
        "netns": _namespace(inventory["netns"]),
        "destination_sha256": digest(values[host.ssh_env].encode()),
        "roles": list(host.roles),
        "python_executable": inventory["python_executable"],
    }


def _interface(
    inventory: dict[str, Any], fields: dict[str, str], launch: dict[str, Any]
) -> dict[str, Any]:
    name = fields["NARWHAL_FABRIC_INTERFACE"]
    addresses = inventory["interfaces"].get(name)
    if not isinstance(addresses, list) or not addresses:
        raise OperationError("prerequisite_failed", "Selected fabric interface has no address")
    addresses = sorted({str(ipaddress.ip_address(value)) for value in addresses})
    number = launch["role"].removeprefix("engine-")
    if str(ipaddress.ip_address(fields[f"NARWHAL_NODE_{number}_IP"])) not in addresses:
        raise OperationError(
            "prerequisite_failed", "Selected engine address is absent from its fabric interface"
        )
    expected = "rocm" if launch["gpu_visibility_env"] == "ROCR_VISIBLE_DEVICES" else "cuda"
    if inventory.get("runtime") != expected:
        raise OperationError(
            "prerequisite_failed", "Engine runtime differs from the host inventory"
        )
    transport_interfaces = {}
    if launch["transfer"]["transport"] == "ucx_tcp":
        for device in launch["transfer"]["net_devices"].split(","):
            selected = inventory["interfaces"].get(device)
            if not selected:
                raise OperationError("prerequisite_failed", "Selected UCX interface is unavailable")
            transport_interfaces[device] = sorted(
                {str(ipaddress.ip_address(value)) for value in selected}
            )
    return {
        "name": name,
        "addresses": addresses,
        "transport": launch["transfer"]["transport"],
        "net_devices": launch["transfer"]["net_devices"],
        "transport_interfaces": transport_interfaces,
    }


def _prerequisites(inventory: dict[str, Any], launches: list[dict[str, Any]]) -> None:
    version = inventory.get("python", [])
    if len(version) < 2 or tuple(version[:2]) < (3, 11):
        raise OperationError("prerequisite_failed", "SSH hosts require Python 3.11 or newer")
    executable = inventory.get("python_executable")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or ".." in Path(executable).parts
        or any(character in executable for character in "\0\r\n")
    ):
        raise OperationError(
            "prerequisite_failed", "Remote Python executable is not a fixed absolute path"
        )
    required = {"python3", "git", "make", "curl", "docker", "ip"}
    if launches:
        required.add(
            "iperf3" if launches[0]["transfer"]["transport"] == "ucx_tcp" else "ib_write_bw"
        )
    if any(not inventory.get("tools", {}).get(name) for name in required):
        raise OperationError(
            "prerequisite_failed", "A required host deployment tool is unavailable"
        )
    for launch in launches:
        selected = set(_physical_gpus(inventory, launch["gpu_ids"]))
        for gpu in inventory["gpus"]:
            if (
                _physical_gpus(inventory, [str(gpu["index"])])[0] in selected
                and gpu.get("product", "").strip().casefold()
                != launch["accelerator"].strip().casefold()
            ):
                raise OperationError(
                    "prerequisite_failed",
                    "Selected GPU product differs from the declared allocation",
                )


def _new_resources_free(inventory: dict[str, Any], gpu_ids: set[str], ports: set[int]) -> None:
    if ports & set(inventory["ports"]):
        raise OperationError("resource_busy", "A required service or measurement port is occupied")
    if not gpu_ids:
        return
    clients = inventory.get("gpu_clients", {})
    if clients.get("complete") is not True:
        raise OperationError("prerequisite_failed", "GPU client inspection is incomplete")
    selected_pci = {
        str(gpu["pci"]).lower()
        for gpu in inventory["gpus"]
        if _physical_gpus(inventory, [str(gpu["index"])])[0] in gpu_ids
    }
    if any(
        "all" in row["devices"] or selected_pci & {value.lower() for value in row["devices"]}
        for row in clients["processes"]
    ):
        raise OperationError("fleet_busy", "A selected GPU has a live client")


def _management_host() -> dict[str, Any]:
    machine = Path("/etc/machine-id").read_text().strip()
    ports = set()
    for name in ("tcp", "tcp6"):
        with Path("/proc/net", name).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise OperationError(
                "source_truncated", "Management listener inventory exceeds its limit"
            )
        for line in raw.decode().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return {
        "host_id": digest(machine.encode()),
        "netns": os.stat("/proc/self/ns/net").st_ino,
        "ports": sorted(ports),
    }


def _bounded_resources(resources: set[str]) -> list[str]:
    if (
        not resources
        or len(resources) > MAX_RESOURCES
        or any(len(value) > 512 for value in resources)
    ):
        raise OperationError(
            "invalid_input", "Plan resource reservations exceed the executor limit"
        )
    return sorted(resources)


def _effect_budget(
    action: str,
    hosts: tuple[ssh_settings.SSHHost, ...],
    launches: dict[str, dict[str, Any]],
    recipe: ssh_settings.SSHRecipe,
) -> None:
    if action not in {"fleet_deploy", "engine_replace"}:
        return
    engine_hosts = sum(any(role.startswith("engine-") for role in host.roles) for host in hosts)
    rails = (
        max(len(row["transfer"]["net_devices"].split(",")) for row in launches.values())
        if recipe.fabric.transport == "ucx_rdma"
        else 1
    )
    fabric = engine_hosts * (engine_hosts - 1) * (3 + 4 * rails * recipe.fabric.repeats)
    # Reserve slots for every stage's local SSH helper, per-engine lifecycle jobs,
    # workload collectors and fixed router/installation helpers. The executor
    # separately enforces record bytes when it records each effect.
    remaining = (
        len(ssh_plan.action_operations()[action]) * len(hosts)
        + 16 * len(launches)
        + 4 * len(recipe.load.rates)
        + 256
    )
    if fabric + remaining > MAX_RESOURCES:
        raise OperationError("invalid_input", "Planned SSH job receipts exceed the executor limit")


def _private_roles(
    target: ManagementTarget, values: dict[str, str], roles: dict[str, dict[str, str]]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    secrets = {value: name for name in target.credential_env if (value := values.get(name))}
    literals = {
        role: {name: value for name, value in fields.items() if value not in secrets}
        for role, fields in roles.items()
    }
    references = {
        role: {name: secrets[value] for name, value in fields.items() if value in secrets}
        for role, fields in roles.items()
    }
    return literals, references


def _replacement_ready(
    transport: Any,
    hosts: tuple[ssh_settings.SSHHost, ...],
    fleet: dict[str, Any],
    state: dict[str, Any],
    endpoints: dict[str, Any],
) -> None:
    if fleet.get("recovery", {}).get("engine_restart_policy", "individual") != "individual":
        raise OperationError(
            "prerequisite_failed", "Engine replacement requires individual restart policy"
        )
    host = next(host.id for host in hosts if "router" in host.roles)
    router = state.get("router", {})
    effect = router.get("effect", {})
    owner = effect.get("owner", {})
    job_id = owner.get("launch_token")
    if (
        router.get("host_id") != host
        or router.get("url") != endpoints["router"]["service"]
        or effect.get("host_id") != host
        or effect.get("kind") != "router"
        or effect.get("effect") != "confirmed"
        or not isinstance(job_id, str)
        or effect.get("identity", {}).get("job_id") != job_id
    ):
        raise OperationError(
            "prerequisite_failed", "Engine replacement requires a recorded owned router"
        )
    status = transport.status(host, job_id)
    if (
        status.get("owner") != owner
        or status.get("state") != "retained"
        or status.get("supervisor_present") is not True
        or not status.get("observed_processes")
    ):
        raise OperationError("ownership_conflict", "Recorded router ownership is unavailable")
    document = transport.probe(host, "router_state", {"url": router["url"]})
    try:
        contracts.validate_document(document, contracts.STATE)
    except contracts.ContractVersionError:
        raise OperationError(
            "unsupported_contract", "Router state has an unsupported contract version"
        ) from None
    ha = document.get("ha", {})
    if ha.get("standby") is not False or type(ha.get("epoch")) is not int or ha["epoch"] != 0:
        raise OperationError(
            "prerequisite_failed", "SSH v1 engine replacement requires a standalone router"
        )


def _prepare(
    context: StageContext,
    target: ManagementTarget,
    action: str,
    parameters: dict[str, Any],
) -> PreparedPlan:
    from .ssh_transport import SSHTransport

    if action not in ssh_plan.action_operations():
        raise OperationError("invalid_input", "Unsupported SSH action")
    context.assert_current()
    if action == "deployment_cleanup":
        return _cleanup_prepare(context, target, parameters)
    settings = ssh_settings.load(target)
    verified = ssh_settings.verify_source(settings, deadline=context.deadline)
    state = deployment_state(target, registry_id=str(context.registry.registry_id))
    recipe_id = parameters.get("recipe_id") or state.get("recipe_id")
    if not isinstance(recipe_id, str):
        raise OperationError(
            "prerequisite_failed", "This action requires a recorded fleet deployment"
        )
    recipe = ssh_settings.load_recipe(target, recipe_id)
    fleet_path, fleet = context.access.fleet(target)
    values, hosts = _host_environment(context, target, settings, recipe, fleet)
    launch_document = read_object(Path(settings.launch_path))
    role_values, launches = _roles(settings, hosts, fleet, values, launch_document)
    _effect_budget(action, hosts, launches, recipe)
    endpoints = _endpoints(target, fleet, role_values, values)
    for role, launch in launches.items():
        launch_engine.build(launch, role_values[role], Path(settings.remote_root) / "check")
    transport = SSHTransport(context, settings, hosts, values)
    if action == "engine_replace":
        _replacement_ready(transport, hosts, fleet, state, endpoints)
    hashes = {
        str(path): digest(read_input(path))
        for path in (
            fleet_path,
            Path(settings.hosts_path),
            Path(settings.launch_path),
            Path(settings.known_hosts_path),
            target.adapter.settings_path,
            next(row.path for row in target.recipes if row.id == recipe_id),
        )
        if path is not None
    }
    resources: set[str] = set()
    physical: dict[str, Any] = {}
    observations: dict[str, Any] = {}
    models: dict[str, Any] = {}
    images: dict[str, Any] = {}
    model_cache: dict[tuple[str, str], Any] = {}
    image_cache: dict[tuple[str, str], Any] = {}
    allocations: dict[str, Any] = {}
    all_devices: set[str] = set()
    all_ports: set[str] = set()
    interfaces: dict[str, Any] = {}
    for host in hosts:
        context.assert_current()
        inventory = transport.probe(host.id, "inventory", {})
        _prerequisites(inventory, [launches[role] for role in host.roles if role != "router"])
        host_identity = _host_identity(inventory, host, values)
        machine, netns = host_identity["host_id"], host_identity["netns"]
        observations[host.id] = inventory
        physical[host.id] = host_identity
        resources.add(f"host:{machine}:deployment:{digest(settings.remote_root.encode())}")
        host_gpus: set[str] = set()
        host_ports: set[int] = set()
        if "router" in host.roles:
            service_ports = [
                urlsplit(endpoints[name]["service"]).port
                for name in ("router", "prometheus", "grafana")
            ]
            if len(set(service_ports)) != len(service_ports):
                raise OperationError("resource_conflict", "Router and monitoring ports overlap")
            host_ports.update(int(port) for port in service_ports if port is not None)
        if any(role.startswith("engine-") for role in host.roles):
            if recipe.fabric.test_port in host_ports:
                raise OperationError("resource_conflict", "Fabric test port overlaps a service")
            host_ports.add(recipe.fabric.test_port)
        service_keys = {f"host:{machine}:netns:{netns}:tcp:{port}" for port in host_ports}
        if all_ports & service_keys:
            raise OperationError("resource_conflict", "Host aliases reserve the same service port")
        all_ports.update(service_keys)
        for role in host.roles:
            if role == "router":
                continue
            selected = _physical_gpus(inventory, launches[role]["gpu_ids"])
            if launches[role]["transfer"]["transport"] != recipe.fabric.transport:
                raise OperationError(
                    "prerequisite_failed", "Fabric recipe and engine transport disagree"
                )
            interfaces[role] = _interface(inventory, role_values[role], launches[role])
            host_gpus.update(selected)
            gpu_keys = {f"host:{machine}:gpu:{gpu}" for gpu in selected}
            if all_devices & gpu_keys:
                raise OperationError(
                    "resource_conflict", "Engine allocations overlap on a physical GPU"
                )
            all_devices.update(gpu_keys)
            ports = _ports(role_values[role])
            port_keys = {f"host:{machine}:netns:{netns}:tcp:{port}" for port in ports}
            if all_ports & port_keys or host_ports & set(ports):
                raise OperationError(
                    "resource_conflict", "Engine service or transfer ports overlap"
                )
            all_ports.update(port_keys)
            host_ports.update(ports)
            allocations[role] = {"host_id": host.id, "gpu_ids": selected, "ports": ports}
            model_key = (host.id, role_values[role]["NARWHAL_MODEL_DIR"])
            image_key = (host.id, role_values[role]["NARWHAL_ENGINE_IMAGE"])
            if model_key not in model_cache:
                model_cache[model_key] = transport.probe(
                    host.id, "checkpoint", {"model_dir": model_key[1]}
                )
            if image_key not in image_cache:
                image_cache[image_key] = transport.probe(host.id, "image", {"image": image_key[1]})
            models[role] = model_cache[model_key]
            images[role] = image_cache[image_key]
            model_files = {row["path"]: row["sha256"] for row in models[role]["files"]}
            if model_files.get("config.json") != role_values[role]["NARWHAL_MODEL_CONFIG_SHA256"]:
                raise OperationError(
                    "prerequisite_failed",
                    "Model configuration differs from the registered deployment",
                )
        resources.update(f"host:{machine}:netns:{netns}:tcp:{port}" for port in host_ports)
        if action == "fleet_deploy":
            _new_resources_free(inventory, host_gpus, host_ports)
    resources.update(all_devices | all_ports)
    management = _management_host()
    tunnel_ports = {
        recipe.load.local_router_port,
        recipe.load.local_prometheus_port,
        recipe.load.local_grafana_port,
    }
    tunnel_resources = {
        f"host:{management['host_id']}:netns:{management['netns']}:tcp:{port}"
        for port in tunnel_ports
    }
    if resources & tunnel_resources:
        raise OperationError(
            "resource_conflict", "Management tunnel ports overlap deployment services"
        )
    if action == "fleet_deploy" and tunnel_ports & set(management["ports"]):
        raise OperationError("resource_busy", "A management tunnel port is occupied")
    resources.update(tunnel_resources)
    if len({row["model_tree_sha256"] for row in models.values()}) != 1:
        raise OperationError("prerequisite_failed", "Engine checkpoint manifests differ")
    if action == "fleet_deploy" and state.get("status") not in {None, "removed"}:
        raise OperationError(
            "resource_conflict", "A recorded fleet must be cleaned up before a new deployment"
        )
    if action == "engine_replace" and parameters["engine_id"] not in {
        row["iid"] for row in fleet["engines"]
    }:
        raise OperationError("invalid_input", "Replacement engine is not registered in the fleet")
    if action == "fleet_profile" and not set(parameters.get("engine_ids", [])) <= {
        row["iid"] for row in fleet["engines"]
    }:
        raise OperationError("invalid_input", "Profiling selection contains an unknown engine")
    if action == "fleet_profile":
        parameters = {
            **parameters,
            "engine_ids": sorted(
                parameters.get("engine_ids") or [row["iid"] for row in fleet["engines"]]
            ),
        }
    # Secrets stay in the caller environment. Retained execution inputs carry references.
    role_literals, credential_fields = _private_roles(target, values, role_values)
    execution = {
        "settings": settings.model_dump(mode="json", by_alias=True),
        "recipe_id": recipe_id,
        "recipe": recipe.model_dump(mode="json", by_alias=True),
        "role_environment": role_literals,
        "credential_fields": credential_fields,
        "hosts": [host.model_dump(mode="json") for host in hosts],
        "launches": launches,
        "input_hashes": hashes,
        "prior_state": state,
    }
    inputs = {
        "fleet_config": encode_record(fleet),
        "launch_config": encode_record(launch_document),
        "host_inventory": encode_record(physical),
        "model_identity": encode_record(models),
        "runtime_identity": encode_record({"execution": execution, "images": images}),
        "network": encode_record(allocations),
        "service_policy": encode_record({"source": verified}),
        "measurement_recipe": encode_record(recipe.profiling.model_dump(mode="json")),
        "load_recipe": encode_record(recipe.load.model_dump(mode="json")),
        "credential_refs": encode_record(credential_fields),
        "resource_ownership": encode_record(state),
    }
    identity = {
        "hosts": physical,
        "allocations": allocations,
        "input_hashes": hashes,
        "models": {role: value["model_tree_sha256"] for role, value in models.items()},
        "images": {role: value["id"] for role, value in images.items()},
        "assets_sha256": verified["assets_sha256"],
        "resources": _bounded_resources(resources),
        "deployment_state_sha256": digest(encode_record(state)),
        "endpoints": endpoints,
        "interfaces": interfaces,
        "role_environment": role_literals,
        "management_host": {name: management[name] for name in ("host_id", "netns")},
    }
    for path, expected in hashes.items():
        if digest(read_input(Path(path))) != expected:
            raise OperationError("stale_plan", "Registered input changed during preparation")
    return PreparedPlan(
        identity=identity,
        observations=observations,
        inputs=inputs,
        stages=ssh_plan.stages(
            action, settings, subjects=[host.id for host in hosts], input_names=list(inputs)
        ),
        parameters=parameters,
    )


def _snapshot(context: StageContext, plan: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(
        PlanStore(context.registry, context.target_id).blob(
            plan["payload"]["binding"]["snapshot_sha256"]
        )
    )
    return dict(value["identity"])


def _selection(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": operation["operation_id"],
        "action": operation["action"],
        "plan_id": operation["plan_id"],
        "state": operation["state"],
        "effects": [row for stage in operation["stages"] for row in stage["effects"]],
    }


def _cleanup_prepare(
    context: StageContext, target: ManagementTarget, parameters: dict[str, Any]
) -> PreparedPlan:
    from .ssh_transport import SSHTransport

    operation = context.store.read(target.id, parameters["operation_id"])
    if (
        operation["tool"] == "plan_prepare"
        or operation["action"] not in ssh_plan.action_operations()
    ):
        raise OperationError("invalid_input", "Cleanup requires an SSH execution operation")
    if operation["state"] in {"queued", "running", "cancelling"}:
        raise OperationError("resource_busy", "Cleanup cannot replace an active operation")
    original = context.store.plan(target.id, operation["operation_id"])
    if original is None:
        raise OperationError(
            "plan_evidence_missing", "Cleanup requires the retained execution plan"
        )
    store = PlanStore(context.registry, target.id)
    original = store.read(original["plan_id"])
    identity = _snapshot(context, original)
    resources = _bounded_resources(set(identity["resources"]))
    inputs = {
        row["name"]: store.blob(row["sha256"]) for row in original["payload"]["binding"]["inputs"]
    }
    predecessors = []
    if operation["action"] == "deployment_cleanup":
        previous = json.loads(inputs["cleanup_selection"])
        predecessors = [
            {name: value for name, value in previous.items() if name != "predecessors"},
            *previous.get("predecessors", []),
        ]
        identifiers = [operation["operation_id"], *(row["operation_id"] for row in predecessors)]
        if (
            len(identifiers) > 16
            or len(set(identifiers)) != len(identifiers)
            or previous["operation_id"] != original["payload"]["parameters"]["operation_id"]
            or any(row["target_id"] != target.id for row in predecessors)
        ):
            raise OperationError("invalid_input", "Cleanup predecessor chain is invalid")
    runtime = json.loads(inputs["runtime_identity"])
    execution = runtime["execution"]
    old_settings = ssh_settings.SSHSettings.model_validate_json(
        encode_record(execution["settings"])
    )
    settings = ssh_settings.load(target)
    if settings.remote_root != old_settings.remote_root:
        raise OperationError("stale_plan", "Cleanup root differs from the original operation")
    # Cleanup addresses owned jobs through SSH and needs no engine/model environment.
    recipe_document = {**execution["recipe"], "environment_refs": {}}
    recipe = ssh_settings.SSHRecipe.model_validate_json(encode_record(recipe_document))
    fleet = json.loads(inputs["fleet_config"])
    values, hosts = _host_environment(context, target, settings, recipe, fleet)
    if [host.model_dump(mode="json") for host in hosts] != execution["hosts"]:
        raise OperationError(
            "stale_plan", "Cleanup host registration differs from the original plan"
        )
    transport = SSHTransport(context, settings, hosts, values)
    physical, observations = {}, {}
    for host in hosts:
        expected = identity["hosts"][host.id]
        if digest(values[host.ssh_env].encode()) != expected["destination_sha256"]:
            raise OperationError("stale_plan", "Cleanup SSH destination changed")
        inventory = transport.probe(host.id, "inventory", {})
        physical[host.id] = _host_identity(inventory, host, values)
        if physical[host.id]["host_id"] != expected["host_id"]:
            raise OperationError("stale_plan", "Cleanup host identity changed")
        observations[host.id] = inventory
    hashes = {
        str(path): digest(read_input(path))
        for path in (
            target.adapter.settings_path,
            Path(settings.hosts_path),
            Path(settings.known_hosts_path),
        )
        if path is not None
    }
    recipe_entry = next((row for row in target.recipes if row.id == execution["recipe_id"]), None)
    if recipe_entry is None:
        raise OperationError("permission_denied", "Cleanup recipe is no longer registered")
    hashes[str(recipe_entry.path)] = digest(read_input(recipe_entry.path))
    state = deployment_state(target, registry_id=str(context.registry.registry_id))
    execution.update(
        settings=settings.model_dump(mode="json", by_alias=True),
        recipe=recipe_document,
        input_hashes=hashes,
        prior_state=state or execution["prior_state"],
    )
    inputs["runtime_identity"] = encode_record(runtime)
    inputs["host_inventory"] = encode_record(physical)
    inputs["resource_ownership"] = encode_record(state or execution["prior_state"])
    inputs["cleanup_selection"] = encode_record(
        {**operation, "predecessors": predecessors} if predecessors else operation
    )
    identity.update(
        hosts=physical,
        resources=resources,
        input_hashes=hashes,
        cleanup_of=operation["operation_id"],
        cleanup_lineage=[operation["operation_id"], *(row["operation_id"] for row in predecessors)],
        cleanup_selection_sha256=digest(encode_record(_selection(operation))),
    )
    return PreparedPlan(
        identity=identity,
        observations=observations,
        inputs=inputs,
        parameters=parameters,
        stages=ssh_plan.stages(
            "deployment_cleanup",
            settings,
            subjects=[host.id for host in hosts],
            input_names=list(inputs),
        ),
    )


def prepare(
    context: StageContext,
    target: ManagementTarget,
    action: str,
    parameters: dict[str, Any],
) -> PreparedPlan:
    """Inspect physical identities without installing software or starting services."""
    try:
        return _prepare(context, target, action, parameters)
    except OperationError:
        raise
    except AccessError as error:
        raise OperationError(error.code, error.message) from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise OperationError(
            "prerequisite_failed", "SSH deployment inputs or host observations are invalid"
        ) from None


def input_document(context: StageContext, plan: dict[str, Any], name: str) -> dict[str, Any]:
    """Read a retained input through its verified content address."""
    row = next(item for item in plan["payload"]["binding"]["inputs"] if item["name"] == name)
    value = json.loads(PlanStore(context.registry, context.target_id).blob(row["sha256"]))
    if not isinstance(value, dict):
        raise OperationError("invalid_input", "Retained SSH input is not an object")
    return value


def _check(context: StageContext, target: ManagementTarget, plan: dict[str, Any]) -> None:
    from .ssh_transport import SSHTransport

    payload = plan["payload"]
    identity = _snapshot(context, plan)
    runtime = input_document(context, plan, "runtime_identity")
    execution = runtime["execution"]
    settings = ssh_settings.SSHSettings.model_validate_json(encode_record(execution["settings"]))
    recipe = ssh_settings.SSHRecipe.model_validate_json(encode_record(execution["recipe"]))
    cleanup = payload["action"] == "deployment_cleanup"
    for path, expected in execution["input_hashes"].items():
        if digest(read_input(Path(path))) != expected:
            raise OperationError("stale_plan", "Registered input changed after preparation")
    if not cleanup:
        verified = ssh_settings.verify_source(settings, deadline=context.deadline)
        if verified["assets_sha256"] != identity["assets_sha256"]:
            raise OperationError("stale_plan", "Registered source assets changed after preparation")
    fleet = input_document(context, plan, "fleet_config")
    values, hosts = _host_environment(context, target, settings, recipe, fleet)
    if [host.model_dump(mode="json") for host in hosts] != execution["hosts"]:
        raise OperationError("stale_plan", "Registered host selection changed")
    transport = SSHTransport(context, settings, hosts, values)
    inventories = {}
    for host in hosts:
        if (
            digest(values[host.ssh_env].encode())
            != identity["hosts"][host.id]["destination_sha256"]
        ):
            raise OperationError("stale_plan", "Registered SSH destination changed")
        observed = transport.probe(host.id, "inventory", {})
        if _host_identity(observed, host, values) != identity["hosts"][host.id]:
            raise OperationError("stale_plan", "Host identity changed after preparation")
        inventories[host.id] = observed
    if cleanup:
        selected = context.store.read(target.id, payload["parameters"]["operation_id"])
        if digest(encode_record(_selection(selected))) != identity["cleanup_selection_sha256"]:
            raise OperationError("stale_plan", "Cleanup ownership evidence changed")
        return
    roles, launches = _roles(
        settings, hosts, fleet, values, input_document(context, plan, "launch_config")
    )
    endpoints = _endpoints(target, fleet, roles, values)
    literals, _ = _private_roles(target, values, roles)
    if endpoints != identity["endpoints"] or literals != identity["role_environment"]:
        raise OperationError("stale_plan", "Resolved deployment fields changed after preparation")
    management = _management_host()
    if {name: management[name] for name in ("host_id", "netns")} != identity["management_host"]:
        raise OperationError("stale_plan", "Management network identity changed")
    for host in hosts:
        _prerequisites(
            inventories[host.id], [launches[role] for role in host.roles if role != "router"]
        )
        for role in host.roles:
            if (
                role != "router"
                and _physical_gpus(inventories[host.id], launches[role]["gpu_ids"])
                != identity["allocations"][role]["gpu_ids"]
            ):
                raise OperationError("stale_plan", "Selected physical GPU allocation changed")
            if (
                role != "router"
                and _interface(inventories[host.id], roles[role], launches[role])
                != identity["interfaces"][role]
            ):
                raise OperationError(
                    "stale_plan", "Selected fabric interface changed after preparation"
                )
    state = deployment_state(target, registry_id=str(context.registry.registry_id))
    if digest(encode_record(state)) != identity["deployment_state_sha256"]:
        owned_progress = (
            state.get("operation_id") == context.operation_id
            or state.get("last_operation_id") == context.operation_id
        )
        if not owned_progress:
            raise OperationError("stale_plan", "Deployment generation changed after preparation")
    if payload["action"] == "engine_replace":
        _replacement_ready(transport, hosts, fleet, state, endpoints)
    if context.stage.get("operation") == "fleet.launch":
        models, images = {}, {}
        selected_engine = payload["parameters"].get("engine_id")
        for host in hosts:
            selected_gpus = set()
            selected_ports = set()
            for role in host.roles:
                if role == "router" or (
                    selected_engine and role != "engine-" + selected_engine.removeprefix("n")
                ):
                    continue
                model_key = (host.id, roles[role]["NARWHAL_MODEL_DIR"])
                image_key = (host.id, roles[role]["NARWHAL_ENGINE_IMAGE"])
                if model_key not in models:
                    models[model_key] = transport.probe(
                        host.id, "checkpoint", {"model_dir": model_key[1]}
                    )
                if image_key not in images:
                    images[image_key] = transport.probe(host.id, "image", {"image": image_key[1]})
                if (
                    models[model_key]["model_tree_sha256"] != identity["models"][role]
                    or images[image_key]["id"] != identity["images"][role]
                ):
                    raise OperationError(
                        "stale_plan", "Launch model or image changed after preparation"
                    )
                selected_gpus.update(identity["allocations"][role]["gpu_ids"])
                selected_ports.update(identity["allocations"][role]["ports"])
            if payload["action"] in {"fleet_deploy", "engine_replace"}:
                _new_resources_free(inventories[host.id], selected_gpus, selected_ports)


def check(context: StageContext, target: ManagementTarget, plan: dict[str, Any]) -> None:
    """Check immutable bindings while allowing this operation's recorded progress.

    The adapter separately checks live ownership and engine generations. Checkpoint
    bytes and image IDs are read again immediately before the launch stage.
    """
    try:
        _check(context, target, plan)
    except OperationError as error:
        if error.code in {
            "invalid_input",
            "input_missing",
            "prerequisite_failed",
            "resource_conflict",
        }:
            raise OperationError(
                "stale_plan", "SSH plan inputs or identity observations changed"
            ) from None
        raise
    except (AccessError, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise OperationError(
            "stale_plan", "SSH plan inputs or identity observations changed"
        ) from None
