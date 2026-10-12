from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .. import stages
from .backend import plan_launcher
from .docker import docker
from .plan import append_private, kv_events_directory
from .runtime import digest, write_once


def check(run: Path, plan: dict) -> None:
    plan_launcher(plan).check(run, plan)


def require_checked(run: Path, plan: dict) -> None:
    checked = json.loads((run / "checked.json").read_text())
    if checked["plan_sha256"] != digest(run / "launch.json"):
        raise ValueError("launch plan changed after its runtime check")
    if plan.get("backend") == "native" and (
        checked.get("backend") != "native"
        or checked.get("python_executable") != plan["python_executable"]
        or checked.get("expected_packages") != plan["expected_packages"]
    ):
        raise ValueError("native runtime check differs from the launch plan")


def start_check(run: Path, plan: dict) -> str:
    log = "runtime-check.log" if plan.get("backend") == "native" else "image-check.log"
    if (run / "checked.json").exists():
        require_checked(run, plan)
    kv_events_directory(plan)
    append_private(
        run / log,
        "\n"
        + json.dumps(
            {"check_attempt": uuid.uuid4().hex, "plan_sha256": digest(run / "launch.json")}
        )
        + "\n",
    )
    return log


def inspect_image(run: Path, plan: dict, log: str) -> dict:
    inspection = json.loads(docker(["image", "inspect", plan["image"]], run, log))[0]
    expected = plan["image"]
    if expected.startswith("sha256:"):
        matches = inspection["Id"] == expected
    else:
        matches = expected in inspection.get("RepoDigests", [])
    if not matches:
        raise ValueError("local image identity differs from the launch plan")
    return inspection


def run_native_check(
    run: Path, plan: dict, log: str, arguments: list[str], env: dict[str, str]
) -> str:
    result = stages.run(
        [plan["python_executable"], *arguments],
        env={**os.environ, **env},
        stage="native-runtime-check",
        log=run / log,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise ValueError(
            f"native runtime check exited {result.returncode}: "
            f"{detail[-1] if detail else 'empty stderr'}; inspect {run / log}"
        )
    return result.stdout


def write_checked(run: Path, plan: dict, fields: dict, inspection: dict | None) -> None:
    # write_once compares bytes, so the key order stays fixed: plan hash, backend fields, mode.
    evidence = {"plan_sha256": digest(run / "launch.json"), **fields}
    if plan.get("backend") == "native":
        evidence.update(
            backend="native",
            python_executable=plan["python_executable"],
            expected_packages=plan["expected_packages"],
        )
    else:
        assert inspection is not None
        evidence["image_id"] = inspection["Id"]
    write_once(
        run / "checked.json",
        json.dumps(evidence),
        "runtime identity changed; prepare a fresh launch directory",
    )
