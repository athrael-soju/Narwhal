"""Run fixed deployment operations inside an authenticated remote job."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from narwhal.config import FleetConfig
from narwhal.contracts import COMMAND_RESULT, STATE, validate_document
from narwhal.engines.attestation import fetch_engine_identity, parse_process_start
from narwhal.runtime.state import validate as validate_handoff

from . import attestation_contract, launch_engine
from .management_records import encode_record


def write_result(path: Path, value: dict[str, Any]) -> None:
    """Commit one immutable gate result within the owned operation directory."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encode_record(value))
        stream.flush()
        os.fsync(stream.fileno())


async def _get(url: str, headers: dict[str, str], maximum: int = 4 * 1024 * 1024) -> bytes:
    async with (
        asyncio.timeout(15),
        httpx.AsyncClient(trust_env=False, follow_redirects=False, headers=headers) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        body = bytearray()
        async for block in response.aiter_bytes():
            if len(body) + len(block) > maximum:
                raise ValueError("Gate observation exceeds its byte limit")
            body.extend(block)
        return bytes(body)


async def _lifecycle(request: dict[str, Any]) -> dict[str, Any]:
    operation = request["operation"]
    engine = request["engine_id"]
    deadline = request["deadline_seconds"]
    payload: dict[str, Any] = {"engines": [engine]}
    if operation == "drain":
        payload["deadline_s"] = deadline
    async with (
        asyncio.timeout(deadline),
        httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=deadline) as client,
    ):
        response = await client.post(
            request["url"] + "/narwhal/lifecycle/" + operation, json=payload
        )
        response.raise_for_status()
        while True:
            response = await client.get(request["url"] + "/narwhal/lifecycle")
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("Lifecycle observation exceeds its byte limit")
            document = response.json()
            if not document["router"]["controls_fleet"]:
                raise ValueError("Router lost controller authority during maintenance")
            state = document["engines"][engine]
            if operation == "readmit":
                if state["state"] != "active" or not state["accepts_new"]:
                    raise ValueError("Engine readmission did not restore placement")
                return document
            if state["ready_to_stop"]:
                return document
            if state["state"] in {"blocked", "deadline_exceeded"}:
                raise ValueError("Engine drain did not finish")
            await asyncio.sleep(0.2)


def idle(fleet_path: Path) -> dict[str, Any]:
    """Require each engine's running and waiting request counters to be zero."""
    fleet = FleetConfig.load(fleet_path)
    rows = {}
    for engine in fleet.engines:
        body = asyncio.run(
            _get(engine.url.rstrip("/") + "/metrics", fleet.engine_headers())
        ).decode()
        values: dict[str, list[float]] = {"running": [], "waiting": []}
        for line in body.splitlines():
            match = re.fullmatch(
                r"vllm:num_requests_(running|waiting)(?:\{[^}]*\})?\s+([^\s]+)(?:\s+[0-9]+)?", line
            )
            if match:
                value = float(match[2])
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Engine occupancy metric is invalid")
                values[match[1]].append(value)
        if any(not samples or any(samples) for samples in values.values()):
            raise ValueError("Engine is busy or its idle counters are unavailable")
        rows[engine.iid] = {name: sum(samples) for name, samples in values.items()}
    return {"idle": True, "engines": rows}


def launch(request: dict[str, Any]) -> dict[str, Any]:
    """Use the checked launcher and capture the resulting live cache generation."""
    run = Path(request["run"])
    launch_engine.prepare(run, dict(os.environ))
    plan = launch_engine.load(run)
    launch_engine.check(run, plan)
    launch_engine.start(run, plan)
    container = (run / "container.id").read_text().strip()
    launch_engine.wait_ready(run, plan, container, request["ready_seconds"])
    launch_engine.capture_cache(run, plan)
    identity = asyncio.run(fetch_engine_identity(plan["endpoint"], headers=_headers()))
    result = {
        "role": plan["role"],
        "container_id": container,
        "generation": asdict(identity),
        "plan_sha256": launch_engine.digest(run / "launch.json"),
        "cache_sha256": launch_engine.digest(run / "cache-layout.json"),
        "image": plan["image"],
        "run": str(run),
    }
    write_result(run / "management-generation.json", result)
    return result


