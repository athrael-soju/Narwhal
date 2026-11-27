from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import httpx

from ...deployment.attestation_contract.capture import Capture, CaptureContext
from ...deployment.attestation_contract.document import (
    STARTUP_LOG,
    AttestationContext,
    AttestationEvidence,
)
from ...deployment.attestation_contract.evidence import (
    LiveEngine,
    checked_plan,
    engine_image,
    live_binding,
    live_identity,
    parse_tagged_capture,
    read_json,
    require_binding,
    require_prior_dimensions,
    require_process_start,
    write_private_json,
)
from ...deployment.attestation_contract.pod_engine import PodEngine, recorded_inspection
from ...deployment.launch_engine.kubernetes import POD_RECORDS
from ...deployment.launch_engine.plan import launch_headers, read_env
from ...deployment.launch_engine.runtime import digest, write_private
from .captures import cache_layout_capture, handshake_policy, layout_record, registration_layout
from .identity import parse_process_start
from .inspections import (
    MODEL_DIMENSIONS_CAPTURE,
    MODEL_DIMENSIONS_CAPTURE_TAG,
    NIXL_CAPTURE_TAG,
    inspection,
)

__all__ = ["MODEL_DIMENSIONS_CAPTURE", "MODEL_DIMENSIONS_CAPTURE_TAG", "NIXL_CAPTURE_TAG"]


def parse_nixl_capture(output: str) -> dict:
    return parse_tagged_capture(output, NIXL_CAPTURE_TAG, "NIXL")


def live_inspection(
    run: Path, plan: dict, identity: LiveEngine, name: str, log: Path
) -> subprocess.CompletedProcess:
    """Run an inspection in the live engine, or read the one its pod's engine container ran.

    A container runs it through `docker exec`, and a native engine with the launch's Python
    and engine environment. Each keeps the cache capture hook off.
    """
    plan_hash = digest(run / "launch.json")
    if isinstance(identity, PodEngine):
        argv = inspection(plan, plan_hash, name)
        stdout = recorded_inspection(identity, name, argv, log)
        return subprocess.CompletedProcess(argv, 0, stdout, "")
    if isinstance(identity, dict):
        argv = inspection(plan, plan_hash, name)
        if name == "model-dimensions":
            argv = [*argv[:3], str(run / "hook/launch.json"), plan["model_config_path"]]
        command = [plan["python_executable"], *argv]
        env = {**os.environ, **read_env(run / "engine.env"), "NARWHAL_CAPTURE_CACHE": "0"}
    else:
        argv = inspection(plan, plan_hash, name)
        command = ["docker", "exec", "--env", "NARWHAL_CAPTURE_CACHE=0", identity, "python3", *argv]
        env = None
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    write_private(log, result.stdout + "\nSTDERR\n" + result.stderr)
    return result


