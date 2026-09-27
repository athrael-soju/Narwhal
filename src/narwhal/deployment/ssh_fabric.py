"""Measure every directed physical-host edge against its source cache budget."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import ssh_worker
from .management_records import OperationError, encode_record
from .ssh_prepare import asset, input_document

if TYPE_CHECKING:
    from .ssh_adapter import Session


def _listener(request: dict[str, Any]) -> dict[str, Any]:
    record = ssh_worker.status(Path(request["root"]), request["job_id"])
    expected = {**request["owner"], "launch_token": request["job_id"]}
    if record["owner"] != expected:
        raise ValueError("Fabric server ownership differs")
    if record["state"] not in {"queued", "running"}:
        raise ValueError("Fabric server exited before measurement")
    inodes = set()
    port = request["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid fabric server port")
    for name in ("tcp", "tcp6"):
        for line in Path("/proc/net/" + name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])
    for pid_text, ticks in record["observed_processes"].items():
        process = ssh_worker._proc(int(pid_text))
        if process is None or process[3] != ticks:
            continue
        try:
            for descriptor in Path(f"/proc/{pid_text}/fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except FileNotFoundError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    return {"listening": True}
        except FileNotFoundError:
            continue
    return {"listening": False}


def observe(request: dict[str, Any]) -> dict[str, Any]:
    """Inspect fixed route, utility and GID inputs inside an authenticated gate job."""
    if request.get("kind") == "listener":
        deadline = time.monotonic() + min(10, request["timeout_s"])
        while True:
            result = _listener(request)
            if result["listening"] or time.monotonic() >= deadline:
                return result
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    address = ipaddress.ip_address(request["address"])
    peer = ipaddress.ip_address(request["peer"])
    interface = request["interface"]
    if address.version != peer.version or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", interface):
        raise ValueError("Fabric address family or interface is invalid")
    deadline = time.monotonic() + min(30, request["timeout_s"])
    route = json.loads(
        ssh_worker._command(
            ["ip", "-json", f"-{address.version}", "route", "get", str(peer), "from", str(address)],
            deadline,
        )
    )
    if len(route) != 1 or route[0].get("dev") != interface:
        raise ValueError("Fabric route selects another interface")
    source = route[0].get("prefsrc", route[0].get("src", route[0].get("from")))
    if source is None or ipaddress.ip_address(source) != address:
        raise ValueError("Fabric route selects another source address")
    utility = "iperf3" if request["transport"] == "ucx_tcp" else "ib_write_bw"
    executable = ssh_worker.shutil.which(utility)
    if executable is None:
        raise ValueError("Required fabric measurement utility is unavailable")
    version = ssh_worker._command([executable, "--version"], deadline).decode().strip()
    if not version or len(version) > 8192:
        raise ValueError("Fabric utility version is unavailable")
    rails = []
    if request["transport"] == "ucx_rdma":
        gid = request["gid_index"]
        if type(gid) is not int or not 0 <= gid <= 255:
            raise ValueError("Invalid RDMA GID index")
        for device in request["net_devices"].split(","):
            matched = re.fullmatch(r"([A-Za-z0-9_.-]+):([1-9][0-9]*)", device)
            if not matched:
                raise ValueError("RDMA fabric requires explicit HCA ports")
            hca, port = matched[1], int(matched[2])
            path = Path("/sys/class/infiniband") / hca / "ports" / str(port)
            if not (path / "state").read_text().strip().endswith("ACTIVE"):
                raise ValueError("RDMA port is not active")
            mapped = (path / "gid_attrs/ndevs" / str(gid)).read_text().strip()
            if mapped != interface:
                raise ValueError("RDMA GID selects another network interface")
            gid_address = ipaddress.ip_address((path / "gids" / str(gid)).read_text().strip())
            if isinstance(gid_address, ipaddress.IPv6Address) and gid_address.ipv4_mapped:
                gid_address = gid_address.ipv4_mapped
            if gid_address != address:
                raise ValueError("RDMA GID selects another fabric address")
            rails.append(
                {
                    "hca": hca,
                    "port": port,
                    "gid": gid,
                    "type": (path / "gid_attrs/types" / str(gid)).read_text().strip(),
                }
            )
    elif request["transport"] != "ucx_tcp":
        raise ValueError("Unsupported fabric transport")
    return {"route": route, "version": version, "executable": executable, "rails": rails}


def _output(session: Session, effect: dict[str, Any]) -> bytes:
    content = bytearray()
    offset = 0
    while True:
        result = session.transport.read(
            effect["host_id"], effect["owner"]["launch_token"], "stdout", offset
        )
        chunk = base64.b64decode(result["data_base64"], validate=True)
        if result["offset"] != offset or len(content) + len(chunk) > ssh_worker.MAX_STDOUT:
            raise OperationError("source_truncated", "Fabric sample exceeds its bound")
        content.extend(chunk)
        offset += len(chunk)
        if result["next_offset"] is None:
            return bytes(content)
        if result["next_offset"] != offset or not chunk:
            raise OperationError("invalid_input", "Fabric sample paging is invalid")


def _stop(session: Session, effect: dict[str, Any]) -> None:
    context = session.context
    deadline = min(context.hard_deadline, time.monotonic() + 30)
    host, job = effect["host_id"], effect["owner"]["launch_token"]
    row = session.transport.cancel_owned(host, job, deadline=deadline)
    while row["state"] not in ssh_worker.TERMINAL:
        if time.monotonic() >= deadline:
            raise OperationError("recovery_required", "Fabric server cleanup deadline expired")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        row = session.transport.status_owned(host, job, deadline=deadline)
    if (
        row["state"] not in {"succeeded", "failed", "cancelled", "timed_out"}
        or row.get("observed_processes")
        or row.get("containers")
        or row.get("container_error")
        or row.get("cleanup", {}).get("error")
    ):
        raise OperationError("recovery_required", "Fabric server cleanup is incomplete")
    effect["effect"] = "absent"
    effect["identity"] = {"job_id": job, "receipt": row}
    context.record_effect(effect)


def _budget(session: Session, role: str, calculator: Any) -> dict[str, Any]:
    engine = session.state["engines"][role]
    relative = Path(engine["run"]).relative_to(Path(session.settings.remote_root))
    raw = session.files.read(
        engine["host_id"], str(relative / "cache-layout.json"), max_bytes=8 * 1024 * 1024
    )
    layout = json.loads(raw)
    launch = session.execution["launches"][role]
    expected = {
        "launch_config_sha256": hashlib.sha256(encode_record(launch)).hexdigest(),
        "model_config_sha256": session.role_environment(role)["NARWHAL_MODEL_CONFIG_SHA256"],
        "plan_sha256": engine["plan_sha256"],
    }
    if hashlib.sha256(raw).hexdigest() != engine["cache_sha256"] or any(
        layout.get(name) != value for name, value in expected.items()
    ):
        raise OperationError("stale_plan", "Serving cache differs from its recorded generation")
    payload = calculator.runtime_payload(
        layout, launch["tensor_parallel_size"], session.recipe.fabric.prompt_tokens
    )
    budget = calculator.workload_budget(
        payload,
        max(session.recipe.load.rates),
        1,
        session.recipe.fabric.transfer_budget_ms / 1000,
        1 / session.recipe.fabric.utilization,
    )
    return {
        **budget,
        **expected,
        "runtime_layout_sha256": hashlib.sha256(raw).hexdigest(),
        "prompt_tokens": session.recipe.fabric.prompt_tokens,
        "tensor_parallel_size": launch["tensor_parallel_size"],
        "image": layout["image"],
        "layout": layout["sizing"],
    }


def _address(session: Session, role: str) -> str:
    return str(
        ipaddress.ip_address(
            session.role_environment(role)[f"NARWHAL_NODE_{role.removeprefix('engine-')}_IP"]
        )
    )


def _observe(session: Session, role: str, peer: str, name: str) -> dict[str, Any]:
    _, result = session.gate(
        session.host_for(role),
        name,
        {
            "operation": "fabric_observe",
            "address": _address(session, role),
            "peer": _address(session, peer),
            "interface": session.role_environment(role)["NARWHAL_FABRIC_INTERFACE"],
            "transport": session.recipe.fabric.transport,
            "net_devices": session.execution["launches"][role]["transfer"]["net_devices"],
            "gid_index": session.recipe.fabric.gid_index,
            "timeout_s": max(1, int(session.context.deadline - time.monotonic())),
        },
        role=role,
    )
    if result is None:
        raise OperationError("source_unavailable", "Fabric route observation is absent")
    return result


def _commands(
    session: Session,
    source: str,
    destination: str,
    source_info: dict[str, Any],
    destination_info: dict[str, Any],
    rail: int,
) -> tuple[list[str], list[str], dict[str, Any]]:
    recipe = session.recipe.fabric
    port = str(recipe.test_port)
    duration = str(recipe.duration_s)
    if recipe.transport == "ucx_tcp":
        streams = session.execution["launches"][source]["tensor_parallel_size"]
        return (
            [
                destination_info["executable"],
                "--server",
                "--bind",
                _address(session, destination),
                "--port",
                port,
            ],
            [
                source_info["executable"],
                "--client",
                _address(session, destination),
                "--bind",
                _address(session, source),
                "--port",
                port,
                "--parallel",
                str(streams),
                "--omit",
                "3",
                "--time",
                duration,
                "--json",
            ],
            {
                "parallel": streams,
                "omit_s": 3,
                "duration_s": recipe.duration_s,
                "port": recipe.test_port,
            },
        )
    source_rail, dest_rail = source_info["rails"][rail], destination_info["rails"][rail]

    def command(info: dict[str, Any], selected: dict[str, Any], role: str) -> list[str]:
        arguments = [
            info["executable"],
            "-d",
            selected["hca"],
            "-i",
            str(selected["port"]),
            "-x",
            str(selected["gid"]),
            "-p",
            port,
            "-s",
            "1048576",
            "-D",
            duration,
            "--report_gbits",
            "--bind_source_ip",
            _address(session, role),
        ]
        if ":" in _address(session, role):
            arguments.extend(["--ipv6-addr", "--ipv6"])
        return arguments

    return (
        command(destination_info, dest_rail, destination),
        [*command(source_info, source_rail, source), _address(session, destination)],
        {
            "source_hca": source_rail["hca"],
            "source_port": source_rail["port"],
            "source_gid": source_rail["gid"],
            "destination_hca": dest_rail["hca"],
            "destination_port": dest_rail["port"],
            "destination_gid": dest_rail["gid"],
            "message_bytes": 1048576,
            "duration_s": recipe.duration_s,
            "port": recipe.test_port,
        },
    )


def _rdma_gbps(raw: bytes) -> float:
    lines = raw.decode().splitlines()
    if not any("BW average[Gb/sec]" in line for line in lines):
        raise OperationError("invalid_input", "RDMA sample has no average Gbit/s column")
    values = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and fields[0] == "1048576" and fields[1].isdigit():
            values.append(float(fields[3]))
    if len(values) != 1 or not math.isfinite(values[0]) or values[0] <= 0:
        raise OperationError("invalid_input", "RDMA sample has no unique positive average")
    return values[0]


def qualify(session: Session) -> dict[str, Any]:
    """Run serial directed measurements and remove each temporary owned server."""
    calculator = asset(session.settings, "tools/deployment/fabric_budget.py")
    roles = sorted(session.state["engines"])
    if session.recipe.fabric.prompt_tokens < session.recipe.load.input_tokens:
        raise OperationError("prerequisite_failed", "Fabric budget does not cover the load prompt")
    if any(
        session.execution["launches"][role]["transfer"]["transport"]
        != session.recipe.fabric.transport
        for role in roles
    ):
        raise OperationError("prerequisite_failed", "Fabric recipe differs from engine transport")
    budgets = {role: _budget(session, role, calculator) for role in roles}
    physical = input_document(session.context, session.plan, "host_inventory")
    representatives: dict[str, str] = {}
    for role in roles:
        host = session.host_for(role)
        machine = physical[host]["host_id"]
        previous = representatives.get(machine)
        if previous is None or budgets[role]["required_gbps"] > budgets[previous]["required_gbps"]:
            representatives[machine] = role
        session.put(host, f"fabric/{role}/budget.json", encode_record(budgets[role]))
    results = []
    for source_machine, source in sorted(representatives.items()):
        source_host = session.host_for(source)
        for destination_machine, destination in sorted(representatives.items()):
            if source_machine == destination_machine:
                continue
            dest_host = session.host_for(destination)
            session.check_generations()
            edge = source + "-to-" + destination
            session.gate(
                session.router_host,
                edge + "-idle",
                {"operation": "idle", "fleet": session.state["router"]["fleet_path"]},
            )
            source_info = _observe(session, source, destination, edge + "-source")
            destination_info = _observe(session, destination, source, edge + "-destination")
            rails = (
                len(source_info["rails"]) if session.recipe.fabric.transport == "ucx_rdma" else 1
            )
            if not rails or (
                session.recipe.fabric.transport == "ucx_rdma"
                and rails != len(destination_info["rails"])
            ):
                raise OperationError("prerequisite_failed", "Directed RDMA rails cannot be paired")
            for rail in range(rails):
                for repeat in range(session.recipe.fabric.repeats):
                    name = f"{edge}-r{rail}-n{repeat}"
                    session.gate(
                        session.router_host,
                        name + "-idle",
                        {"operation": "idle", "fleet": session.state["router"]["fleet_path"]},
                    )
                    server, client, parameters = _commands(
                        session, source, destination, source_info, destination_info, rail
                    )
                    effect = session.command(
                        dest_host,
                        name + "-server",
                        server,
                        cwd=session.checkout(dest_host),
                        env=session.role_environment(destination),
                        background=True,
                    )
                    try:
                        _, ready = session.gate(
                            dest_host,
                            name + "-ready",
                            {
                                "operation": "fabric_observe",
                                "kind": "listener",
                                "root": session.settings.remote_root,
                                "job_id": effect["owner"]["launch_token"],
                                "port": session.recipe.fabric.test_port,
                                "timeout_s": max(
                                    1, int(session.context.deadline - time.monotonic())
                                ),
                            },
                            role=destination,
                        )
                        if not ready or not ready["listening"]:
                            raise OperationError(
                                "prerequisite_failed", "Owned fabric server did not listen"
                            )
                        measured = session.command(
                            source_host,
                            name + "-client",
                            client,
                            cwd=session.checkout(source_host),
                            env=session.role_environment(source),
                        )
                        raw = _output(session, measured)
                    finally:
                        _stop(session, effect)
                    suffix = "json" if session.recipe.fabric.transport == "ucx_tcp" else "txt"
                    path = session.put(source_host, f"fabric/{name}.{suffix}", raw)
                    link = {
                        "source_role": source,
                        "destination_role": destination,
                        "source_address": _address(session, source),
                        "destination_address": _address(session, destination),
                        "source_interface": session.role_environment(source)[
                            "NARWHAL_FABRIC_INTERFACE"
                        ],
                        "destination_interface": session.role_environment(destination)[
                            "NARWHAL_FABRIC_INTERFACE"
                        ],
                        "source_route": json.dumps(source_info["route"], sort_keys=True),
                        "destination_route": json.dumps(destination_info["route"], sort_keys=True),
                        "transport": session.recipe.fabric.transport,
                        "tool_version": json.dumps(
                            {
                                "source": source_info["version"],
                                "destination": destination_info["version"],
                            },
                            sort_keys=True,
                        ),
                        "test_parameters": parameters,
                    }
                    link_path = session.put(
                        source_host, f"fabric/{name}.link.json", encode_record(link)
                    )
                    gbps = (
                        calculator.received_gbps(json.loads(raw))
                        if suffix == "json"
                        else _rdma_gbps(raw)
                    )
                    evidence = {
                        "schema": "narwhal.fabric-edge-evidence",
                        "schema_version": 1,
                        "link_sha256": calculator.link_fingerprint(link),
                        "sample_sha256": hashlib.sha256(raw).hexdigest(),
                        "budget_sha256": hashlib.sha256(encode_record(budgets[source])).hexdigest(),
                        "measured_gbps": gbps,
                        "required_gbps": budgets[source]["required_gbps"],
                        "passed": gbps >= budgets[source]["required_gbps"],
                    }
                    session.put(
                        source_host, f"fabric/{name}.evidence.json", encode_record(evidence)
                    )
                    results.append(
                        {
                            "source": source,
                            "destination": destination,
                            "rail": rail,
                            "repeat": repeat,
                            "sample": str(path),
                            "link": str(link_path),
                            **evidence,
                        }
                    )
                    if not evidence["passed"]:
                        raise OperationError(
                            "fabric_budget_unmet",
                            "Directed fabric measurement is below its source budget",
                        )
    return {
        "budgets": budgets,
        "edges": results,
        "directed_host_pairs": len(representatives) * (len(representatives) - 1),
    }
