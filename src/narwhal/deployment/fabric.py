"""Qualify every directed edge between engine hosts against the budgets of its source roles.

These functions run the fabric stages of the deployment graph. `transfer-measure` measures the
KV handoffs of each engine whose backend sizes budgets from measured handoffs, before the cache
groups are derived; `narwhal deploy transfer-measure` runs it. `fabric-budget` sizes each cache
group's budget. `fabric-edge` checks the tools, routes and RDMA GID tables of one directed host
edge, then samples it with `iperf3` or `ib_write_bw`, or reuses a retained sample when the link
record is unchanged, and records the sample against the budget of every source role behind the
link. `narwhal deploy fabric` runs those two stages in order and writes each instance's
evidence to `<out>/<stage>/<instance>/`.

Engine roles that share a host, an address, an interface and, on RDMA, an HCA port share one
fabric endpoint, so a directed host pair whose hosts each have one endpoint has one sample.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import shlex
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import product
from pathlib import Path
from typing import Any

from narwhal.contracts import canonical_digest

from .cache_groups import SCHEMA as CACHE_GROUPS_SCHEMA
from .fabric_budget import (
    Workload,
    budget,
    link_fingerprint,
    record_edge,
    retained_evidence,
    reuse_edge,
    sample_gbps,
    transfer_record,
    write_private,
)
from .hosts import HOST_CAUSE, SSH, Host
from .launch_engine.backend import cache_capture, launcher, transfer_measurement
from .role_env import read_environment

TRANSFER_MEASURE = "transfer-measure"
FABRIC_BUDGET = "fabric-budget"
FABRIC_EDGE = "fabric-edge"
# The engine hosts' kernel view of their RDMA devices. Tests point it elsewhere.
INFINIBAND = "/sys/class/infiniband"
# The measurement tool of each transport and the package that provides it.
TOOLS = {"ucx_tcp": ("iperf3", "iperf3"), "ucx_rdma": ("ib_write_bw", "perftest")}
# The other commands a host needs, and their packages.
HOST_TOOLS = (("ip", "iproute2"), ("ss", "iproute2"), ("setsid", "util-linux"))
TCP_OMIT_S = 3
TCP_DURATION_S = 10
RDMA_DURATION_S = 10
RDMA_MESSAGE_BYTES = 1048576
# Seconds the test server waits for its client before it exits, beyond the test itself.
SERVER_WINDOW_S = 60
# Tenths of a second the server start waits for its listener, and the server stop for its exit.
LISTEN_TENTHS = 100
EXIT_TENTHS = 50
# The values of `RDMA_ROCE_MODE`, and the GID type and link layer each requires.
ROCE_MODES = {
    "v2": ("RoCE v2", "Ethernet"),
    "v1": ("IB/RoCE v1", "Ethernet"),
    "infiniband": ("IB/RoCE v1", "InfiniBand"),
}
# The `Link type` a perftest report gives for each link layer.
REPORT_LINK = {"Ethernet": "Ethernet", "InfiniBand": "IB"}
# The route fields that identify a route. Cache fields such as `expires` change by themselves.
ROUTE_FIELDS = ("dst", "from", "gateway", "via", "dev", "prefsrc", "table")
WORKLOAD = (
    ("prompt_tokens", "NARWHAL_FABRIC_PROMPT_TOKENS", int),
    ("handoffs_per_s", "NARWHAL_FABRIC_HANDOFFS_PER_S", float),
    ("burst", "NARWHAL_FABRIC_BURST", int),
    ("transfer_budget_s", "NARWHAL_FABRIC_TRANSFER_BUDGET_S", float),
    ("headroom", "NARWHAL_FABRIC_HEADROOM", float),
)
TEST_PORT = "NARWHAL_FABRIC_TEST_PORT"
PROMPT_TOKENS = WORKLOAD[0][1]
TRANSFER_FILES = ("before.prom", "after.prom", "transfer.json")
# The launch inputs that decide an engine's KV cache. A measured engine's decode peer shares
# them, so the peer accepts its handoffs and resizes for decode as it would.
CACHE_INPUTS = ("accelerator", "tensor_parallel_size", "gpu_visibility_env", "runtime")
# Recovery sections of the fabric gate page.
_PAGE = "deploy/04-Qualify-Fabric.md"
BELOW_BUDGET = _PAGE + "#troubleshooting-an-edge-below-its-budget"


class FabricFailure(RuntimeError):
    """A fabric stage stopped at a named edge, sample or role, with its cause."""

    def __init__(self, stage: str, where: str, cause: str, **context: Any) -> None:
        self.stage = stage
        self.context = {"instance": where, **context}
        super().__init__(f"{where}: {cause}")


class BelowBudget(FabricFailure):
    """A directed sample measured less than a source role's budget."""


@dataclass(frozen=True)
class Rdma:
    """The HCA port and GID entry an engine sends KV through, and the expected RoCE mode."""

    device: str
    port: int
    gid_index: int
    mode: str


@dataclass(frozen=True)
class Endpoint:
    """Where KV leaves or enters a host for one or more engine roles."""

    host: Host
    address: str
    interface: str
    transport: str
    rdma: Rdma | None


@dataclass(frozen=True)
class Engine:
    role: str
    host: Host
    endpoint: Endpoint
    net_devices: str
    tensor_parallel: int
    launch: Path
    env: Mapping[str, str]
    cache_inputs: Mapping[str, Any]
    captures_cache: bool
    measures_transfer: bool


@dataclass(frozen=True)
class Group:
    """A cache-equivalence group. The first role is its representative and names the group."""

    roles: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.roles[0]


@dataclass(frozen=True)
class Sample:
    """One directed measurement between two endpoints of a host edge."""

    source: Endpoint
    destination: Endpoint
    source_roles: tuple[str, ...]
    destination_roles: tuple[str, ...]

    @property
    def name(self) -> str:
        return f"{self.source_roles[0]}-to-{self.destination_roles[0]}"


@dataclass(frozen=True)
class Edge:
    source: Host
    destination: Host
    samples: tuple[Sample, ...]

    @property
    def name(self) -> str:
        return f"{self.source.id}-to-{self.destination.id}"


