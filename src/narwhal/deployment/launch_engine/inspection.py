"""`narwhal-engine inspect-host`: check an engine host against its launch record before launch.

The inspection runs every check, records what it observed in `host-inspection.json`, and fails
when any check failed, naming each one. A check that cannot run, such as a missing tool, fails
that check only.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from ...runtime.listeners import check_engine_bind, check_http_bind
from .. import stages
from .accelerators import gpu_inventory
from .backend import engine_backend, plan_launcher
from .docker import docker
from .evidence import CheckFailed, evidence_directory, write_json
from .plan import build, verify_model
from .runtime import digest

SCHEMA = "narwhal.host-inspection"
EVIDENCE = "host-inspection.json"
LOG = "inspection.log"
# Holds the label of the containers the inspection runs, so a timed-out run can be removed.
OWNER = "docker-owner.json"
# The launch record fields that fix the engine's allocation and launch policy.
ALLOCATION = (
    "role",
    "accelerator",
    "gpu_ids",
    "tensor_parallel_size",
    "accelerator_devices",
    "network_mode",
    "transfer",
    "environment",
)
# Prints the credentials of a process that the engine image starts, with its user namespace
# mappings. The serving container runs as the image's user with the image's groups, because
# its launch sets neither.
CREDENTIALS = (
    "import json, os; print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), "
    "'groups': sorted(set(os.getgroups())), "
    "'uid_map': open('/proc/self/uid_map').read(), "
    "'gid_map': open('/proc/self/gid_map').read()}))"
)
ACL = "system.posix_acl_access"


class Inputs(NamedTuple):
    """The launch inputs an inspection checks, as `prepare` will read them."""

    source: Path
    record: dict
    plan: dict
    values: dict[str, str]
    env: Mapping[str, str]
    backend: str

    @property
    def node(self) -> str:
        return str(self.record["role"]).removeprefix("engine-")


def load_inputs(env: Mapping[str, str], backend: str, out: Path) -> Inputs:
    """Read the launch record and build the plan that `prepare` would, without writing it.

    `out` stands in for the launch directory, which does not exist yet; the inspection reads
    only the plan's addresses, ports and devices.
    """
    source = Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"])
    record = json.loads(source.read_text())
    if not isinstance(record, dict):
        raise ValueError(f"{source}: expected a JSON object")
    plan, values = build(record, dict(env), out, backend=backend)
    inputs = Inputs(source, record, plan, values, env, backend)
    # The checks read these inputs; a missing or malformed one is an input error, not a failed
    # check, so it stops the inspection before any check runs.
    for name in ("NARWHAL_FABRIC_INTERFACE", f"NARWHAL_NODE_{inputs.node}_IP", "NARWHAL_RUN_DIR"):
        if not env.get(name):
            raise KeyError(name)
    ipaddress.ip_address(env[f"NARWHAL_NODE_{inputs.node}_IP"])
    attestation_address(env, inputs.node)
    return inputs


def attestation_address(env: Mapping[str, str], node: str) -> tuple[str, int]:
    """The host and port of the engine's attestation URL."""
    name = f"NARWHAL_NODE_{node}_ATTESTATION_URL"
    url = env.get(name)
    if not url:
        raise KeyError(name)
    attestation = urlsplit(url)
    host, port = attestation.hostname, attestation.port
    if attestation.scheme != "http" or host is None or port is None:
        raise ValueError(f"{name} {url!r} names no HTTP host and port")
    return host, port


def can_read_write(info: os.stat_result, uid: int, gid: int, groups: list[int]) -> bool:
    """Whether a process with these credentials may open the file for reading and writing.

    Root may. Otherwise the owner bits apply to the owner, the group bits to a member of the
    file's group, and the other bits to everyone else, as the kernel's mode check applies them.
    The IDs must be those of the inspection's user namespace, and the file must carry no access
    control list, whose mask
    replaces the group bits. The inspection refuses the other cases before it calls this.
    """
    if uid == 0:
        return True
    mode = info.st_mode
    if info.st_uid == uid:
        return mode & 0o600 == 0o600
    if info.st_gid == gid or info.st_gid in groups:
        return mode & 0o060 == 0o060
    return mode & 0o006 == 0o006


def has_acl(path: str) -> bool:
    """Whether the file carries an access control list beyond its mode bits."""
    try:
        os.getxattr(path, ACL)
    except OSError as error:
        if error.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return False
        raise
    return True


def _mapping(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines()]


