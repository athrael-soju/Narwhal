from __future__ import annotations

import json
from pathlib import Path

from ...deployment.launch_engine.check import (
    inspect_image,
    run_native_check,
    start_check,
    write_checked,
)
from ...deployment.launch_engine.docker import docker
from ...deployment.launch_engine.plan import (
    container_options,
    env_file_name,
    read_env,
    requires_remote_code,
)
from .runtime import SETTINGS_TAG


def engine_arguments(plan: dict) -> list[str]:
    # The wrapper's own "-m launch_engine serve" precedes the server arguments.
    return list(plan["args"][3:])


def check(run: Path, plan: dict) -> None:
    native = plan.get("backend") == "native"
    log = start_check(run, plan)
    if requires_remote_code(Path(plan["model_dir"])) and "--trust-remote-code" not in plan["args"]:
        raise ValueError("Model metadata requires --trust-remote-code in the launch record")
    arguments = [
        "_check",
        json.dumps(plan["expected_packages"]),
        json.dumps(engine_arguments(plan)),
    ]
    inspection = None
    if native:
        output = run_native_check(
            run,
            plan,
            log,
            [str(run / "hook/launch_engine.py"), *arguments],
            read_env(run / env_file_name(plan)),
        )
    else:
        inspection = inspect_image(run, plan, log)
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
        "sglang_version": version,
        "prefix_caching": record["prefix_caching"],
        "kv_events": record["kv_events"],
    }
    write_checked(run, plan, evidence, inspection)
    print("Runtime identity, package pins and server arguments passed.")
    if not evidence["prefix_caching"]:
        print("The radix cache is off; the engine publishes no cache events.")
    elif planned is None:
        print("The radix cache is on; cache-event publication is disabled.")
    else:
        print(f"The radix cache is on; cache events publish under {planned['socket_dir']}.")