@dataclass(frozen=True)
class Fabric:
    """Everything the fabric stages read: engines, cache groups, inputs and the package."""

    engines: Mapping[str, Engine]
    groups: tuple[Group, ...]
    workload: Workload
    test_port: int
    remote_dir: str
    # Each representative's captured `cache-layout.json`, on a backend that captures it.
    layouts: Mapping[str, bytes]
    # Each representative's `transfer.json` with the metric snapshots beside it, on a backend
    # that measures its handoffs.
    transfers: Mapping[str, Mapping[str, bytes]]

    def group(self, role: str) -> Group:
        return next(group for group in self.groups if role in group.roles)

    def edges(self) -> tuple[Edge, ...]:
        """Every directed edge between engine hosts, in inventory order."""
        by_host: dict[Host, list[Engine]] = {}
        for engine in self.engines.values():
            by_host.setdefault(engine.host, []).append(engine)
        edges = []
        for source, destination in product(by_host, by_host):
            if source == destination:
                continue
            samples = []
            for outgoing, incoming in product(
                _endpoints(by_host[source]), _endpoints(by_host[destination])
            ):
                sample = Sample(outgoing[0], incoming[0], outgoing[1], incoming[1])
                name = f"{source.id}-to-{destination.id}"
                if sample.source.transport != sample.destination.transport:
                    raise ValueError(
                        f"{name}: {sample.source_roles[0]} sends KV over "
                        f"{sample.source.transport} and {sample.destination_roles[0]} over "
                        f"{sample.destination.transport}, so KV between them would leave the "
                        "configured transport; give both one transfer.transport"
                    )
                samples.append(sample)
            edges.append(Edge(source, destination, tuple(samples)))
        return tuple(edges)

    def digests(self) -> dict[str, str]:
        """The digests of the `fabric_workload` and `fabric_test` inputs of the stage graph."""
        rdma = {
            role: vars(engine.endpoint.rdma)
            for role, engine in sorted(self.engines.items())
            if engine.endpoint.rdma is not None
        }
        return {
            "fabric_workload": canonical_digest(self.workload.fields()),
            "fabric_test": canonical_digest({"test_port": self.test_port, "rdma": rdma}),
        }


def _endpoints(engines: list[Engine]) -> list[tuple[Endpoint, tuple[str, ...]]]:
    roles: dict[Endpoint, list[str]] = {}
    for engine in engines:
        roles.setdefault(engine.endpoint, []).append(engine.role)
    return [(endpoint, tuple(names)) for endpoint, names in roles.items()]


def _number(env: Mapping[str, str], name: str, kind: type) -> Any:
    raw = env.get(name, "")
    if not raw:
        raise ValueError(f"Set {name}; fabric qualification reads it as a deployment input")
    try:
        return kind(raw)
    except ValueError:
        noun = "an integer" if kind is int else "a number"
        raise ValueError(f"{name} must be {noun}, not {raw!r}") from None


def inputs(env: Mapping[str, str]) -> tuple[Workload, int]:
    """Return the budget workload and the test port from the deployment inputs."""
    values = {field: _number(env, name, kind) for field, name, kind in WORKLOAD}
    try:
        workload = Workload(**values)
    except ValueError as error:
        raise ValueError(f"fabric workload: {error}") from None
    port = _number(env, TEST_PORT, int)
    if not 1 <= port <= 65535:
        raise ValueError(f"{TEST_PORT} must be a TCP port between 1 and 65535")
    return workload, port


def _node_value(env: Mapping[str, str], node: int, field: str) -> str:
    name = f"NARWHAL_NODE_{node}_{field}"
    return env[name] if name in env else env.get(f"NARWHAL_{field}", "")


def rdma_input(env: Mapping[str, str], role: str, net_devices: str) -> Rdma:
    """Return the HCA port of a ucx_rdma launch with its GID index and RoCE mode inputs."""
    node = int(role.split("-")[1])
    match = re.fullmatch(r"([A-Za-z0-9_.-]+):([1-9][0-9]*)", net_devices)
    if match is None:
        raise ValueError(
            f"{role}: fabric qualification measures one HCA port per ucx_rdma engine, and "
            f"transfer.net_devices names {net_devices}; select one device:port"
        )
    index = _node_value(env, node, "RDMA_GID_INDEX")
    if not re.fullmatch(r"[0-9]+", index):
        raise ValueError(
            f"Set NARWHAL_RDMA_GID_INDEX or NARWHAL_NODE_{node}_RDMA_GID_INDEX to the GID "
            f"index {role} uses on {net_devices}"
        )
    mode = _node_value(env, node, "RDMA_ROCE_MODE")
    if mode not in ROCE_MODES:
        raise ValueError(
            f"Set NARWHAL_RDMA_ROCE_MODE or NARWHAL_NODE_{node}_RDMA_ROCE_MODE to "
            f"{', '.join(ROCE_MODES)} for {role}"
        )
    return Rdma(match[1], int(match[2]), int(index), mode)


def _node(role: str) -> int:
    return int(role.split("-")[1])


def read_engines(hosts: Sequence[Host], run: Path) -> dict[str, Engine]:
    """Read every engine role of an installed package from its role environment and launch."""
    engines: dict[str, Engine] = {}
    for host in hosts:
        for role in (role for role in host.roles if role.startswith("engine-")):
            role_env = read_environment(run / host.id / f".env.{role}")
            launch_path = run / host.id / f"engine-launch.{role}.json"
            launch = json.loads(launch_path.read_text())
            address = role_env.get(f"NARWHAL_NODE_{_node(role)}_IP", "")
            interface = role_env.get("NARWHAL_FABRIC_INTERFACE", "")
            if not address or not interface:
                raise ValueError(f"{role}: its role environment lacks a fabric address")
            transfer = launch["transfer"]
            inputs = {
                "image": role_env.get("NARWHAL_ENGINE_IMAGE", ""),
                "model_config_sha256": role_env.get("NARWHAL_MODEL_CONFIG_SHA256", ""),
                **{name: launch[name] for name in CACHE_INPUTS},
                "transport": transfer["transport"],
            }
            engine = launcher(launch.get("runtime", {}).get("backend"))
            engines[role] = Engine(
                role,
                host,
                Endpoint(host, address, interface, transfer["transport"], None),
                transfer["net_devices"],
                launch["tensor_parallel_size"],
                launch_path,
                role_env,
                inputs,
                cache_capture(engine) is not None,
                transfer_measurement(engine) is not None,
            )
    return engines


