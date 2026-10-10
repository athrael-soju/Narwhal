from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ...deployment import stages
from ...deployment.launch_engine.check import require_checked
from ...deployment.launch_engine.docker import docker
from ...deployment.launch_engine.plan import (
    append_private,
    container_options,
    env_file_name,
    kv_events_directory,
    read_env,
    requires_remote_code,
)
from ...deployment.launch_engine.runtime import digest, write_once
from .runtime import SETTINGS_TAG


def engine_arguments(plan: dict) -> list[str]:
    # The wrapper's own "-m launch_engine serve" precedes the server arguments.
    return list(plan["args"][3:])


def check(run: Path, plan: dict) -> None:
    native = plan.get("backend") == "native"
    log = "runtime-check.log" if native else "image-check.log"
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
    if requires_remote_code(Path(plan["model_dir"])) and "--trust-remote-code" not in plan["args"]:
        raise ValueError("Model metadata requires --trust-remote-code in the launch record")
    arguments = [
        "_check",
        json.dumps(plan["expected_packages"]),
        json.dumps(engine_arguments(plan)),
    ]
    inspection = None
    if native:
        result = stages.run(
            [plan["python_executable"], str(run / "hook/launch_engine.py"), *arguments],
            env={**os.environ, **read_env(run / env_file_name(plan))},
            stage="native-runtime-check",
            log=run / log,
        )
        if result.returncode:
            detail = result.stderr.strip().splitlines()
            raise ValueError(
                f"native runtime check exited {result.returncode}: "
                f"{detail[-1] if detail else 'empty stderr'}; inspect {run / log}"
            )
        output = result.stdout
    else:
        inspection = json.loads(docker(["image", "inspect", plan["image"]], run, log))[0]
        expected = plan["image"]
        matches = (
            inspection["Id"] == expected
            if expected.startswith("sha256:")
            else expected in inspection.get("RepoDigests", [])
        )
        if not matches:
            raise ValueError("local image identity differs from the launch plan")
        output = docker(
            [
                "run",
                "--rm",
                *container_options(plan),
                "--entrypoint",
                "python3",
                plan["image"],
                "/narwhal-hooks/launch_engine.py",
                *arguments,
            ],
            run,
            log,
        )
    records = [
        json.loads(line.removeprefix(SETTINGS_TAG))
        for line in output.splitlines()
        if line.startswith(SETTINGS_TAG)
    ]
    if len(records) != 1:
        raise ValueError(f"runtime check requires one runtime record; inspect {run / log}")
    record = records[0]
    version = record.get("sglang_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"runtime check returned an invalid SGLang version; inspect {run / log}")
    if type(record.get("prefix_caching")) is not bool:
        raise ValueError(f"runtime check requires one resolved cache setting; inspect {run / log}")
    planned = plan.get("kv_events")
    expected_events = (
        None if planned is None else {key: planned[key] for key in ("endpoint", "replay_endpoint")}
    )
    if record.get("kv_events") != expected_events:
        raise ValueError(
            f"runtime cache-event endpoints differ from the launch plan; inspect {run / log}"
        )
    evidence = {
        "plan_sha256": digest(run / "launch.json"),
        "sglang_version": version,
        "prefix_caching": record["prefix_caching"],
        "kv_events": record["kv_events"],
    }
    if native:
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
    print("Runtime identity, package pins and server arguments passed.")
    if not evidence["prefix_caching"]:
        print("The radix cache is off; the engine publishes no cache events.")
    elif planned is None:
        print("The radix cache is on; cache-event publication is disabled.")
    else:
        print(f"The radix cache is on; cache events publish under {planned['socket_dir']}.")