def same_ids(credentials: Mapping[str, object]) -> list[str]:
    """Why the engine user's IDs differ from those this inspection reads device owners in.

    The inspection reads each device's owner and group in its own user namespace. Those match
    the engine user's IDs only when the engine runs in the same namespace, as a native engine
    does, and as a container does unless rootless Docker or userns-remap gives it its own.
    """
    problems = []
    for name in ("uid_map", "gid_map"):
        mapping = credentials.get(name)
        own = Path("/proc/self", name).read_text()
        if not isinstance(mapping, str):
            problems.append(f"the engine user's {name} is unknown")
        elif _mapping(mapping) != _mapping(own):
            ranges = "; ".join(" ".join(line) for line in _mapping(mapping))
            problems.append(
                f"the engine user runs in another user namespace ({name} {ranges!r}), so its IDs "
                "are not the IDs that the device owners name"
            )
    return problems


def inspect_host(out: Path, env: Mapping[str, str], backend: str) -> dict:
    """Inspect this host for the engine role that NARWHAL_ENGINE_LAUNCH_CONFIG selects."""
    out = evidence_directory(out, (EVIDENCE, LOG, OWNER))
    return inspect(out, load_inputs(env, backend, out))


def inspect(out: Path, inputs: Inputs) -> dict:
    """Run every check into the evidence directory `out`, and return the inspection record."""
    return _Inspection(out, inputs).run()