def transfer_files(role: str, data: bytes, path: Path, prompt_tokens: int) -> dict[str, bytes]:
    """Check a `transfer.json` and read the metric snapshots beside it at `path`.

    `prompt_tokens` is the workload's prompt length, which the measurement must have used.
    """
    try:
        files = {name: (path.parent / name).read_bytes() for name in TRANSFER_FILES[:2]}
    except OSError as error:
        raise ValueError(f"{role}: {error}") from None
    files["transfer.json"] = data
    try:
        transfer_record(data, prompt_tokens)
        recorded = json.loads(data)["metrics_sha256"]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{role}: {path}: {error}") from None
    for name in TRANSFER_FILES[:2]:
        if recorded[name] != hashlib.sha256(files[name]).hexdigest():
            raise ValueError(f"{role}: {name} beside {path} is not the one it records")
    return files


def read_groups(path: Path, engines: Mapping[str, Engine]) -> tuple[tuple[Group, ...], dict]:
    """Read the cache groups of `narwhal deploy cache-groups`, checked against the package.

    Returns the groups, each led by its representative, and each representative's geometry
    source: the cache layout or the transfer record the groups were derived from, with the
    file's path. Every role's packaged launch record must be the one the groups read.
    """
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"--groups {path}: {error}") from None
    if document.get("schema") != CACHE_GROUPS_SCHEMA or document.get("schema_version") != 1:
        raise ValueError(f"--groups {path}: expected a {CACHE_GROUPS_SCHEMA} version 1 record")
    roles = document["engines"]
    if sorted(roles) != sorted(engines):
        raise ValueError(
            f"--groups {path} groups {', '.join(sorted(roles))}, but the package holds "
            + ", ".join(sorted(engines))
        )
    for role, engine in engines.items():
        if roles[role]["launch_sha256"] != hashlib.sha256(engine.launch.read_bytes()).hexdigest():
            raise ValueError(
                f"{role}: --groups {path} was derived from another launch record; derive the "
                "groups again from this package"
            )
    groups = tuple(Group(tuple(group["roles"])) for group in document["groups"])
    sources = {}
    for group in groups:
        record = roles[group.name]
        if "geometry_source" not in record:
            continue
        source = Path(record["geometry_source"])
        try:
            data = source.read_bytes()
        except OSError as error:
            raise ValueError(f"{group.name}: {error}") from None
        if hashlib.sha256(data).hexdigest() != record["geometry_source_sha256"]:
            raise ValueError(
                f"{group.name}: {source} changed after the groups were derived; derive them again"
            )
        sources[group.name] = (data, source)
    return groups, sources


def load(
    hosts: Sequence[Host],
    manifest: Mapping[str, Any],
    run: Path,
    env: Mapping[str, str],
    groups_path: Path,
) -> Fabric:
    """Read the engines of an installed package, and the cache groups derived from it.

    `groups_path` is the `cache-groups.json` that `narwhal deploy cache-groups` wrote. Its
    representatives' cache layouts and transfer records size the budgets.
    """
    workload, port = inputs(env)
    engines = read_engines(hosts, run)
    for role, engine in engines.items():
        endpoint, net = engine.endpoint, engine.net_devices
        if endpoint.transport == "ucx_rdma":
            engines[role] = replace(
                engine, endpoint=replace(endpoint, rdma=rdma_input(env, role, net))
            )
        elif net != endpoint.interface:
            raise ValueError(
                f"{role}: KV leaves through transfer.net_devices {net}, but "
                f"NARWHAL_FABRIC_INTERFACE is {endpoint.interface}; qualification measures the "
                "interface KV uses, so set them to one interface"
            )
    cache_groups, sources = read_groups(groups_path, engines)
    for group in cache_groups:
        members = [engines[role] for role in group.roles]
        if len({(m.captures_cache, m.measures_transfer) for m in members}) != 1:
            raise ValueError(f"cache group {group.name} mixes engine backends")
        if not members[0].captures_cache and not members[0].measures_transfer:
            raise ValueError(
                f"{group.name}: its backend neither captures the cache layout nor measures "
                "handoffs, so its fabric budget has no source"
            )
        if group.name not in sources:
            raise ValueError(f"{group.name}: --groups {groups_path} names no geometry source")
    layouts = {role: data for role, (data, _) in sources.items() if engines[role].captures_cache}
    records = {
        role: transfer_files(role, data, source, workload.prompt_tokens)
        for role, (data, source) in sorted(sources.items())
        if engines[role].measures_transfer
    }
    fabric = Fabric(engines, cache_groups, workload, port, manifest["remote_dir"], layouts, records)
    fabric.edges()
    return fabric


def decode_peers(engines: Mapping[str, Engine]) -> dict[str, str]:
    """Choose the decode peer of each engine that measures its handoffs.

    The peer is the next engine role, in role order and wrapping around, among the measuring
    engines with the same image, model configuration and cache launch inputs. An engine with no
    such peer cannot be measured, and its cache group could hold no other engine.
    """
    measuring = sorted(
        (engine for engine in engines.values() if engine.measures_transfer),
        key=lambda engine: _node(engine.role),
    )
    peers = {}
    for engine in measuring:
        alike = [other.role for other in measuring if other.cache_inputs == engine.cache_inputs]
        if len(alike) == 1:
            raise ValueError(
                f"{engine.role}: its backend sizes the fabric budget from handoffs to another "
                "idle engine with the same image, model configuration, accelerator, TP size, "
                "GPU visibility, runtime and transport, and no other engine matches it"
            )
        peers[engine.role] = alike[(alike.index(engine.role) + 1) % len(alike)]
    return peers