def _headers() -> dict[str, str]:
    key = os.environ.get("NARWHAL_ENGINE_API_KEY")
    return {"authorization": "Bearer " + key} if key else {}


async def _attestation_http(
    run: Path, plan: dict[str, Any], checked: dict[str, Any], container: str
) -> None:
    """Retain bounded HTTP evidence only for the generation captured at launch."""
    generation = attestation_contract.read_json(run / "management-generation.json")
    attestation_contract.require_binding(
        generation,
        "Launch generation",
        role=plan["role"],
        container_id=container,
        plan_sha256=launch_engine.digest(run / "launch.json"),
        cache_sha256=launch_engine.digest(run / "cache-layout.json"),
        image=plan["image"],
    )
    endpoint = plan["endpoint"].rstrip("/")
    version_body = await _get(endpoint + "/version", _headers())
    metrics_body = await _get(endpoint + "/metrics", _headers())
    version = json.loads(version_body)
    if not isinstance(version, dict) or version.get("version") != checked["vllm_api_version"]:
        raise ValueError("HTTP version differs from the checked runtime")
    metrics = metrics_body.decode()
    observed = {
        "vllm_version": version["version"],
        "process_start_time_seconds": parse_process_start(metrics),
    }
    if observed != generation.get("generation"):
        raise ValueError("HTTP engine generation changed since launch")
    launch_engine.write_private(run / "version.json", version_body.decode())
    launch_engine.write_private(run / "metrics.txt", metrics)