def capture_model_dimensions(run: Path, out: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    identity = live_identity(run, plan, checked)
    destination = out / "model-dimensions.live.json"
    if destination.exists():
        raise ValueError("Live model dimensions already exist; retain the capture")
    log = out / "model-dimensions.live.log"
    result = live_inspection(run, plan, identity, "model-dimensions", log)
    if result.returncode:
        raise ValueError(f"Live model dimension inspection failed; inspect {log}")
    try:
        record = parse_tagged_capture(
            result.stdout, MODEL_DIMENSIONS_CAPTURE_TAG, "model dimensions"
        )
    except ValueError as exc:
        raise ValueError(f"{exc}; inspect {log}") from exc
    require_binding(
        record,
        "Live model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        revision=plan["revision"],
        model_config_sha256=plan["model_config_sha256"],
        launcher_sha256=plan["launcher_sha256"],
    )
    if (
        not isinstance(record.get("model_architecture"), str)
        or not record["model_architecture"].strip()
    ):
        raise ValueError("Live model dimensions lack the resolved architecture")
    contract = record.get("contract")
    if not isinstance(contract, dict) or any(
        type(contract.get(field)) is not int or contract[field] < 1
        for field in ("head_size", "kv_heads", "hidden_layers")
    ):
        raise ValueError("Live model dimensions contain invalid compatibility getters")
    require_prior_dimensions(run, plan, plan_hash, contract)
    record.update(live_binding(plan, checked, identity, model_revision=True))
    record["capture_log_sha256"] = digest(log)
    write_private_json(destination, record)
    return destination


def capture_nixl(run: Path, out: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    identity = live_identity(run, plan, checked)
    log = out / "nixl-connector-version.log"
    result = live_inspection(run, plan, identity, "connector-version", log)
    if result.returncode:
        raise ValueError(f"NIXL connector inspection exited {result.returncode}; inspect {log}")
    record = parse_nixl_capture(result.stdout)
    if (
        type(record.get("nixl_connector_version")) is not int
        or record["nixl_connector_version"] < 1
    ):
        raise ValueError("Pinned connector returned an invalid protocol version")
    record["plan_sha256"] = plan_hash
    record.update(live_binding(plan, checked, identity))
    destination = out / "nixl-connector-version.json"
    write_private_json(destination, record)
    return destination


# The transfer protocol of each connector class the pinned connector factory resolves. In the
# pinned API, NixlConnector is an alias of NixlPullConnector, and the factory reports a class by
# its own name, so a class named NixlConnector is another implementation.
TRANSFER_MODES = {"NixlPullConnector": "pull", "NixlPushConnector": "push"}


def resolved_connectors(log: str, plan_hash: str) -> set[str]:
    """Connector classes the runtime check resolved, in check attempts of the plan `plan_hash`."""
    resolved = set()
    attempt = None
    for line in log.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if "check_attempt" in item:
            attempt = item.get("plan_sha256")
        elif attempt == plan_hash and isinstance(item.get("connector"), str):
            resolved.add(item["connector"])
    return resolved


def pod_check_connectors(run: Path, checked: dict, plan_hash: str) -> tuple[set[str], dict]:
    """Connector classes the checked pod's runtime check resolved, read from the records of
    this plan that `narwhal-engine check` kept for the pod, and the SHA-256 of each record."""
    resolved = set()
    sources = {}
    for path in sorted((run / POD_RECORDS / checked["pod_uid"]).glob("runtime-check-*.json")):
        data = path.read_bytes()
        record = json.loads(data)
        if record.get("plan_sha256") != plan_hash or record.get("returncode") != 0:
            continue
        sources[str(path.resolve())] = hashlib.sha256(data).hexdigest()
        for line in str(record.get("stdout", "")).splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("connector"), str):
                resolved.add(item["connector"])
    return resolved, sources


def capture_transfer_mode(run: Path, out: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    native = plan.get("backend") == "native"
    identity = live_identity(run, plan, checked)
    destination = out / "transfer-mode.json"
    if isinstance(identity, PodEngine):
        label = f"{POD_RECORDS}/{checked['pod_uid']}/"
        resolved, provenance = pod_check_connectors(run, checked, plan_hash)
    else:
        log = run / ("runtime-check.log" if native else "image-check.log")
        data = log.read_bytes()
        label = log.name
        resolved = resolved_connectors(data.decode(errors="replace"), plan_hash)
        provenance = {
            "source": str(log.resolve()),
            "source_sha256": hashlib.sha256(data).hexdigest(),
        }
    if len(resolved) != 1:
        raise ValueError(
            f"{label} holds {len(resolved)} factory-resolved connector classes for this plan; "
            "rerun the runtime check for this launch plan"
        )
    connector = resolved.pop()
    mode = TRANSFER_MODES.get(connector.rsplit(".", 1)[-1])
    if mode is None:
        raise ValueError(f"Inspect the pinned connector implementation of {connector}")
    record = {
        "transfer_mode": mode,
        "configured_connector": plan["connector"]["kv_connector"],
        "kv_role": plan["connector"]["kv_role"],
        "resolved_connector": connector,
        "plan_sha256": plan_hash,
        **(provenance if not isinstance(identity, PodEngine) else {"sources": provenance}),
        **transfer_binding(checked, identity),
    }
    write_private_json(destination, record)
    return destination


# The connector factory logs each connector it creates, once per worker and scheduler:
#   [factory.py:62] Creating v1 connector with name: NixlPullConnector and engine_id: <id>
# Other connector lines, such as "NixlConnector setting KV cache layout to ...", name the
# configured alias, not the resolved class.
CREATED_CONNECTOR = re.compile(r"\bCreating v1 connector with name: (\w+) and engine_id: ")


def check_startup_connector(attempt: Path) -> None:
    """Require the serving process to have created the connector class that the attempt's
    transfer-mode.json names."""
    startup_log = attempt / STARTUP_LOG
    expected = read_json(attempt / "transfer-mode.json")["resolved_connector"].rsplit(".", 1)[-1]
    created = set(CREATED_CONNECTOR.findall(startup_log.read_text(errors="replace")))
    if not created:
        raise ValueError(
            f"{startup_log.name} records no connector the engine created; "
            "retain the complete engine log"
        )
    if created != {expected}:
        raise ValueError(
            f"{startup_log.name} records the engine creating {', '.join(sorted(created))}, "
            f"not the resolved {expected}"
        )


def transfer_binding(checked: dict, identity: LiveEngine) -> dict:
    """The transfer mode comes from the runtime check, so it binds the image the check ran,
    or the native process."""
    if isinstance(identity, dict):
        return {"process": identity}
    return {"image_id": engine_image(checked, identity)}


def capture_http_identity(run: Path, out: Path) -> None:
    """Write the live engine's /version and /metrics into `out`, checked against the checked
    runtime and the recorded process start."""
    plan, checked, _ = checked_plan(run)
    identity = live_identity(run, plan, checked)
    with httpx.Client(timeout=10, headers=launch_headers(run, plan)) as client:
        version_response = client.get(plan["endpoint"].rstrip("/") + "/version")
        version_response.raise_for_status()
        metrics_response = client.get(plan["endpoint"].rstrip("/") + "/metrics")
        metrics_response.raise_for_status()
    version = version_response.json()
    if version.get("version") != checked["engine_version"]:
        raise ValueError("HTTP version differs from the checked runtime")
    require_process_start(run, plan, identity, parse_process_start(metrics_response.text))
    write_private_json(out / "version.json", version)
    write_private(out / "metrics.txt", metrics_response.text)


def attestation_captures(context: CaptureContext) -> list[Capture]:
    run, attempt, plan = context.run, context.attempt, context.plan
    # A pod's cache capture hook writes the layout in the engine volume, which the attempt
    # keeps; a container's capture-cache writes it in the launch directory.
    layout = (
        [
            Capture(
                "cache-layout",
                "cache-layout.json",
                lambda: cache_layout_capture(run, plan, attempt),
            )
        ]
        if plan.get("backend") == "kubernetes"
        else []
    )
    return [
        *layout,
        Capture(
            "http-identity",
            "version.json, metrics.txt",
            lambda: capture_http_identity(run, attempt),
        ),
        Capture(
            "connector-version",
            "nixl-connector-version.json",
            lambda: capture_nixl(run, attempt),
        ),
        Capture(
            "cache-registration",
            "cache-registration.json",
            lambda: registration_layout(run, plan, attempt),
        ),
        Capture("transfer-mode", "transfer-mode.json", lambda: capture_transfer_mode(run, attempt)),
        Capture("startup-connector", STARTUP_LOG, lambda: check_startup_connector(attempt)),
        Capture(
            "handshake-policy",
            "handshake-policy.json",
            lambda: handshake_policy(run, plan, attempt),
        ),
    ]


def option(args: list[str], name: str) -> str:
    if args.count(name) != 1:
        raise ValueError(f"Serving plan requires one {name} argument")
    index = args.index(name)
    if index + 1 == len(args):
        raise ValueError(f"Serving plan has no value for {name}")
    return args[index + 1]


def attention_backends(log: str) -> str:
    names = set()
    for pattern in (
        r"\bUsing (?:AttentionBackendEnum\.)?([A-Za-z][A-Za-z0-9_]*) (?:attention )?backend\b",
        r"\bOverriding with ([A-Za-z][A-Za-z0-9_]*) out of potential backends\b",
    ):
        names.update(re.findall(pattern, log))
    if not names:
        raise ValueError(
            "Serving startup log has no resolved attention backend; retain the complete Docker log"
        )
    return ",".join(sorted(names))


def nixl_package(plan: dict) -> str | None:
    packages = plan["expected_packages"]
    return next((name for name in ("nixl", "nixl-rocm") if packages.get(name)), None)


def attestation_evidence(context: AttestationContext) -> AttestationEvidence:
    run, plan, checked, plan_hash = context.run, context.plan, context.checked, context.plan_hash
    attempt, identity = context.attempt, context.identity
    registration = read_json(attempt / "cache-registration.json")
    require_binding(registration, "Cache registration", plan_sha256=plan_hash, image=plan["image"])
    layout = layout_record(run, plan, attempt)
    cache = read_json(layout)
    require_binding(
        cache,
        "Live cache layout",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        launch_config_sha256=plan["launch_sha256"],
    )
    nixl = read_json(attempt / "nixl-connector-version.json")
    require_binding(
        nixl, "NIXL protocol", plan_sha256=plan_hash, **live_binding(plan, checked, identity)
    )
    transfer = read_json(attempt / "transfer-mode.json")
    require_binding(
        transfer, "Transfer mode", plan_sha256=plan_hash, **transfer_binding(checked, identity)
    )
    handshake = read_json(attempt / "handshake-policy.json")
    require_binding(
        handshake,
        "Handshake policy",
        plan_sha256=plan_hash,
        image=plan["image"],
        enforce_handshake_compat=True,
    )
    if handshake.get("connector_config") != plan["connector"]:
        raise ValueError("Handshake policy uses another connector configuration")
    version = read_json(attempt / "version.json").get("version")
    process_start = parse_process_start((attempt / "metrics.txt").read_text())
    require_process_start(run, plan, identity, process_start)
    ranks = cache.get("ranks", [])
    tp = int(option(plan["args"], "--tensor-parallel-size"))
    if sorted(rank.get("rank") for rank in ranks) != list(range(tp)):
        raise ValueError("Live cache layout lacks a TP rank")
    kinds = [{layer["kind"] for layer in rank["layers"]} for rank in ranks]
    if any(group != kinds[0] for group in kinds):
        raise ValueError("Cache kinds differ between TP ranks")
    if (
        registration["kv_cache_layout"] not in {rank.get("kv_cache_layout") for rank in ranks}
        or len({rank.get("kv_cache_layout") for rank in ranks}) != 1
    ):
        raise ValueError("Resolved cache registration differs from the live cache layout")
    if transfer["transfer_mode"] not in {"pull", "push"}:
        raise ValueError("Resolved NIXL transfer mode must be pull or push")
    contract = {
        "connector_version": nixl["nixl_connector_version"],
        "model_dtype": option(plan["args"], "--dtype"),
        "attention_backend": attention_backends(context.startup_log.read_text()),
        "kv_cache_dtype": option(plan["args"], "--kv-cache-dtype"),
        "cross_layers_blocks": registration["cross_layers_blocks"],
        "hybrid_kv_cache_manager": len(kinds[0]) > 1,
        "connector": plan["connector"]["kv_connector"],
        "kv_role": plan["connector"]["kv_role"],
        "transfer_mode": transfer["transfer_mode"],
        "speculative_config": (
            option(plan["args"], "--speculative-config")
            if "--speculative-config" in plan["args"]
            else "disabled"
        ),
        "enforce_handshake_compat": handshake["enforce_handshake_compat"],
    }
    sources = {
        "engine_version": attempt / "version.json",
        "connector_version": attempt / "nixl-connector-version.json",
        "model_dtype": run / "launch.json",
        "attention_backend": context.startup_log,
        "kv_cache_dtype": run / "launch.json",
        "cross_layers_blocks": attempt / "cache-registration.json",
        "hybrid_kv_cache_manager": layout,
        "connector": run / "launch.json",
        "kv_role": run / "launch.json",
        "transfer_mode": attempt / "transfer-mode.json",
        "speculative_config": run / "launch.json",
        "enforce_handshake_compat": attempt / "handshake-policy.json",
    }
    launch = {
        "ucx_version": checked.get("ucx_version") or "",
        "peer_release": checked.get("peer_release") is True,
    }
    return AttestationEvidence(version, contract, sources, launch, process_start)


# Flags whose values change per launch.
LAUNCH_VARYING_FLAGS = frozenset(("--host", "--port", "--served-model-name", "--kv-events-config"))