def _prelude(remote_dir: str) -> str:
    return f"""set -eu
umask 077
fail() {{ printf '{HOST_CAUSE}%s\\n' "$1" >&2; exit 1; }}
emit() {{
  test -f "$2" && test ! -L "$2" || fail "$2 is missing or not a regular file"
  printf 'narwhal-file %s %s\\n' "$1" "$(base64 -w 0 < "$2")"
}}
cd {shlex.quote(remote_dir)}
"""


def _files(output: str) -> dict[str, bytes]:
    files = {}
    for line in output.splitlines():
        if line.startswith("narwhal-file "):
            _, name, data = line.split(" ", 2)
            try:
                files[name] = base64.b64decode(data, validate=True)
            except binascii.Error:
                raise ValueError(f"the host returned {name} in an unreadable form") from None
    return files


def _fetch(
    ssh: SSH, host: Host, stage: str, operation: str, script: str, names: Sequence[str]
) -> dict[str, bytes]:
    files = _files(ssh.run(host, stage, operation, script))
    if missing := [name for name in names if name not in files]:
        raise FabricFailure(stage, host.id, f"{operation} returned no {', '.join(missing)}")
    return files


def measure_transfer(
    engine: Engine, peer: Engine, ssh: SSH, remote_dir: str, prompt_tokens: int, directory: Path
) -> str:
    """Run `transfer-measure` for one engine, with `peer` switched to decode.

    Writes `before.prom`, `after.prom` and `transfer.json` to `directory`, and returns the
    SHA-256 of `transfer.json`, the stage's `handoff_geometry` product.
    """
    role = engine.role
    prefill = engine.env.get(f"NARWHAL_NODE_{_node(role)}_URL", "")
    decode = peer.env.get(f"NARWHAL_NODE_{_node(peer.role)}_URL", "")
    if not prefill or not decode:
        raise ValueError(
            f"{role}: the role environments lack the engine URL of {role} or {peer.role}"
        )
    scratch = f"runs/fabric/{uuid.uuid4().hex}"
    command = shlex.join(
        [
            "--prefill",
            prefill,
            "--decode",
            decode,
            "--bootstrap-host",
            engine.endpoint.address,
            "--prompt-tokens",
            str(prompt_tokens),
            "--out-dir",
            scratch,
        ]
    )
    # The peer shares this engine's runtime, so this engine's launch record sizes its decode.
    script = (
        _prelude(remote_dir)
        + f""". ./runs/deployment/.env.{role}
test "$(sha256sum "$NARWHAL_TRANSFER_MEASURE_TOOL" | cut -d' ' -f1)" = \\
  "$NARWHAL_TRANSFER_MEASURE_SHA256" ||
  fail "NARWHAL_TRANSFER_MEASURE_TOOL differs from its packaged SHA-256; install the package again"
mkdir -p runs/fabric
trap 'rm -rf {scratch}' EXIT
output=$(python3 -I "$NARWHAL_TRANSFER_MEASURE_TOOL" {command} \\
  --bootstrap-port "$NARWHAL_KV_SIDE_CHANNEL_PORT" --model "$NARWHAL_ENGINE_MODEL_NAME" \\
  --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" 2>&1) ||
  fail "the transfer measurement to {peer.role} failed: $(printf '%s' "$output" | tail -n 1)"
printf '%s\\n' "$output" >&2
for name in before.prom after.prom transfer.json; do emit "$name" "{scratch}/$name"; done
"""
    )
    operation = f"{role} handoffs to {peer.role}"
    files = _fetch(ssh, engine.host, TRANSFER_MEASURE, operation, script, TRANSFER_FILES)
    record = json.loads(files["transfer.json"])
    snapshots = record.get("metrics_sha256", {})
    for name in TRANSFER_FILES[:2]:
        if snapshots.get(name) != hashlib.sha256(files[name]).hexdigest():
            raise FabricFailure(
                TRANSFER_MEASURE, role, f"transfer.json does not record the {name} it came with"
            )
    directory.mkdir(mode=0o700, parents=True)
    for name in TRANSFER_FILES:
        _write(directory / name, files[name])
    return hashlib.sha256(files["transfer.json"]).hexdigest()


def measure_handoffs(
    hosts: Sequence[Host],
    manifest: Mapping[str, Any],
    run: Path,
    env: Mapping[str, str],
) -> tuple[dict[str, Engine], dict[str, str], int]:
    """Read the measuring engines of a package, their decode peers and the prompt length."""
    prompt_tokens = _number(env, PROMPT_TOKENS, int)
    if prompt_tokens < 1:
        raise ValueError(f"{PROMPT_TOKENS} must be a positive integer")
    engines = read_engines(hosts, run)
    peers = decode_peers(engines)
    if not peers:
        raise ValueError(
            "No engine of this package measures its handoffs; its backend captures the cache "
            "layout, so run narwhal-engine capture-cache instead"
        )
    return engines, peers, prompt_tokens


def measure(
    engines: Mapping[str, Engine],
    peers: Mapping[str, str],
    prompt_tokens: int,
    remote_dir: str,
    ssh: SSH,
    out: Path,
) -> dict[str, Any]:
    """Run `transfer-measure` for every measuring engine, writing each to `<out>/<role>/`."""
    summary: dict[str, Any] = {
        "schema": "narwhal.transfer-measurements",
        "schema_version": 1,
        "prompt_tokens": prompt_tokens,
        "engines": {},
    }
    try:
        for role, peer in peers.items():
            folder = out / role
            digest = measure_transfer(
                engines[role], engines[peer], ssh, remote_dir, prompt_tokens, folder
            )
            payload = json.loads((folder / "transfer.json").read_bytes())
            summary["engines"][role] = {
                "peer": peer,
                "transfer_sha256": digest,
                "payload_bytes_per_handoff": payload["payload_bytes_per_handoff"],
            }
            print(
                f"{TRANSFER_MEASURE} {role}: {payload['payload_bytes_per_handoff']} bytes per "
                f"handoff to {peer}",
                flush=True,
            )
    finally:
        write_private(out / "transfer-measure.json", summary)
    return summary


