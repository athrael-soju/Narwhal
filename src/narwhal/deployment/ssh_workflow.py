"""Order installed Narwhal commands and retain each qualified generation."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .management_records import OperationError, encode_record, utc_now
from .ssh_adapter import remote_absent
from .ssh_prepare import input_document

if TYPE_CHECKING:
    from .ssh_adapter import Session


def _engine_roles(session: Session) -> list[str]:
    if session.plan["payload"]["action"] == "engine_replace":
        return ["engine-" + session.plan["payload"]["parameters"]["engine_id"].removeprefix("n")]
    return sorted(session.execution["launches"], key=lambda value: int(value.split("-")[1]))


def _fleet_files(session: Session) -> None:
    """Write the initial runtime configuration under the owned deployment root."""
    fleet = copy.deepcopy(session.fleet)
    for engine in fleet["engines"]:
        for field in ("url", "attestation_url"):
            value = engine[field]
            if value.startswith("${"):
                engine[field] = session.environment[value[2:-1]]
    fleet["profiles"]["path"] = str(session.root / "profiles.json")
    path = session.put(session.router_host, "fleet.json", encode_record(fleet))
    limits = {}
    for role, launch in session.execution["launches"].items():
        arguments = launch["runtime"]["extra_args"]
        selected = []
        for index, value in enumerate(arguments):
            if value == "--max-num-seqs" and index + 1 < len(arguments):
                selected.append(arguments[index + 1])
            elif value.startswith("--max-num-seqs="):
                selected.append(value.partition("=")[2])
        if len(selected) != 1 or not str(selected[0]).isdigit() or int(selected[0]) < 1:
            raise OperationError(
                "prerequisite_failed", "Each launch requires one positive max-num-seqs value"
            )
        limits["n" + role.removeprefix("engine-")] = int(selected[0])
    limits_path = session.put(
        session.router_host,
        "profiling-limits.json",
        encode_record(
            {
                "schema": "narwhal.profiling-limits",
                "schema_version": 1,
                "engines": limits,
            }
        ),
    )
    origin = session.role_environment("router").get("NARWHAL_ROUTER_URL", "http://127.0.0.1:8000")
    endpoint = urlsplit(origin)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
    ):
        raise OperationError("prerequisite_failed", "SSH v1 router requires a loopback HTTP origin")
    session.state["router"] = {
        "host_id": session.router_host,
        "url": origin,
        "port": endpoint.port or 80,
        "fleet_path": str(path),
        "profiles_path": fleet["profiles"]["path"],
        "limits_path": str(limits_path),
        "journal_path": str(session.root / "journal.jsonl"),
    }
    session.save()


def _launch(session: Session) -> dict[str, Any]:
    images = input_document(session.context, session.plan, "runtime_identity")["images"]
    results = {}
    for role in _engine_roles(session):
        host = session.host_for(role)
        checkout = session.checkout(host)
        launch = session.execution["launches"][role]
        selected = session.put(host, f"launches/{role}.json", encode_record(launch))
        environment = session.execution["role_environment"][role]
        hook = checkout / "src/narwhal/deployment/cache_capture_hook.py"
        environment.update(
            {
                "NARWHAL_ENGINE_LAUNCH_CONFIG": str(selected),
                "NARWHAL_ENGINE_IMAGE": images[role]["id"],
                "NARWHAL_ENGINE_LAUNCHER": str(
                    checkout / "src/narwhal/deployment/launch_engine.py"
                ),
                "NARWHAL_CACHE_CAPTURE_HOOK": str(hook),
                "NARWHAL_CACHE_CAPTURE_HOOK_SHA256": hashlib.sha256(
                    Path(__file__).with_name("cache_capture_hook.py").read_bytes()
                ).hexdigest(),
                "NARWHAL_FABRIC_BUDGET_TOOL": str(checkout / "tools/deployment/fabric_budget.py"),
            }
        )
        run = str(session.root / "engines" / role)
        effect, result = session.gate(
            host,
            role,
            {
                "operation": "launch",
                "run": run,
                "ready_seconds": max(1, int(session.context.deadline - time.monotonic()) - 5),
            },
            role=role,
            kind="engine",
            containers=True,
        )
        if result is None:
            raise OperationError(
                "source_unavailable", "Engine launch returned no generation evidence"
            )
        session.state["engines"][role] = {
            **result,
            "host_id": host,
            "owner": effect["owner"],
            "effect": effect,
        }
        session.retain(effect)
        results[role] = result
    return {"engines": results}


def _attest(session: Session) -> dict[str, Any]:
    results = {}
    for role in _engine_roles(session):
        engine = session.state["engines"][role]
        host = engine["host_id"]
        _, result = session.gate(
            host, role + "-capture", {"operation": "attest", "run": engine["run"]}, role=role
        )
        effect, _ = session.gate(
            host,
            role + "-sidecar",
            {"operation": "attest_serve", "run": engine["run"]},
            role=role,
            kind="attestation",
            background=True,
        )
        url = session.role_environment(role)[
            f"NARWHAL_NODE_{role.removeprefix('engine-')}_ATTESTATION_URL"
        ]
        _, observed = session.gate(
            session.router_host,
            role + "-attestation-check",
            {
                "operation": "service_probe",
                "url": url,
                "ready_seconds": max(1, int(session.context.deadline - time.monotonic()) - 2),
            },
        )
        session.retain(effect)
        engine["attestation_effect"] = effect
        results[role] = {"capture": result, "observation": observed}
    _, final = session.gate(
        session.router_host,
        "finalize",
        {"operation": "finalize", "fleet": session.state["router"]["fleet_path"]},
    )
    return {"engines": results, "fleet": final}


def _profile(session: Session) -> dict[str, Any]:
    router = session.state["router"]
    remote_relative = (
        Path(router["fleet_path"]).relative_to(session.settings.remote_root).as_posix()
    )
    fleet = json.loads(
        session.files.read(session.router_host, remote_relative, max_bytes=1024 * 1024)
    )
    previous_profiles = fleet["profiles"]["path"]
    fleet["profiles"]["path"] = str(session.root / "profile" / "selected.json")
    candidate = session.put(session.router_host, "profile/fleet.json", encode_record(fleet))
    request = {
        "operation": "profile",
        "fleet": str(candidate),
        "limits": router["limits_path"],
        "profiling": session.recipe.profiling.model_dump(mode="json"),
        "engine_ids": session.plan["payload"]["parameters"].get("engine_ids", []),
        "overwrite": False,
    }
    if session.plan["payload"]["action"] == "engine_replace":
        request["engine_ids"] = [session.plan["payload"]["parameters"]["engine_id"]]
    if request["engine_ids"]:
        request["previous_profiles"] = previous_profiles
        request["combined_profiles"] = str(session.root / "profile" / "complete.json")
    _, result = session.gate(session.router_host, "profile", request)
    if result is None:
        raise OperationError("source_unavailable", "Profiling returned no retained output")
    session.state["measured_profiles"] = result
    if session.plan["payload"]["action"] in {"fleet_deploy", "engine_replace"}:
        router["fleet_path"] = str(candidate)
        router["profiles_path"] = result["profiles_path"]
    return {"profiling": result}


def _serve(session: Session, *, resume: bool = False) -> dict[str, Any]:
    router = session.state["router"]
    effect, _ = session.gate(
        session.router_host,
        "router",
        {
            "operation": "router_serve",
            "fleet": router["fleet_path"],
            "port": router["port"],
            "journal": router["journal_path"],
            "resume": resume,
        },
        kind="router",
        background=True,
    )
    _, ready = session.gate(
        session.router_host,
        "router-ready",
        {
            "operation": "service_probe",
            "url": router["url"] + ("/narwhal/lifecycle" if resume else "/ready"),
            "ready_seconds": max(1, int(session.context.deadline - time.monotonic()) - 2),
        },
    )
    if resume:
        _, ready = session.gate(
            session.router_host,
            "router-resume-check",
            {
                "operation": "router_resume_check",
                "url": router["url"],
                "fleet": router["fleet_path"],
                "engine_id": session.plan["payload"]["parameters"]["engine_id"],
                "preserved_handoff": router["preserved_handoff"],
                "deadline_seconds": max(1, int(session.context.deadline - time.monotonic()) - 2),
            },
        )
    session.retain(effect)
    router["effect"] = effect
    return {"ready": ready}


def _activate_profiles(session: Session) -> dict[str, Any]:
    router = session.state["router"]
    preserved = session.root / "profile/handoff-preserved.json"
    resume = session.root / "profile/handoff-resume.json"
    _, captured = session.gate(
        session.router_host,
        "router-capture",
        {
            "operation": "router_capture",
            "url": router["url"],
            "fleet": router["fleet_path"],
            "engine_id": session.plan["payload"]["parameters"]["engine_id"],
            "preserved_handoff": str(preserved),
            "resume_handoff": str(resume),
            "deadline_seconds": max(1, int(session.context.deadline - time.monotonic()) - 2),
        },
    )
    relative = Path(router["fleet_path"]).relative_to(session.settings.remote_root).as_posix()
    fleet = json.loads(session.files.read(session.router_host, relative, max_bytes=1024 * 1024))
    fleet.setdefault("recovery", {})["state_path"] = str(resume)
    activated = session.put(
        session.router_host, "profile/activated-fleet.json", encode_record(fleet)
    )
    removed = _stop_effect(session, router["effect"])
    session.state["services"] = [
        row for row in session.state["services"] if row["resource_id"] != removed["resource_id"]
    ]
    router.update(fleet_path=str(activated), preserved_handoff=str(preserved))
    session.save()
    return {"capture": captured, "removed": removed, "resumed": _serve(session, resume=True)}


def _stop_effect(session: Session, effect: dict[str, Any]) -> dict[str, Any]:
    host, job_id = effect["host_id"], effect["owner"]["launch_token"]
    status = session.transport.status(host, job_id)
    if status["owner"] != effect["owner"]:
        raise OperationError(
            "ownership_conflict", "Cleanup receipt no longer identifies the recorded owner"
        )
    session.transport.cancel(host, job_id)
    while True:
        session.context.assert_current()
        status = session.transport.status(host, job_id)
        if status.get("owner") != effect["owner"]:
            raise OperationError("ownership_conflict", "Cleanup receipt changed owner")
        if status["state"] in {
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "recovery_required",
        }:
            break
        time.sleep(0.2)
    if not remote_absent(status):
        raise OperationError(
            "recovery_required", "Owned cleanup left residual or unobservable resources"
        )
    return {"resource_id": effect["resource_id"], "effect": "absent", "receipt": status}


def cleanup_effects(operation: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the latest receipt for every selected remote resource and owner."""
    effects = {
        (effect["resource_id"], encode_record(effect["owner"])): effect
        for selected_operation in [*reversed(operation.get("predecessors", [])), operation]
        for stage in selected_operation["stages"]
        for effect in stage["effects"]
        if effect["host_id"] != "management"
    }
    return [
        effect
        for effect in effects.values()
        if effect["effect"] != "absent" or not isinstance(effect.get("identity"), dict)
    ]


