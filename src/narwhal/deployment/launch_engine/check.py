from __future__ import annotations

import json
from pathlib import Path

from .backend import plan_launcher
from .runtime import digest


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