def group_budget(fabric: Fabric, ssh: SSH, group: Group, directory: Path) -> dict:
    """Run `fabric-budget` for one cache group and return its budget.

    Copies the representative's model configuration and launch record from its host into
    `directory`, with the `cache-layout.json` or `transfer-measure` files the groups were
    derived from, then writes `budget.json` there.
    """
    engine = fabric.engines[group.name]
    names = ["model-config.json", "engine-launch.json"]
    script = (
        _prelude(fabric.remote_dir)
        + f""". ./runs/deployment/.env.{group.name}
emit model-config.json "$NARWHAL_MODEL_DIR/config.json"
emit engine-launch.json "$NARWHAL_ENGINE_LAUNCH_CONFIG"
"""
    )
    transfer = fabric.transfers.get(group.name, {})
    layout = fabric.layouts.get(group.name)
    files = _fetch(ssh, engine.host, FABRIC_BUDGET, f"{group.name} budget inputs", script, names)
    if files["engine-launch.json"] != engine.launch.read_bytes():
        raise FabricFailure(
            FABRIC_BUDGET,
            group.name,
            "the host's launch record differs from the package's; install the package again",
        )
    try:
        result = budget(
            files["model-config.json"],
            files["engine-launch.json"],
            fabric.workload,
            runtime_layout=layout,
            transfer=transfer.get("transfer.json"),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise FabricFailure(FABRIC_BUDGET, group.name, str(error)) from None
    result["cache_group"] = list(group.roles)
    directory.mkdir(mode=0o700, parents=True)
    copies = {name: files[name] for name in names}
    if layout is not None:
        copies["cache-layout.json"] = layout
    for name, data in {**copies, **transfer}.items():
        _write(directory / name, data)
    write_private(directory / "budget.json", result)
    return result


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)


def _probe_script(fabric: Fabric, local: Endpoint, peer: Endpoint) -> str:
    tool, package = TOOLS[local.transport]
    needs = "\n".join(
        f"need {name} {package_name}" for name, package_name in (*HOST_TOOLS, (tool, package))
    )
    interface, address, target = map(shlex.quote, (local.interface, local.address, peer.address))
    script = f"""need() {{
  command -v "$1" > /dev/null 2>&1 || fail "the host lacks $1; install the $2 package"
}}
{needs}
printf 'narwhal-tool %s\\n' "$({tool} --version 2>&1 | head -n 1)"
addresses=$(ip -json address show dev {interface} 2>&1) ||
  fail "the host has no interface {local.interface}"
printf 'narwhal-addresses %s\\n' "$(printf '%s' "$addresses" | tr -d '\\n')"
route=$(ip -json route get {target} from {address} 2>&1) ||
  fail "no route from {local.address} to {peer.address}: $route"
printf 'narwhal-route %s\\n' "$(printf '%s' "$route" | tr -d '\\n')"
"""
    if local.rdma is not None:
        rdma = local.rdma
        port = shlex.quote(f"{INFINIBAND}/{rdma.device}/ports/{rdma.port}")
        index = rdma.gid_index
        script += f"""port={port}
test -d "$port" || fail "the host has no RDMA port {rdma.device}:{rdma.port}"
test -r "$port/gids/{index}" ||
  fail "the GID table of {rdma.device}:{rdma.port} has no index {index}"
printf 'narwhal-rdma-link %s\\n' "$(cat "$port/link_layer")"
printf 'narwhal-rdma-state %s\\n' "$(cat "$port/state")"
printf 'narwhal-rdma-gid %s\\n' "$(cat "$port/gids/{index}")"
printf 'narwhal-rdma-type %s\\n' "$(cat "$port/gid_attrs/types/{index}" 2> /dev/null || true)"
printf 'narwhal-rdma-ndev %s\\n' "$(cat "$port/gid_attrs/ndevs/{index}" 2> /dev/null || true)"
"""
    return _prelude(fabric.remote_dir) + script