def attest(request: dict[str, Any]) -> dict[str, Any]:
    """Capture the running container's attestation inputs with existing validators."""
    run = Path(request["run"])
    plan, checked, plan_hash = attestation_contract.checked_plan(run)
    container = attestation_contract.live_container(run, checked)
    startup = run / "startup.log"
    output = launch_engine.docker(
        ["logs", container], run, "management-startup.log", include_stderr=True
    )
    launch_engine.write_private(startup, output)
    attestation_contract.capture_nixl(run)
    attestation_contract.capture_model_dimensions(run)
    launch_engine.registration_layout(run, plan, run / "cache-layout.json", True)
    capture = run / "image-check.log"
    resolved = set()
    for line in capture.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("connector"), str):
            resolved.add(row["connector"])
    if len(resolved) != 1:
        raise ValueError("Checked runtime did not resolve one connector class")
    connector = resolved.pop()
    mode = {"NixlPullConnector": "pull", "NixlPushConnector": "push"}.get(
        connector.rsplit(".", 1)[-1]
    )
    if mode is None:
        raise ValueError("Checked connector has no supported transfer mode")
    write_result(
        run / "transfer-mode.json",
        {
            "transfer_mode": mode,
            "configured_connector": plan["connector"]["kv_connector"],
            "kv_role": plan["connector"]["kv_role"],
            "resolved_connector": connector,
            "image_id": checked["image_id"],
            "plan_sha256": plan_hash,
            "source": str(capture),
            "source_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        },
    )
    launch_engine.handshake_policy(run, plan)
    asyncio.run(_attestation_http(run, plan, checked, container))
    destination = attestation_contract.generate(run, startup)
    return {
        "document": str(destination),
        "sha256": launch_engine.digest(destination),
        "container_id": container,
    }


def profile_arguments(request: dict[str, Any]) -> list[str]:
    """Translate the typed recipe into the existing profiler's supported options."""
    recipe = request["profiling"]
    arguments = ["--fleet", request["fleet"], "--limits", request["limits"], "--format", "json"]
    for name in ("prefill_lens", "decode_concurrency", "decode_input_lens"):
        arguments.extend(["--" + name.replace("_", "-"), ",".join(map(str, recipe[name]))])
    for name in ("decode_tokens", "decode_repeats", "prefill_repeats"):
        arguments.extend(["--" + name.replace("_", "-"), str(recipe[name])])
    for engine in request.get("engine_ids", []):
        arguments.extend(["--only", engine])
    if request.get("overwrite", False):
        arguments.append("--overwrite")
    return arguments


def command_result(
    request: dict[str, Any], callback: Callable[[list[str]], int], arguments: list[str]
) -> int:
    """Retain the installed CLI's structured result, including a failed gate."""
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
        with contextlib.redirect_stdout(output):
            code = callback(arguments)
        output.seek(0)
        content = output.read(8 * 1024 * 1024 + 1)
    if len(content.encode()) > 8 * 1024 * 1024:
        raise ValueError("Command result exceeds its byte limit")
    result = json.loads(content)
    validate_document(result, COMMAND_RESULT)
    if result["exit_code"] != code:
        raise ValueError("Command exit status differs from its structured result")
    write_result(Path(request["result_path"]).with_name("command-result.json"), result)
    return code


def router_idle(document: dict[str, Any], engine_ids: set[str]) -> None:
    """Require complete zero occupancy and standalone controller state."""
    validate_document(document, STATE)
    if document["ha"]["standby"] is not False or document["ha"]["epoch"] != 0:
        raise ValueError("Profile activation requires a standalone router")
    residents = document["resident"]
    if set(residents) != engine_ids:
        raise ValueError("Router occupancy does not cover the registered fleet")
    counters = [
        *(
            document["admission"][name]
            for name in ("inflight", "queued", "waiting_prefill", "waiting_decode")
        ),
        document["serving"]["http_retained"],
        *(row[name] for row in residents.values() for name in ("prefill", "decode")),
    ]
    if any(type(value) is not int or value != 0 for value in counters):
        raise ValueError("Router admission or resident work is not idle")


def verify_hold(document: dict[str, Any], engine_id: str) -> dict[str, Any]:
    """Require the selected engine's persisted individual drain hold."""
    validate_handoff(document)
    lifecycle = document["lifecycle"]
    records = [row for row in lifecycle["records"] if row["iid"] == engine_id]
    if (
        document["epoch"] != 0
        or lifecycle["engine_restart_policy"] != "individual"
        or lifecycle["wave_id"]
        or len(records) != 1
    ):
        raise ValueError("Router handoff has no matching individual drain")
    record = records[0]
    start = record["old_process_start"]
    if (
        record["state"] != "drained"
        or record["restart_required"] is not True
        or record["wave_id"]
        or type(start) not in (int, float)
        or not math.isfinite(start)
        or start <= 0
    ):
        raise ValueError("Router handoff does not retain the pre-restart process hold")
    return record


async def router_handoff(request: dict[str, Any]) -> dict[str, Any]:
    """Capture an idle router or verify that resume preserved its selected hold."""
    url = request["url"]
    engine_id = request["engine_id"]
    fleet = FleetConfig.load(Path(request["fleet"]))
    async with asyncio.timeout(request["deadline_seconds"]):
        before = json.loads(await _get(url + "/narwhal/state", {}))
        router_idle(before, {row.iid for row in fleet.engines})
        document = json.loads(await _get(url + "/narwhal/handoff", {}))
        held = verify_hold(document, engine_id)
        if request["operation"] == "router_resume_check":
            previous = json.loads(Path(request["preserved_handoff"]).read_bytes())
            original = verify_hold(previous, engine_id)
            for field in ("old_process_start", "wave_id", "restart_required", "requested_at"):
                if held[field] != original[field]:
                    raise ValueError("Router resume changed the persisted drain hold")
            lifecycle = json.loads(await _get(url + "/narwhal/lifecycle", {}))
            row = lifecycle["engines"][engine_id]
            if (
                not lifecycle["router"]["controls_fleet"]
                or row["accepts_new"]
                or not row["draining"]
                or row["state"] != "drained"
            ):
                raise ValueError("Resumed router did not preserve the placement hold")
        after = json.loads(await _get(url + "/narwhal/state", {}))
        router_idle(after, {row.iid for row in fleet.engines})
        if request["operation"] == "router_capture":
            write_result(Path(request["preserved_handoff"]), document)
            write_result(Path(request["resume_handoff"]), document)
        return {
            "handoff_sha256": hashlib.sha256(encode_record(document)).hexdigest(),
            "hold": held,
            "idle": True,
        }


def perform(request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one finite operation or one supervised foreground service."""
    operation = request["operation"]
    if operation == "launch":
        return launch(request)
    if operation == "attest":
        return attest(request)
    if operation == "attest_serve":
        code = attestation_contract.serve(Path(request["run"]))
        if code:
            raise ValueError("Attestation service exited unsuccessfully")
        return {"exit_code": code}
    if operation == "finalize":
        contract = attestation_contract.finalize_fleet(Path(request["fleet"]))
        return {"contract": contract.fields()}
    if operation == "idle":
        return idle(Path(request["fleet"]))
    if operation == "profile":
        from narwhal.profiling.management import combine_selected
        from narwhal.profiling.probe import main

        observed = idle(Path(request["fleet"]))
        code = command_result(request, main, profile_arguments(request))
        if code:
            raise ValueError("Generation profiling did not pass")
        path = Path(request["fleet"])
        fleet = json.loads(path.read_bytes())
        if request.get("previous_profiles"):
            output = Path(request["combined_profiles"])
            combine_selected(
                Path(request["previous_profiles"]),
                Path(fleet["profiles"]["path"]),
                output,
                {row["iid"] for row in fleet["engines"]},
            )
            fleet["profiles"]["path"] = str(output)
            temporary = path.with_suffix(".profiled.json")
            write_result(temporary, fleet)
            os.replace(temporary, path)
        profiles = Path(fleet["profiles"]["path"])
        return {
            "idle": observed,
            "exit_code": code,
            "profiles_path": str(profiles),
            "profiles_sha256": hashlib.sha256(profiles.read_bytes()).hexdigest(),
            "samples_sha256": hashlib.sha256(
                profiles.with_suffix(".samples.json").read_bytes()
            ).hexdigest(),
        }
    if operation in {"preflight", "postload"}:
        from narwhal.diagnostics.check import main

        observed = idle(Path(request["fleet"]))
        arguments = ["--fleet", request["fleet"], "--format", "json"]
        if operation == "postload":
            arguments.append("--ring")
        code = command_result(request, main, arguments)
        if code:
            raise ValueError("Live KV preflight did not pass")
        return {"idle": observed, "exit_code": code}
    if operation == "router_serve":
        from narwhal.cli import serve

        arguments = [
            "--fleet",
            request["fleet"],
            "--host",
            "127.0.0.1",
            "--port",
            str(request["port"]),
            "--journal",
            request["journal"],
        ]
        if request.get("resume"):
            arguments.append("--resume")
        code = serve(arguments)
        if code:
            raise ValueError("Router service exited unsuccessfully")
        return {"exit_code": code}
    if operation == "service_probe":
        end = time.monotonic() + request["ready_seconds"]
        while time.monotonic() < end:
            try:
                body = asyncio.run(_get(request["url"], {}))
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {502, 503, 504}:
                    raise
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            else:
                return {"response": json.loads(body)}
            time.sleep(min(0.2, max(0, end - time.monotonic())))
        raise ValueError("Service did not become ready within its recorded deadline")
    if operation in {"drain", "readmit"}:
        return asyncio.run(_lifecycle(request))
    if operation in {"router_capture", "router_resume_check"}:
        return asyncio.run(router_handoff(request))
    if operation == "workload_evidence":
        from .ssh_workload import collect_remote

        return collect_remote(request)
    if operation == "monitoring_start":
        from .ssh_monitoring import start_remote

        return start_remote(request)
    if operation == "fabric_observe":
        from .ssh_fabric import observe

        return observe(request)
    raise ValueError("Unsupported remote gate operation")


def main(argv: list[str] | None = None) -> int:
    """Accept only a supervisor-authenticated request with a private result path."""
    from .ssh_worker import inherited_owner

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    owner = inherited_owner()
    if owner is None:
        raise ValueError("Remote gate requires an authenticated job owner")
    request = json.loads(args.request.read_bytes())
    if request["owner"] != owner:
        raise ValueError("Gate request differs from its authenticated owner")
    os.umask(0o077)
    result = perform(request)
    write_result(
        Path(request["result_path"]),
        {"owner": owner, "operation": request["operation"], "result": result},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