class _Inspection:
    def __init__(self, out: Path, inputs: Inputs) -> None:
        self.out = out
        self.inputs = inputs
        self.record = inputs.record
        self.report: dict = {}
        self.failures: list[str] = []
        self.image_id: str | None = None
        self.user: dict | None = None
        # The device nodes of the selected GPUs and those every GPU needs.
        self.gpu_devices: list[str] = []

    def run(self) -> dict:
        container = self.inputs.backend == "container"
        checks: list[tuple[str, Callable[[dict], None]]] = [("accelerators", self.accelerators)]
        if container:
            checks.append(("image", self.image))
        checks += [
            ("engine_user", self.engine_user),
            ("model", self.model),
            ("run_directory", self.run_directory),
            ("fabric_interface", self.fabric_interface),
            ("ports", self.ports),
            ("devices", self.devices),
        ]
        for name, check in checks:
            self.check(name, check)
        document = {
            "schema": SCHEMA,
            "schema_version": 1,
            "role": self.record["role"],
            "backend": self.inputs.backend,
            "launch_config": str(self.inputs.source),
            "launch_config_sha256": digest(self.inputs.source),
            "allocation": {field: self.record.get(field) for field in ALLOCATION},
            **self.report,
            "failures": self.failures,
            "passed": not self.failures,
        }
        path = self.out / EVIDENCE
        write_json(path, document)
        if self.failures:
            raise CheckFailed(f"{'; '.join(self.failures)}; inspect {path}")
        return document

    def check(self, name: str, function: Callable[[dict], None]) -> None:
        observed: dict = {}
        self.report[name] = observed
        try:
            function(observed)
        except stages.StageTimeout:
            raise
        except (CheckFailed, OSError, ValueError, RuntimeError, KeyError) as error:
            detail = f"{error.args[0]} is unset" if isinstance(error, KeyError) else str(error)
            self.failures.append(f"{name}: {detail}")

    def command(self, args: list[str]) -> str:
        log = self.out / LOG
        result = stages.run(args, stage="engine-inspect", log=log)
        if result.returncode:
            raise RuntimeError(f"{args[0]} exited {result.returncode}; inspect {log}")
        return result.stdout

    def accelerators(self, observed: dict) -> None:
        runtime, gpus, common = gpu_inventory(self.command)
        observed.update(runtime=runtime, gpus=gpus, common_devices=common)
        visibility = self.record["gpu_visibility_env"]
        expected = "rocm" if visibility == "ROCR_VISIBLE_DEVICES" else "cuda"
        problems = []
        if runtime != expected:
            problems.append(f"the host's GPUs run {runtime}, but {visibility} selects {expected}")
        selected = []
        for name in self.record["gpu_ids"]:
            matches = [gpu for gpu in gpus if name in (gpu["id"], gpu.get("uuid"))]
            if matches:
                selected.append(matches[0])
            else:
                problems.append(f"gpu_ids names {name}, which is no GPU index or UUID on this host")
        ids = [gpu["id"] for gpu in selected]
        observed["selected"] = ids
        if len(set(ids)) != len(ids):
            problems.append("gpu_ids names one GPU twice")
        declared = self.record["accelerator"]
        problems += [
            f"GPU {gpu['id']} is {gpu['name']!r}, not the declared accelerator {declared!r}"
            for gpu in selected
            if gpu["name"] != declared
        ]
        required = [("the KFD", "/dev/kfd")] if runtime == "rocm" else []
        required += [(f"GPU {gpu['id']}", gpu["device"]) for gpu in selected]
        self.gpu_devices = list(dict.fromkeys([*common, *(device for _, device in required)]))
        if self.inputs.backend == "container":
            # The launch maps these device nodes into the container, so they must be the nodes
            # of the GPUs that gpu_ids selects.
            for owner, device in required:
                if device not in self.record["accelerator_devices"]:
                    problems.append(f"accelerator_devices lacks {device} for {owner}")
        if problems:
            raise CheckFailed("; ".join(problems))

    def image(self, observed: dict) -> None:
        image = self.inputs.env["NARWHAL_ENGINE_IMAGE"]
        inspection = json.loads(docker(["image", "inspect", image], self.out, LOG))[0]
        digests = inspection.get("RepoDigests") or []
        observed.update(
            configured=image,
            id=inspection["Id"],
            repo_digests=digests,
            user=(inspection.get("Config") or {}).get("User") or "",
        )
        if image.startswith("sha256:"):
            if inspection["Id"] != image:
                raise CheckFailed(f"the local image ID is {inspection['Id']}, not {image}")
        elif image not in digests:
            raise CheckFailed(f"{image} is not among the local image's registry digests")
        self.image_id = inspection["Id"]

    def engine_user(self, observed: dict) -> None:
        if self.inputs.backend == "container":
            if self.image_id is None:
                raise CheckFailed("the image check failed, so the image's user is unknown")
            output = docker(
                [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--entrypoint",
                    "python3",
                    self.image_id,
                    "-c",
                    CREDENTIALS,
                ],
                self.out,
                LOG,
            )
            lines = output.splitlines()
            credentials = json.loads(lines[-1]) if lines else None
            if not isinstance(credentials, dict):
                raise CheckFailed(f"the image's python3 printed no credentials; inspect {LOG}")
            observed["source"] = "image"
        else:
            credentials = {
                "uid": os.getuid(),
                "gid": os.getgid(),
                "groups": sorted(set(os.getgroups())),
                "uid_map": Path("/proc/self/uid_map").read_text(),
                "gid_map": Path("/proc/self/gid_map").read_text(),
            }
            observed["source"] = "process"
        user = {name: credentials[name] for name in ("uid", "gid", "groups")}
        observed.update(
            user, uid_map=credentials.get("uid_map"), gid_map=credentials.get("gid_map")
        )
        if type(user["uid"]) is not int or type(user["gid"]) is not int:
            raise CheckFailed("the engine user's credentials are not numeric IDs")
        if problems := same_ids(credentials):
            raise CheckFailed("; ".join(problems))
        self.user = user

    def model(self, observed: dict) -> None:
        # Prepare reads the model through the same function, so the two follow the same links
        # and refuse the same entries.
        config = Path(self.inputs.env["NARWHAL_MODEL_DIR"]) / "config.json"
        observed["config_sha256"] = digest(config)
        files = verify_model(self.inputs.plan, self.inputs.env, self.inputs.backend)
        observed.update(directory_sha256=files.directory, tree_sha256=files.tree)
        if files.model_file is not None:
            observed.update(path=self.inputs.plan["model_path"], sha256=files.model_file)

    def run_directory(self, observed: dict) -> None:
        path = Path(self.inputs.env["NARWHAL_RUN_DIR"])
        observed["path"] = str(path)
        if not path.is_dir():
            raise CheckFailed(f"NARWHAL_RUN_DIR {path} is not a directory")

    def fabric_interface(self, observed: dict) -> None:
        name = self.inputs.env["NARWHAL_FABRIC_INTERFACE"]
        address = ipaddress.ip_address(self.inputs.env[f"NARWHAL_NODE_{self.inputs.node}_IP"])
        interfaces = json.loads(self.command(["ip", "-json", "address", "show", "dev", name]))
        if not interfaces:
            raise CheckFailed(f"{name} is not an interface on this host")
        flags = interfaces[0].get("flags", [])
        addresses = [
            entry["local"] for entry in interfaces[0].get("addr_info", []) if "local" in entry
        ]
        observed.update(
            name=name,
            flags=flags,
            operstate=interfaces[0].get("operstate"),
            addresses=addresses,
            fabric_address=str(address),
        )
        problems = []
        if "UP" not in flags:
            problems.append(f"{name} is down")
        if str(address) not in {str(ipaddress.ip_address(local)) for local in addresses}:
            problems.append(f"{name} does not hold NARWHAL_NODE_{self.inputs.node}_IP {address}")
        if problems:
            raise CheckFailed("; ".join(problems))

    def ports(self, observed: dict) -> None:
        plan, node = self.inputs.plan, self.inputs.node
        fabric = engine_backend(plan.get("engine")).fabric
        endpoint = urlsplit(plan["endpoint"])
        host, port = endpoint.hostname, endpoint.port
        if host is None or port is None:
            raise CheckFailed(f"the engine URL {plan['endpoint']} names no host and port")
        # A native engine binds the endpoint's host. A container engine on the host network
        # binds every address of the endpoint's family, as the plan's serving arguments say.
        if self.inputs.backend != "native":
            host = "::" if ":" in host else "0.0.0.0"
        engine = plan_launcher(plan)
        side_host, side_port = engine.side_channel_address(self.inputs.values)
        attest_host, attest_port = attestation_address(self.inputs.env, node)
        listeners: list[tuple[str, str, int, Callable[[], None]]] = [
            ("engine", host, port, lambda: fabric.check_engine_bind(host, port)),
            (
                engine.side_channel,
                side_host,
                side_port,
                lambda: fabric.check_engine_bind(side_host, side_port, fabric=True),
            ),
            (
                "attestation",
                attest_host,
                attest_port,
                lambda: check_http_bind(attest_host, attest_port),
            ),
        ]
        problems = []
        observed["listeners"] = []
        for label, address, number, bind in listeners:
            try:
                bind()
            except OSError as error:
                problems.append(f"the {label} port {number} at {address} is unavailable: {error}")
                free = False
            else:
                free = True
            observed["listeners"].append(
                {"name": label, "host": address, "port": number, "free": free}
            )
        if self.record["transfer"]["transport"] == "ucx_tcp":
            problems += self.ucx_ports(observed)
        if problems:
            raise CheckFailed("; ".join(problems))

    def ucx_ports(self, observed: dict) -> list[str]:
        """UCX over TCP listens on a free port of NARWHAL_UCX_TCP_PORT_RANGE at the fabric address.

        Each TP rank opens one listener on each transfer device and takes a port of the range
        that it can bind, so the range needs that many free ports, not every port. A port that
        a recently closed outgoing connection holds in TIME-WAIT cannot be bound until the
        kernel releases it, which is why other ports of the range may be busy.
        """
        value = self.inputs.values["UCX_TCP_PORT_RANGE"]
        source = self.inputs.env[f"NARWHAL_NODE_{self.inputs.node}_IP"]
        bounds = re.fullmatch(r"([0-9]{1,5})-([0-9]{1,5})", value)
        if bounds is None or not 1 <= int(bounds[1]) <= int(bounds[2]) <= 65535:
            return [f"NARWHAL_UCX_TCP_PORT_RANGE {value!r} is not a port range LOW-HIGH"]
        devices = [d for d in str(self.record["transfer"]["net_devices"]).split(",") if d.strip()]
        needed = int(self.record["tensor_parallel_size"]) * max(1, len(devices))
        busy = []
        ports = range(int(bounds[1]), int(bounds[2]) + 1)
        for port in ports:
            try:
                check_engine_bind(source, port)
            except OSError:
                busy.append(port)
        free = len(ports) - len(busy)
        observed["ucx_tcp_port_range"] = {
            "range": value,
            "host": source,
            "listeners": needed,
            "free": free,
            "unavailable": busy,
        }
        if free < needed:
            listed = ", ".join(map(str, busy[:10])) + (", ..." if len(busy) > 10 else "")
            return [
                f"NARWHAL_UCX_TCP_PORT_RANGE {value} at {source} has {free} free ports and the "
                f"engine opens {needed} UCX listeners; unavailable: {listed}"
            ]
        return []

    def devices(self, observed: dict) -> None:
        planned = [*self.record["accelerator_devices"], *self.record["transfer"]["devices"]]
        paths = list(dict.fromkeys([*planned, *self.gpu_devices]))
        problems = []
        if self.user is None:
            problems.append("the engine user is unknown, so no device permission was checked")
        observed["devices"] = []
        for path in paths:
            try:
                info = os.stat(path)
            except FileNotFoundError:
                problems.append(f"{path} is missing")
                observed["devices"].append({"path": path, "present": False})
                continue
            row: dict = {
                "path": path,
                "present": True,
                "character_device": stat.S_ISCHR(info.st_mode),
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
            if row["character_device"]:
                row.update(major=os.major(info.st_rdev), minor=os.minor(info.st_rdev))
            else:
                problems.append(f"{path} is not a character device")
            row["acl"] = has_acl(path)
            if row["acl"]:
                problems.append(
                    f"{path} carries an access control list, which replaces its group bits with "
                    "the list's mask; remove the list and grant the engine user access through "
                    "the owner, group or other bits"
                )
            elif self.user is not None:
                user = self.user
                row["read_write"] = can_read_write(info, user["uid"], user["gid"], user["groups"])
                if not row["read_write"]:
                    problems.append(
                        f"{path} (mode {row['mode']}, owner {info.st_uid}:{info.st_gid}) is not "
                        f"readable and writable by the engine user {user['uid']}:{user['gid']}"
                    )
            observed["devices"].append(row)
        if problems:
            raise CheckFailed("; ".join(problems))