def _lines(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        if line.startswith("narwhal-"):
            key, _, value = line.partition(" ")
            values[key.removeprefix("narwhal-")] = value.strip()
    return values


def _same(first: str, second: str) -> bool:
    try:
        return ipaddress.ip_address(first) == ipaddress.ip_address(second)
    except ValueError:
        return first == second


def _check_gid(rdma: Rdma, endpoint: Endpoint, values: Mapping[str, str]) -> str | None:
    """Return why a host's GID table entry contradicts the RDMA inputs, or None."""
    name = f"{rdma.device}:{rdma.port}"
    gid_type, link_layer = ROCE_MODES[rdma.mode]
    if "ACTIVE" not in values.get("rdma-state", ""):
        return f"{name} is {values.get('rdma-state') or 'in no state'}, not ACTIVE"
    if values.get("rdma-link") != link_layer:
        return (
            f"{name} has link layer {values.get('rdma-link')}, and RoCE mode {rdma.mode} "
            f"needs {link_layer}"
        )
    found = values.get("rdma-type", "")
    if found != gid_type:
        return (
            f"GID {rdma.gid_index} of {name} has type {found or 'none'}, and RoCE mode "
            f"{rdma.mode} needs {gid_type}"
        )
    try:
        gid = ipaddress.IPv6Address(values.get("rdma-gid", ""))
    except ValueError:
        return f"GID {rdma.gid_index} of {name} is unreadable"
    if gid == ipaddress.IPv6Address(0):
        return f"GID {rdma.gid_index} of {name} is unset"
    if rdma.mode == "infiniband":
        return None
    if values.get("rdma-ndev") != endpoint.interface:
        return (
            f"GID {rdma.gid_index} of {name} belongs to {values.get('rdma-ndev') or 'no device'}"
            f", not NARWHAL_FABRIC_INTERFACE {endpoint.interface}"
        )
    address = ipaddress.ip_address(endpoint.address)
    holds = gid.ipv4_mapped == address if address.version == 4 else gid == address
    if not holds:
        return f"GID {rdma.gid_index} of {name} is {gid}, not the fabric address {address}"
    return None


def _route(
    where: str, endpoint: Endpoint, peer: Endpoint, values: Mapping[str, str]
) -> dict[str, Any]:
    """Check a host's route to its peer and return the fields that identify it."""
    host = endpoint.host.id
    try:
        routes = json.loads(values["route"])
        interfaces = json.loads(values["addresses"])
    except (KeyError, json.JSONDecodeError):
        raise FabricFailure(FABRIC_EDGE, where, f"{host} returned no readable route") from None
    if not isinstance(routes, list) or len(routes) != 1 or not isinstance(routes[0], dict):
        raise FabricFailure(FABRIC_EDGE, where, f"{host} returned no single route")
    route = {name: routes[0][name] for name in ROUTE_FIELDS if name in routes[0]}
    path = f"the route from {endpoint.address} to {peer.address} on {host}"
    if route.get("dev") != endpoint.interface:
        raise FabricFailure(
            FABRIC_EDGE,
            where,
            f"{path} leaves through {route.get('dev')}, not NARWHAL_FABRIC_INTERFACE "
            f"{endpoint.interface}",
            host=host,
        )
    source = route.get("prefsrc", route.get("from", endpoint.address))
    if not _same(str(source), endpoint.address):
        raise FabricFailure(FABRIC_EDGE, where, f"{path} uses source address {source}", host=host)
    local = {
        info.get("local")
        for interface in interfaces
        if isinstance(interface, dict)
        for info in interface.get("addr_info", [])
    }
    if not any(_same(str(value), endpoint.address) for value in local if value):
        raise FabricFailure(
            FABRIC_EDGE,
            where,
            f"{endpoint.address} is not an address of {endpoint.interface} on {host}",
            host=host,
        )
    return route


def _test_parameters(fabric: Fabric, sample: Sample, gids: Sequence[str]) -> dict[str, Any]:
    if sample.source.rdma is None or sample.destination.rdma is None:
        streams = {fabric.engines[role].tensor_parallel for role in sample.source_roles}
        if len(streams) != 1:
            raise ValueError(f"{sample.name}: the source roles differ in tensor parallel size")
        return {
            "port": fabric.test_port,
            "parallel": streams.pop(),
            "omit_s": TCP_OMIT_S,
            "duration_s": TCP_DURATION_S,
        }
    source, destination = sample.source.rdma, sample.destination.rdma
    return {
        "port": fabric.test_port,
        "source_device": source.device,
        "source_port": source.port,
        "source_gid_index": source.gid_index,
        "source_gid": gids[0],
        "destination_device": destination.device,
        "destination_port": destination.port,
        "destination_gid_index": destination.gid_index,
        "destination_gid": gids[1],
        "roce_mode": source.mode,
        "message_bytes": RDMA_MESSAGE_BYTES,
        "duration_s": RDMA_DURATION_S,
    }


@dataclass(frozen=True)
class Observation:
    """The live state of one sample's link: its link record and each host's probe."""

    sample: Sample
    link: dict[str, Any]
    probes: tuple[dict[str, Any], dict[str, Any]]

    @property
    def fingerprint(self) -> str:
        return link_fingerprint(self.link)


def observe(fabric: Fabric, ssh: SSH, edge: Edge) -> tuple[Observation, ...]:
    """Check the tools, routes and GID tables behind each sample of `edge`, without sampling.

    Returns each sample's current link record. The `link` product of `fabric-edge` is the
    fingerprint of each record, so this is also the stage's live check.
    """
    observations = []
    for sample in edge.samples:
        where = f"{edge.name} {sample.name}"
        probes = []
        for endpoint, peer in (
            (sample.source, sample.destination),
            (sample.destination, sample.source),
        ):
            output = ssh.run(
                endpoint.host,
                FABRIC_EDGE,
                f"{where} tools and route on {endpoint.host.id}",
                _probe_script(fabric, endpoint, peer),
            )
            values = _lines(output)
            if not values.get("tool"):
                raise FabricFailure(
                    FABRIC_EDGE,
                    where,
                    f"{endpoint.host.id} reported no {TOOLS[endpoint.transport][0]} version",
                )
            route = _route(where, endpoint, peer, values)
            probe: dict[str, Any] = {
                "host": endpoint.host.id,
                "tool_version": values["tool"],
                "route": route,
            }
            if endpoint.rdma is not None:
                if cause := _check_gid(endpoint.rdma, endpoint, values):
                    raise FabricFailure(
                        FABRIC_EDGE, where, f"{endpoint.host.id}: {cause}", host=endpoint.host.id
                    )
                probe["gid"] = {
                    "device": endpoint.rdma.device,
                    "port": endpoint.rdma.port,
                    "index": endpoint.rdma.gid_index,
                    "gid": values["rdma-gid"],
                    "type": values["rdma-type"],
                    "ndev": values.get("rdma-ndev", ""),
                    "link_layer": values["rdma-link"],
                }
            probes.append(probe)
        gids = [probe["gid"]["gid"] for probe in probes if "gid" in probe]
        link = {
            "source_host": sample.source.host.id,
            "destination_host": sample.destination.host.id,
            "source_address": sample.source.address,
            "destination_address": sample.destination.address,
            "source_interface": sample.source.interface,
            "destination_interface": sample.destination.interface,
            "source_route": json.dumps(probes[0]["route"], sort_keys=True),
            "destination_route": json.dumps(probes[1]["route"], sort_keys=True),
            "transport": sample.source.transport,
            "tool_version": f"{probes[0]['tool_version']} / {probes[1]['tool_version']}",
            "test_parameters": _test_parameters(fabric, sample, gids),
        }
        observations.append(Observation(sample, link, (probes[0], probes[1])))
    return tuple(observations)


def _client_argv(fabric: Fabric, sample: Sample) -> tuple[list[str], list[str]]:
    """Return the server and client commands of one directed sample."""
    source, destination = sample.source, sample.destination
    port = str(fabric.test_port)
    if source.rdma is None or destination.rdma is None:
        streams = str(fabric.engines[sample.source_roles[0]].tensor_parallel)
        server = ["iperf3", "--server", "--one-off", "--bind", destination.address, "--port", port]
        client = [
            *("iperf3", "--client", destination.address, "--bind", source.address),
            *("--port", port, "--parallel", streams, "--omit", str(TCP_OMIT_S)),
            *("--time", str(TCP_DURATION_S), "--json"),
        ]
        return server, client

    def perftest(rdma: Rdma, address: str) -> list[str]:
        family = ["--ipv6-addr", "--ipv6"] if ":" in destination.address else []
        return [
            *("ib_write_bw", "-d", rdma.device, "-i", str(rdma.port), "-x", str(rdma.gid_index)),
            *("-p", port, "-s", str(RDMA_MESSAGE_BYTES), "-D", str(RDMA_DURATION_S)),
            *("--report_gbits", *family, "--bind_source_ip", address),
        ]

    return perftest(destination.rdma, destination.address), [
        *perftest(source.rdma, source.address),
        destination.address,
    ]


def _listeners(fabric: Fabric, ssh: SSH, where: str, host: Host) -> None:
    port = fabric.test_port
    ssh.run(
        host,
        FABRIC_EDGE,
        f"{where} test port on {host.id}",
        _prelude(fabric.remote_dir)
        + f"""busy=$(ss -Hltn "sport = :{port}")
test -z "$busy" || fail "test port {port} is in use: $(printf '%s' "$busy" | head -n 1)"
""",
    )


def _sample(fabric: Fabric, ssh: SSH, where: str, sample: Sample) -> bytes:
    """Run the server on the destination and the client on the source; return the report."""
    for host in (sample.source.host, sample.destination.host):
        _listeners(fabric, ssh, where, host)
    server, client = _client_argv(fabric, sample)
    scratch = shlex.quote(f"runs/fabric/{uuid.uuid4().hex}")
    window = SERVER_WINDOW_S + TCP_OMIT_S + max(TCP_DURATION_S, RDMA_DURATION_S)
    port = fabric.test_port
    destination = sample.destination.host
    start = (
        _prelude(fabric.remote_dir)
        + f"""mkdir -p runs/fabric
mkdir {scratch}
setsid timeout -k 5 {window} {shlex.join(server)} > {scratch}/server.log 2>&1 < /dev/null &
pid=$!
ticks=$(sed 's/^.*) //' /proc/$pid/stat | cut -d' ' -f20)
printf '%s %s\\n' "$pid" "$ticks" > {scratch}/server.pid
tries=0
until ss -Hltn "sport = :{port}" | grep -q .; do
  kill -0 "$pid" 2> /dev/null ||
    fail "the {server[0]} server exited: $(tail -n 1 {scratch}/server.log)"
  tries=$((tries + 1))
  test "$tries" -lt {LISTEN_TENTHS} ||
    fail "the {server[0]} server did not listen on port {port}"
  sleep 0.1
done
"""
    )
    stop = (
        _prelude(fabric.remote_dir)
        + f"""test -f {scratch}/server.pid || exit 0
read -r pid ticks < {scratch}/server.pid
alive() {{
  test -r "/proc/$pid/stat" &&
    test "$(sed 's/^.*) //' "/proc/$pid/stat" | cut -d' ' -f20)" = "$ticks"
}}
tries=0
while alive && test "$tries" -lt {EXIT_TENTHS}; do sleep 0.1; tries=$((tries + 1)); done
if alive; then kill -TERM "$pid"; fi
cat {scratch}/server.log >&2 || true
rm -rf {scratch}
"""
    )
    ssh.run(destination, FABRIC_EDGE, f"{where} {server[0]} server on {destination.id}", start)
    try:
        report = ssh.run(
            sample.source.host,
            FABRIC_EDGE,
            f"{where} {client[0]} client on {sample.source.host.id}",
            _prelude(fabric.remote_dir)
            + f"""status=0
{shlex.join(client)} || status=$?
test "$status" -eq 0 || fail "the {client[0]} client exited with status $status"
""",
        )
    finally:
        ssh.run(destination, FABRIC_EDGE, f"{where} {server[0]} server stop", stop)
    return report.encode() + b"\n"


def _report_fields(report: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value in re.findall(r"([A-Za-z][A-Za-z0-9 ._*-]*?)\s*:\s*(\S+)", report):
        fields.setdefault(key.strip(), value)
    return fields


def _check_sample(fabric: Fabric, where: str, sample: Sample, data: bytes) -> None:
    """Stop when a sample ran between other addresses, devices or another transport."""
    source, destination = sample.source, sample.destination
    if source.rdma is None or destination.rdma is None:
        try:
            report = json.loads(data)
            test = report["start"]["test_start"]
            connected = report["start"]["connected"]
            sample_gbps("ucx_tcp", data)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise FabricFailure(FABRIC_EDGE, where, f"unusable iperf3 sample: {error}") from None
        if test.get("num_streams") != fabric.engines[sample.source_roles[0]].tensor_parallel:
            raise FabricFailure(FABRIC_EDGE, where, "the iperf3 sample ran another stream count")
        if not connected or any(
            not _same(str(stream.get("local_host")), source.address)
            or not _same(str(stream.get("remote_host")), destination.address)
            or stream.get("remote_port") != fabric.test_port
            for stream in connected
        ):
            raise FabricFailure(
                FABRIC_EDGE,
                where,
                f"the iperf3 streams did not run from {source.address} to "
                f"{destination.address}:{fabric.test_port}",
            )
        return
    text = data.decode(errors="replace")
    fields = _report_fields(text)
    link_layer = ROCE_MODES[source.rdma.mode][1]
    expected = {
        "Device": source.rdma.device,
        "Link type": REPORT_LINK[link_layer],
        "Connection type": "RC",
    }
    if link_layer == "Ethernet" or "GID index" in fields:
        expected["GID index"] = str(source.rdma.gid_index)
    for name, value in expected.items():
        if fields.get(name) != value:
            raise FabricFailure(
                FABRIC_EDGE,
                where,
                f"the perftest report names {name} {fields.get(name) or 'nothing'}, not {value}",
            )
    try:
        sample_gbps("ucx_rdma", data, RDMA_MESSAGE_BYTES)
    except ValueError as error:
        raise FabricFailure(FABRIC_EDGE, where, f"unusable perftest report: {error}") from None


def _sample_name(sample: Sample) -> str:
    return "sample.txt" if sample.source.rdma is not None else "sample.json"


def _retained(folder: Path | None, name: str, fingerprint: str) -> tuple[Path, Path] | None:
    """Return a retained sample and one of its evidence files that hold for `fingerprint`."""
    if folder is None:
        return None
    for root in (folder, folder / "retained"):
        sample = root / name
        if not sample.is_file():
            continue
        data = sample.read_bytes()
        for evidence in sorted(root.glob("*.evidence.json")):
            try:
                record = json.loads(evidence.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if retained_evidence(record, fingerprint, data):
                return sample, evidence
    return None


def qualify_edge(
    fabric: Fabric,
    ssh: SSH,
    edge: Edge,
    budgets: Mapping[str, Path],
    directory: Path,
    retained: Path | None = None,
    results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run `fabric-edge` for one directed host edge and return each sample's results.

    `budgets` maps each engine role to its cache group's `budget.json`. `retained` is an
    earlier attempt directory of this edge. A sample whose link record matches a retained
    sample's evidence is compared with the current budgets instead of measured again.
    Each sample's result is appended to `results` as it completes. Raises `BelowBudget`
    after writing the evidence of a sample below a role's budget.
    """
    directory.mkdir(mode=0o700, parents=True)
    results = [] if results is None else results
    for observation in observe(fabric, ssh, edge):
        sample = observation.sample
        where = f"{edge.name} {sample.name}"
        folder = directory / sample.name
        folder.mkdir(mode=0o700)
        write_private(folder / "source.json", observation.probes[0])
        write_private(folder / "destination.json", observation.probes[1])
        write_private(folder / "link.json", observation.link)
        fingerprint = observation.fingerprint
        name = _sample_name(sample)
        found = _retained(retained / sample.name if retained else None, name, fingerprint)
        records = {}
        if found is not None:
            kept = folder / "retained"
            kept.mkdir(mode=0o700)
            shutil.copyfile(found[0], kept / name)
            shutil.copyfile(found[1], kept / found[1].name)
            for path in kept.iterdir():
                path.chmod(0o600)
            for role in sample.source_roles:
                records[role] = reuse_edge(
                    folder / "link.json",
                    kept / found[1].name,
                    kept / name,
                    budgets[role],
                    role,
                    folder / f"{role}.comparison.json",
                )
        else:
            # A runner that starts a sample's server and client itself, as the Kubernetes
            # target does with Jobs, samples it; an SSH host runs both from one session each.
            sampler = getattr(ssh, "sample", None)
            data = (
                sampler(fabric, where, sample) if sampler else _sample(fabric, ssh, where, sample)
            )
            _write(folder / name, data)
            _check_sample(fabric, where, sample, data)
            for role in sample.source_roles:
                records[role] = record_edge(
                    folder / "link.json",
                    budgets[role],
                    folder / name,
                    role,
                    folder / f"{role}.evidence.json",
                )
        result = {
            "edge": edge.name,
            "sample": sample.name,
            "source_roles": list(sample.source_roles),
            "destination_roles": list(sample.destination_roles),
            "transport": sample.source.transport,
            "reused": found is not None,
            "link_sha256": fingerprint,
            "measured_gbps": next(iter(records.values()))["measured_gbps"],
            "roles": {
                role: {"required_gbps": record["required_gbps"], "passed": record["passed"]}
                for role, record in records.items()
            },
        }
        results.append(result)
        if failed := [role for role, record in records.items() if not record["passed"]]:
            role = failed[0]
            raise BelowBudget(
                FABRIC_EDGE,
                where,
                f"measured {result['measured_gbps']:.3f} Gbit/s, below the "
                f"{records[role]['required_gbps']:.3f} Gbit/s budget of {role}; see "
                f"{BELOW_BUDGET}",
                role=role,
                measured_gbps=result["measured_gbps"],
                required_gbps=records[role]["required_gbps"],
            )
    return results


def qualify(fabric: Fabric, ssh: SSH, out: Path, retained: Path | None = None) -> dict[str, Any]:
    """Run every fabric stage in order, writing each instance to `<out>/<stage>/<instance>/`."""
    summary: dict[str, Any] = {
        "schema": "narwhal.fabric-qualification",
        "schema_version": 1,
        "inputs": fabric.digests(),
        "budgets": {},
        "edges": [],
    }
    budgets = {}
    for group in fabric.groups:
        folder = out / FABRIC_BUDGET / group.name
        result = group_budget(fabric, ssh, group, folder)
        for role in group.roles:
            budgets[role] = folder / "budget.json"
        summary["budgets"][group.name] = {
            "roles": list(group.roles),
            "required_gbps": result["required_gbps"],
        }
        print(
            f"{FABRIC_BUDGET} {group.name} ({', '.join(group.roles)}): "
            f"{result['required_gbps']:.6f} Gbit/s per directed host edge",
            flush=True,
        )
    edges = fabric.edges()
    if not edges:
        print(
            "No directed host edges: every engine runs on one host. Preflight checks "
            "transfers between engines of one host.",
            flush=True,
        )
    try:
        for edge in edges:
            folder = out / FABRIC_EDGE / edge.name
            earlier = retained / FABRIC_EDGE / edge.name if retained else None
            done = len(summary["edges"])
            qualify_edge(fabric, ssh, edge, budgets, folder, earlier, summary["edges"])
            for result in summary["edges"][done:]:
                verdicts = ", ".join(
                    f"{role} needs {verdict['required_gbps']:.3f}"
                    for role, verdict in result["roles"].items()
                )
                how = "retained sample" if result["reused"] else "measured"
                print(
                    f"{FABRIC_EDGE} {edge.name} {result['sample']}: {how} "
                    f"{result['measured_gbps']:.3f} Gbit/s; {verdicts}; passed",
                    flush=True,
                )
    finally:
        write_private(out / "fabric.json", summary)
    return summary
