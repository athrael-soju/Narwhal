from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ...deployment.attestation_contract.pod_engine import (
    engine_record,
    live_pod,
    recorded_inspection,
)
from ...deployment.launch_engine import pod as pod_steps
from ...deployment.launch_engine.check import read_checked, require_checked
from ...deployment.launch_engine.docker import docker, run_runtime_script
from ...deployment.launch_engine.plan import container_options
from ...deployment.launch_engine.runtime import digest, write_private
from .inspections import (
    CACHE_REGISTRATION,
    CACHE_REGISTRATION_TAG,
    HANDSHAKE_POLICY,
    HANDSHAKE_POLICY_TAG,
    connector_config,
    inspection,
)
from .runtime import LAUNCHER


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


def inspection_log(run: Path, out: Path, name: str) -> str:
    """The path of an inspection log in `out`, relative to the launch directory."""
    return str((out / name).relative_to(run))


def layout_record(run: Path, plan: dict, out: Path) -> Path:
    """The live KV cache layout a capture reads: the one capture-cache kept in the launch
    directory, or for a Kubernetes engine the copy its capture keeps in the attempt `out`."""
    if plan.get("backend") == "kubernetes":
        return out / "cache-layout.json"
    return run / "cache-layout.json"


def resolved_layout(source: Path) -> str:
    """The one KV cache layout that the live cache layout record `source` holds across TP
    ranks."""
    if not source.is_file():
        raise ValueError(f"the engine has captured no live KV cache layout in {source.name}")
    names = {rank.get("kv_cache_layout") for rank in json.loads(source.read_bytes())["ranks"]}
    if len(names) != 1 or None in names:
        raise ValueError(f"{source.name} must hold one resolved KV cache layout across TP ranks")
    return str(next(iter(names)))


def inspected(run: Path, plan: dict, out: Path, name: str, image_arguments: list[str]) -> str:
    """Return an inspection's output from the engine's image: a container of the image, the
    native runtime, or the record of the one a pod's engine container ran.

    `image_arguments` follow the script when it runs in a container of the image or the native
    runtime; a pod's engine container ran it before the engine resolved them.
    """
    log = out / f"{name}.log"
    if plan.get("backend") == "kubernetes":
        engine = live_pod(read_checked(run))
        return recorded_inspection(
            engine, name, inspection(plan, digest(run / "launch.json"), name), log
        )
    script = {"cache-registration": CACHE_REGISTRATION, "handshake-policy": HANDSHAKE_POLICY}[name]
    return run_runtime_script(
        run, plan, script, image_arguments, inspection_log(run, out, log.name)
    )


def registration_layout(run: Path, plan: dict, out: Path) -> None:
    """Write cache-registration.json into `out` from the live cache layout."""
    require_checked(run, plan)
    destination = out / "cache-registration.json"
    if destination.exists():
        raise ValueError(
            "cache registration capture exists; retain it and use a fresh inspection plan"
        )
    source = layout_record(run, plan, out)
    name = resolved_layout(source)
    data = source.read_bytes()
    record = json.loads(data)
    for field, expected in (
        ("image", plan["image"]),
        ("model_config_sha256", plan["model_config_sha256"]),
        ("launch_config_sha256", plan["launch_sha256"]),
    ):
        if record[field] != expected:
            raise ValueError("cache layout record differs from this image, model or launch input")
    tp = int(plan["args"][plan["args"].index("--tensor-parallel-size") + 1])
    if sorted(rank["rank"] for rank in record["ranks"]) != list(range(tp)):
        raise ValueError("cache layout record requires every TP rank exactly once")
    output = inspected(run, plan, out, "cache-registration", [name])
    records = [
        result
        for result in tagged_records(output, CACHE_REGISTRATION_TAG)
        if result.get("kv_cache_layout") == name
    ]
    if len(records) != 1 or type(records[0].get("cross_layers_blocks")) is not bool:
        raise ValueError(
            f"cache registration inspection requires one boolean result for {name}; inspect its log"
        )
    result = records[0]
    result.update(
        image=plan["image"],
        plan_sha256=digest(run / "launch.json"),
        input_path=str(source.resolve()),
        input_sha256=hashlib.sha256(data).hexdigest(),
        input_kind="runtime_cache_layout",
        launcher_sha256=digest(LAUNCHER),
    )
    write_private(destination, json.dumps(result, indent=2) + "\n")


def handshake_policy(run: Path, plan: dict, out: Path) -> None:
    """Write handshake-policy.json into `out` from the installed connector of `run`."""
    require_checked(run, plan)
    destination = out / "handshake-policy.json"
    if destination.exists():
        raise ValueError(
            "handshake policy capture exists; retain it and use a fresh inspection plan"
        )
    connector = connector_config(plan)
    output = inspected(run, plan, out, "handshake-policy", [json.dumps(connector)])
    records = tagged_records(output, HANDSHAKE_POLICY_TAG)
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


def tagged_records(output: str, prefix: str) -> list[dict]:
    return [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]


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
    require_live_layout(json.loads(pending.read_text()), run, plan)
    write_private(destination, pending.read_text())
    pending.unlink()
    print("Live serving cache pages captured in cache-layout.json; container remains running.")


def require_live_layout(record: dict, run: Path, plan: dict) -> None:
    """Require a cache layout that the serving engine's hook wrote to belong to the plan."""
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


def cache_layout_capture(run: Path, plan: dict, out: Path) -> Path:
    """Copy into `out` the cache layout that a pod's cache capture hook wrote for the live
    engine process, checked as capture-cache checks a container's."""
    require_checked(run, plan)
    source = engine_record(live_pod(read_checked(run)), pod_steps.CACHE_LAYOUT)
    text = source.read_text()
    require_live_layout(json.loads(text), run, plan)
    destination = out / "cache-layout.json"
    write_private(destination, text)
    return destination
