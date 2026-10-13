from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import stages
from .backend import plan_launcher
from .docker import docker
from .plan import (
    append_private,
    container_options,
    env_file_name,
    kv_events_directory,
    option,
    read_env,
    requires_remote_code,
)
from .runtime import digest, write_once

CONTAINER_LAUNCHER = "/narwhal-hooks/launch_engine.py"


@dataclass(frozen=True)
class CheckContext:
    run: Path
    plan: dict
    log: str
    environment: dict[str, str]
    # The tokenizer path in the serving namespace, and whether the launch trusts remote code.
    tokenizer: str
    trust_remote_code: bool

    @property
    def native(self) -> bool:
        return self.plan.get("backend") == "native"

    @property
    def launcher_path(self) -> str:
        return str(self.run / "hook/launch_engine.py") if self.native else CONTAINER_LAUNCHER

    def failure(self, message: str) -> ValueError:
        return ValueError(f"{message}; inspect {self.run / self.log}")


@dataclass(frozen=True)
class ProbeResult:
    version: object
    prefix_caching: object
    kv_events: object
    # Engine-specific evidence, recorded after the shared fields.
    fields: dict = field(default_factory=dict)
    # Lines printed between the summary and the cache line.
    notes: tuple[str, ...] = ()


def check(run: Path, plan: dict) -> None:
    engine = plan_launcher(plan)
    native = plan.get("backend") == "native"
    log = start_check(run, plan)
    mapping = engine.launch_settings
    default_tokenizer = plan["model_dir"] if native else "/model"
    flag = mapping.settings["tokenizer"].flag
    tokenizer = (flag and option(plan["args"], flag)) or default_tokenizer
    trust_remote_code = mapping.remote_code_flag in plan["args"]
    # The probe checks a separate tokenizer's metadata in the serving namespace.
    if (
        requires_remote_code(
            Path(plan["model_dir"]), include_tokenizer=tokenizer == default_tokenizer
        )
        and not trust_remote_code
    ):
        raise ValueError(f"Model metadata requires {mapping.remote_code_flag} in the launch record")
    context = CheckContext(
        run,
        plan,
        log,
        read_env(run / env_file_name(plan)),
        tokenizer,
        trust_remote_code,
    )
    inspection = None
    if native:
        model_path = Path(plan["model_path"])
        if not model_path.exists():
            raise ValueError(f"native model path is missing: {model_path}")
        if plan.get("model_sha256") and digest(model_path) != plan["model_sha256"]:
            raise ValueError("native model file changed after launch preparation")
        arguments = engine.runtime_probe(context)
        output = run_native_check(run, plan, log, arguments, context.environment)
    else:
        arguments = engine.runtime_probe(context)
        inspection = inspect_image(run, plan, log)
        output = docker(
            [
                "run",
                "--rm",
                *container_options(plan),
                "--entrypoint",
                "python3",
                plan["image"],
                *arguments,
            ],
            run,
            log,
        )
    result = engine.probe_result(context, output)
    if not isinstance(result.version, str) or not result.version.strip():
        raise context.failure(f"runtime check returned an invalid {engine.version_label} version")
    if type(result.prefix_caching) is not bool:
        raise context.failure("runtime check requires one resolved cache setting")
    planned = plan.get("kv_events")
    expected = (
        None if planned is None else {key: planned[key] for key in ("endpoint", "replay_endpoint")}
    )
    if result.kv_events != expected:
        raise context.failure("runtime cache-event endpoints differ from the launch plan")
    evidence = {
        "engine_version": result.version,
        "prefix_caching": result.prefix_caching,
        "kv_events": result.kv_events,
        **result.fields,
    }
    write_checked(run, plan, evidence, inspection)
    print(engine.check_summary)
    for note in result.notes:
        print(note)
    cache = engine.prefix_cache_name
    if not result.prefix_caching:
        print(f"{cache} is off; the engine publishes no cache events.")
    elif planned is None:
        print(f"{cache} is on; cache-event publication is disabled.")
    else:
        print(f"{cache} is on; cache events publish under {planned['socket_dir']}.")


def read_checked(run: Path) -> dict:
    return json.loads((run / "checked.json").read_text())


def require_checked(run: Path, plan: dict) -> None:
    checked = read_checked(run)
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