def _cleanup(session: Session) -> dict[str, Any]:
    operation = input_document(session.context, session.plan, "cleanup_selection")
    pending = cleanup_effects(operation)
    for effect in pending:
        session.context.record_intent({**effect, "effect": "unknown", "observed_at": utc_now()})
    removed, errors = [], []
    for effect in reversed(pending):
        try:
            stopped = _stop_effect(session, effect)
            session.context.record_effect(
                {
                    **effect,
                    "effect": "absent",
                    "observed_at": utc_now(),
                    "identity": {
                        "job_id": effect["owner"]["launch_token"],
                        "receipt": stopped["receipt"],
                    },
                }
            )
            removed.append(stopped)
        except OperationError as error:
            errors.append(
                {"resource_id": effect["resource_id"], "code": error.code, "message": error.message}
            )
    selected = {row["resource_id"] for row in removed}
    session.state["services"] = [
        row for row in session.state.get("services", []) if row["resource_id"] not in selected
    ]
    if not session.state["services"]:
        session.state["status"] = "removed"
    result = {
        "removed": removed,
        "residual": errors,
        "retained": {
            "evidence": "Private source, gate and failure evidence remain on the registered hosts",
            "monitoring_volumes": session.state.get("monitoring", {}).get("volumes", []),
        },
    }
    session.export("cleanup_result", result)
    session.save()
    if errors:
        raise OperationError(
            "recovery_required", "Cleanup requires inspection of residual remote resources"
        )
    return result


