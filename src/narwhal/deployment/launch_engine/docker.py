from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from .. import stages
from .plan import container_options, read_env
from .runtime import write_private


def _docker_owner(run: Path) -> str:
    marker = run / "docker-owner.json"
    if not marker.exists():
        with contextlib.suppress(FileExistsError):
            write_private(marker, json.dumps({"label": uuid.uuid4().hex}))
    owner = json.loads(marker.read_text())["label"]
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("docker-owner.json requires a valid ownership token")
    return owner


def reconcile_docker(
    run: Path,
    owner: str,
    *,
    remove: bool,
    operation: str | None = None,
    targets: tuple[str, ...] = (),
) -> dict:
    budget = stages.seconds("NARWHAL_DOCKER_RECONCILE_SECONDS", 30)
    started = time.monotonic()
    report: dict = {
        "budget_seconds": budget,
        "owner": owner,
        "operation": operation,
        "targets": list(targets),
        "removed": [],
        "preserved_resources": [],
        "surviving_resources": [],
        "status": "inspection_required",
    }
    recorded = set()
    for name in ("container.id", "cache-probe.id"):
        path = run / name
        if path.exists():
            cid = path.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{64}", cid):
                recorded.add(cid)

    def invoke(args: list[str]) -> str:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            raise ValueError("Docker reconciliation budget exhausted")
        result = stages.run(
            ["docker", *args],
            stage="docker-reconcile-" + args[0],
            log=run / "docker-reconcile.log",
            timeout=remaining,
        )
        if result.returncode:
            raise ValueError(f"Docker reconciliation {args[0]} exited {result.returncode}")
        return result.stdout.strip()

    try:
        listed = invoke(["ps", "-aq", "--no-trunc", "--filter", f"label=io.narwhal.launch={owner}"])
        ids = recorded | {cid for cid in listed.splitlines() if re.fullmatch(r"[0-9a-f]{64}", cid)}
        report["candidates"] = sorted(ids)
        # The all-container listing permits an absent recorded ID after an earlier cleanup.
        present = set(invoke(["ps", "-aq", "--no-trunc"]).splitlines()) & ids
        owned = []
        cleanup = []
        if present:
            records = json.loads(invoke(["inspect", *sorted(present)]))
            for record in records:
                cid = record["Id"]
                labels = record.get("Config", {}).get("Labels") or {}
                if cid in present and (cid in recorded or labels.get("io.narwhal.launch") == owner):
                    owned.append(cid)
                    if remove and (
                        cid in targets
                        or (
                            operation is not None
                            and labels.get("io.narwhal.operation") == operation
                        )
                    ):
                        cleanup.append(cid)
                else:
                    report.setdefault("refused_resources", []).append(cid)
        report["surviving_resources"] = sorted(owned)
        report["preserved_resources"] = sorted(set(owned) - set(cleanup))
        if cleanup:
            invoke(["rm", "--force", *cleanup])
        remaining = set(invoke(["ps", "-aq", "--no-trunc"]).splitlines())
        report["surviving_resources"] = sorted(set(owned) & remaining)
        if remove:
            report["removed"] = sorted(set(cleanup) - remaining)
        report["status"] = "observed"
    except (OSError, ValueError, KeyboardInterrupt) as error:
        report["error"] = str(error)
    report.update(
        elapsed_seconds=time.monotonic() - started,
        observed_at=time.time(),
        recovery="inspect docker-reconcile-*.json and recorded IDs/ownership label before retrying",
    )
    destination = run / ("docker-reconcile-" + uuid.uuid4().hex[:12] + ".json")
    write_private(destination, json.dumps(report, indent=2) + "\n")
    report["evidence"] = str(destination)
    return report


def docker(command: list[str], run: Path, log: str, *, include_stderr: bool = False) -> str:
    owner = _docker_owner(run)
    invocation = list(command)
    operation = uuid.uuid4().hex if command[0] in {"create", "run"} else None
    targets = (
        tuple(value for value in command[1:] if re.fullmatch(r"[0-9a-f]{64}", value))
        if command[0] == "start"
        else ()
    )
    if operation is not None:
        invocation[1:1] = [
            "--label",
            f"io.narwhal.launch={owner}",
            "--label",
            f"io.narwhal.operation={operation}",
        ]
    try:
        result = stages.run(["docker", *invocation], stage="docker-" + command[0], log=run / log)
    except (stages.StageTimeout, stages.StageCancelled) as error:
        try:
            report = reconcile_docker(
                run,
                owner,
                remove=command[0] in {"create", "run", "start"},
                operation=operation,
                targets=targets,
            )
        except OSError as cleanup_error:
            report = {
                "status": "inspection_required",
                "error": str(cleanup_error),
                "recovery": "inspect the recorded container IDs and ownership label",
            }
        error.context["docker_reconciliation"] = report
        error.context["recovery"] = report["recovery"]
        with contextlib.suppress(OSError):
            stages.write_evidence(Path(error.context["evidence"]), error.context)
        raise
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError(
            f"Docker command exited {result.returncode}: "
            f"{detail[-1] if detail else 'empty subprocess output'}; inspect {run / log}"
        )
    return (result.stdout + result.stderr if include_stderr else result.stdout).strip()


def run_runtime_script(run: Path, plan: dict, script: str, arguments: list[str], log: str) -> str:
    if plan.get("backend") != "native":
        return docker(
            [
                "run",
                "--rm",
                *container_options(plan),
                "--entrypoint",
                "python3",
                plan["image"],
                "-c",
                script,
                *arguments,
            ],
            run,
            log,
        )
    values = read_env(run / "engine.env")
    if (run / log).exists():
        raise FileExistsError(17, "inspection log already exists", str(run / log))
    result = stages.run(
        [plan["python_executable"], "-c", script, *arguments],
        env={**os.environ, **values, "NARWHAL_CAPTURE_CACHE": "0"},
        stage="native-runtime-inspection",
        log=run / log,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise ValueError(
            f"native runtime inspection exited {result.returncode}: "
            f"{detail[-1] if detail else 'empty stderr'}; inspect {run / log}"
        )
    return result.stdout
