from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

from ...engines.attestation import fetch_engine_identity
from ...engines.host_process import process_clock
from ..launch_engine.backend import engine_backend
from ..launch_engine.plan import read_env
from ..launch_engine.runtime import digest, write_private
from ..native_engine import process_identity


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def write_private_json(path: Path, value: dict) -> None:
    write_private(path, json.dumps(value, indent=2) + "\n")


def checked_plan(run: Path) -> tuple[dict, dict, str]:
    plan_path = run / "launch.json"
    plan = read_json(plan_path)
    checked = read_json(run / "checked.json")
    plan_hash = digest(plan_path)
    if checked.get("plan_sha256") != plan_hash:
        raise ValueError("Runtime check belongs to another serving plan")
    if plan.get("backend") == "native":
        if (
            checked.get("backend") != "native"
            or checked.get("python_executable") != plan.get("python_executable")
            or checked.get("expected_packages") != plan.get("expected_packages")
        ):
            raise ValueError("Native runtime check differs from the serving plan")
    elif not re.fullmatch(r"sha256:[0-9a-f]{64}", checked.get("image_id", "")):
        raise ValueError("Image check lacks an immutable image ID")
    return plan, checked, plan_hash


def live_container(run: Path, checked: dict) -> str:
    cid = (run / "container.id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise ValueError("Serving container ID is invalid")
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .}}", cid],
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(result.stdout)
    if observed.get("Id") != cid or observed.get("Image") != checked["image_id"]:
        raise ValueError("Serving container differs from the checked image or recorded ID")
    if observed.get("State", {}).get("Running") is not True:
        raise ValueError("Serving container must be running before attestation")
    return cid


def live_native(run: Path, plan: dict, checked: dict) -> dict:
    process = read_json(run / "native-process.json")
    try:
        if process_identity(process["pid"]) != process:
            raise ValueError("Native serving process identity changed")
    except (FileNotFoundError, ProcessLookupError) as error:
        raise ValueError("Native serving process is no longer running") from error
    backend = engine_backend(plan.get("engine"))
    engine = backend.launcher()
    startup = read_json(run / "shared-start.json")
    if (
        startup.get("status") != "running"
        or startup.get("plan_sha256") != digest(run / "launch.json")
        or startup.get("process") != process
        or startup.get(engine.version_field) != checked[engine.checked_version_field]
    ):
        raise ValueError("Native startup evidence differs from the live serving plan")
    values = read_env(run / "engine.env")
    key = values.get(engine.api_key_env, "")
    headers = {"Authorization": f"Bearer {key}"} if key else None
    identity = asyncio.run(
        fetch_engine_identity(
            plan["endpoint"],
            headers=headers,
            reader=backend.identity,
            process=process_clock(pid=process["pid"]),
        )
    )
    if (
        identity.version != startup[engine.version_field]
        or identity.process_start_time_seconds != startup["process_start_time_seconds"]
    ):
        raise ValueError("Native engine HTTP identity changed since launch")
    return process


def parse_tagged_capture(output: str, tag: str, label: str) -> dict:
    captures = [line.removeprefix(tag) for line in output.splitlines() if line.startswith(tag)]
    if len(captures) != 1:
        raise ValueError(f"Expected one tagged {label} capture, received {len(captures)}")
    try:
        record = json.loads(captures[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Tagged {label} capture contains invalid JSON") from exc
    if not isinstance(record, dict):
        raise ValueError(f"Tagged {label} capture must contain a JSON object")
    return record


def require_binding(record: dict, label: str, **expected: object) -> None:
    for field, value in expected.items():
        if record.get(field) != value:
            raise ValueError(f"{label} {field} differs from the checked serving plan")


def require_prior_dimensions(run: Path, plan: dict, plan_hash: str, contract: object) -> None:
    original = run / "model-dimensions.json"
    if original.exists():
        previous = read_json(original)
        require_binding(
            previous,
            "Prior model dimensions",
            plan_sha256=plan_hash,
            image=plan["image"],
            revision=plan["revision"],
            model_config_sha256=plan["model_config_sha256"],
        )
        if previous.get("contract") != contract:
            raise ValueError("Live model dimensions differ from the retained plan capture")