def execute(session: Session, operation: str) -> dict[str, Any]:
    """Dispatch the operation declared in the immutable stage list."""
    if operation == "fleet.discover":
        return {"hosts": input_document(session.context, session.plan, "host_inventory")}
    if operation == "fleet.install":
        installation = session.install()
        _fleet_files(session)
        return installation
    if operation == "fleet.launch":
        return _launch(session)
    if operation == "fleet.fabric":
        from .ssh_fabric import qualify

        return qualify(session)
    if operation == "fleet.attest":
        return _attest(session)
    if operation == "fleet.profile":
        return _profile(session)
    if operation in {"fleet.preflight", "fleet.postload"}:
        _, result = session.gate(
            session.router_host,
            operation.replace(".", "-"),
            {
                "operation": "postload" if operation == "fleet.postload" else "preflight",
                "fleet": session.state["router"]["fleet_path"],
            },
        )
        return {"validation": result}
    if operation == "fleet.serve":
        return _serve(session)
    if operation == "fleet.activate_profiles":
        return _activate_profiles(session)
    if operation == "fleet.monitor":
        from .ssh_monitoring import start

        return start(session)
    if operation == "fleet.workload":
        from .ssh_workload import run

        return run(session)
    if operation == "fleet.accept":
        from .ssh_workload import accept

        result = accept(session)
        session.state["status"] = "qualified"
        return result
    if operation == "fleet.cleanup":
        return _cleanup(session)
    if operation in {"fleet.drain", "fleet.readmit"}:
        _, result = session.gate(
            session.router_host,
            operation.replace(".", "-"),
            {
                "operation": operation.removeprefix("fleet."),
                "url": session.state["router"]["url"],
                "engine_id": session.plan["payload"]["parameters"]["engine_id"],
                "deadline_seconds": max(1, int(session.context.deadline - time.monotonic()) - 2),
            },
        )
        return {"lifecycle": result}
    if operation == "fleet.stop_engine":
        role = _engine_roles(session)[0]
        engine = session.state["engines"][role]
        removed = [_stop_effect(session, engine[name]) for name in ("attestation_effect", "effect")]
        old = {row["resource_id"] for row in removed}
        session.state["services"] = [
            row for row in session.state["services"] if row["resource_id"] not in old
        ]
        session.state["engines"].pop(role)
        session.state["invalidated"] = [
            "cache",
            "fabric",
            "attestation",
            "profiles",
            "preflight",
            "workload",
        ]
        return {"removed": removed, "invalidated": session.state["invalidated"]}
    raise OperationError("invalid_input", "SSH stage operation is not implemented")
