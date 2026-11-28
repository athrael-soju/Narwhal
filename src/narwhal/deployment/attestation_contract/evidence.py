from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ...engines import host_process
from ...engines.host_process import started_at_seconds
from ..launch_engine.backend import LiveModelDimensions, plan_launcher
from ..launch_engine.check import read_checked
from ..launch_engine.kubernetes import UID
from ..launch_engine.plan import env_file_name, read_env
from ..launch_engine.runtime import digest, write_private
from ..native_engine import native_identity, process_identity
from .pod_engine import PodEngine, live_pod, model_config

# A live engine: the serving container's ID, a native engine's process identity, or the
# engine container of a Kubernetes pod.
LiveEngine = str | dict | PodEngine


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def write_private_json(path: Path, value: dict) -> None:
    write_private(path, json.dumps(value, indent=2) + "\n")


def read_native_start(run: Path) -> dict:
    """The start record of a native engine, from start-native or start-shared."""
    own = run / "native-start.json"
    return read_json(own if own.exists() else run / "shared-start.json")


def checked_plan(run: Path) -> tuple[dict, dict, str]:
    plan_path = run / "launch.json"
    plan = read_json(plan_path)
    checked = read_checked(run)
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
    elif plan.get("backend") == "kubernetes":
        # The pod reports the image it runs; the attestation reads it from the pod.
        if checked.get("backend") != "kubernetes" or not re.fullmatch(
            UID, str(checked.get("pod_uid"))
        ):
            raise ValueError("Runtime check names no engine pod of the Kubernetes plan")
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
        timeout=15,
        check=True,
    )
    observed = json.loads(result.stdout)
    if observed.get("Id") != cid or observed.get("Image") != checked["image_id"]:
        raise ValueError("Serving container differs from the checked image or recorded ID")
    state = observed.get("State", {})
    if state.get("Running") is not True:
        raise ValueError("Serving container is not running; inspect its logs")
    recorded = container_start(run)
    if recorded["container_id"] != cid or state.get("StartedAt") != recorded["started_at"]:
        raise ValueError(
            "Serving container started again after narwhal-engine started it; its evidence "
            "describes an earlier process, so prepare, check and start a fresh launch"
        )
    return cid


def container_start(run: Path) -> dict:
    """The container start that narwhal-engine recorded in container-start.json."""
    path = run / "container-start.json"
    if not path.is_file():
        raise ValueError(
            "container-start.json is missing; start the engine with this release's narwhal-engine"
        )
    record = read_json(path)
    started_at_seconds(record["started_at"])
    return record


# The serving process is the container's first process, so the process start in /metrics and
# the container's State.StartedAt name one moment. /metrics derives the start from the kernel's
# boot time, which has one-second resolution.
CONTAINER_START_TOLERANCE_S = 2.0


def require_process_start(
    run: Path, plan: dict, identity: LiveEngine, process_start: float
) -> None:
    """Require an engine's reported process start to be the start of the recorded process."""
    if plan.get("backend") == "native":
        if process_start != read_native_start(run)["process_start_time_seconds"]:
            raise ValueError("HTTP process start differs from the native launch record")
        return
    if isinstance(identity, PodEngine):
        # The engine is the first process of its container, which the pod's identity names.
        started = identity.identity.process_start_time_seconds
    else:
        started = started_at_seconds(container_start(run)["started_at"])
    if abs(process_start - started) > CONTAINER_START_TOLERANCE_S:
        raise ValueError(
            f"HTTP process start {process_start} is not the container start {started}; "
            "the endpoint serves another process"
        )


def live_native(run: Path, plan: dict, checked: dict) -> dict:
    process = read_json(run / "native-process.json")
    try:
        if process_identity(process["pid"]) != process:
            raise ValueError("Native serving process identity changed")
    except (FileNotFoundError, ProcessLookupError) as error:
        raise ValueError("Native serving process is no longer running") from error
    startup = read_native_start(run)
    if (
        startup.get("status") != "running"
        or startup.get("plan_sha256") != digest(run / "launch.json")
        or startup.get("process") != process
        or startup.get("engine_version") != checked["engine_version"]
    ):
        raise ValueError("Native startup evidence differs from the live serving plan")
    identity = native_identity(run, plan, process["pid"])
    if (
        identity.version != startup["engine_version"]
        or identity.process_start_time_seconds != startup["process_start_time_seconds"]
    ):
        raise ValueError("Native engine HTTP identity changed since launch")
    return process


def live_identity(run: Path, plan: dict, checked: dict) -> LiveEngine:
    """The live engine of a checked plan, through the identity source of its launch backend:
    the native process, the Docker container, or the Kubernetes pod."""
    if plan.get("backend") == "native":
        return live_native(run, plan, checked)
    if plan.get("backend") == "kubernetes":
        return live_pod(checked)
    return live_container(run, checked)


def live_binding(
    plan: dict, checked: dict, identity: LiveEngine, *, model_revision: bool = False
) -> dict:
    if isinstance(identity, PodEngine):
        return identity.binding()
    if plan.get("backend") != "native":
        return {"image_id": checked["image_id"], "container_id": identity}
    if model_revision:
        return {"process": identity, "model_revision": plan["model_revision"]}
    return {"process": identity}


def engine_image(checked: dict, identity: LiveEngine) -> str:
    """The immutable image ID the engine runs: the one its pod reports, or the one the image
    check observed; a native engine runs none."""
    if isinstance(identity, PodEngine):
        return identity.identity.image_id
    return "" if isinstance(identity, dict) else str(checked["image_id"])


def engine_process_start(identity: LiveEngine) -> float:
    """Seconds since the epoch at which the live engine's process started."""
    if isinstance(identity, PodEngine):
        return identity.identity.process_start_time_seconds
    if isinstance(identity, dict):
        return host_process.native_start(identity["pid"])
    return host_process.container_start(identity)


def engine_environment(run: Path, plan: dict, identity: LiveEngine) -> dict[str, str]:
    """The engine's launch environment as the engine received it: a pod's holds its address
    where the launch names the pod address."""
    values = read_env(run / env_file_name(plan))
    return identity.environment(values) if isinstance(identity, PodEngine) else values


def planned_model_config(plan: dict) -> Path:
    """The model configuration the engine serves: in its pod, or in the model directory."""
    if plan.get("backend") == "kubernetes":
        return model_config()
    return Path(plan["model_dir"]) / "config.json"


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


def model_contract(config: dict) -> dict[str, Any]:
    text = config.get("text_config", config)
    heads = text.get("num_attention_heads")
    head_size = text.get("head_dim") or (
        text["hidden_size"] // heads if isinstance(heads, int) and heads > 0 else None
    )
    values = {
        "kv_heads": text.get("num_key_value_heads", heads),
        "head_size": head_size,
        "hidden_layers": text.get("num_hidden_layers"),
    }
    if any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("model config lacks positive KV head, head size and layer counts")
    architectures = config.get("architectures") or []
    if not architectures or not isinstance(architectures[0], str):
        raise ValueError("model config names no architecture")
    return {"contract": values, "model_architecture": architectures[0]}


def config_model_dimensions(run: Path, plan: dict, out: Path) -> Path:
    """Write model-dimensions.json into `out` from the launch's model configuration."""
    destination = out / "model-dimensions.json"
    path = planned_model_config(plan)
    if digest(path) != plan["model_config_sha256"]:
        raise ValueError("Model configuration changed after launch preparation")
    record = {
        **model_contract(json.loads(path.read_text())),
        "sources": {"model_config": f"{path.resolve()} sha256:{digest(path)}"},
        "plan_sha256": digest(run / "launch.json"),
        "image": plan["image"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
    }
    if destination.exists():
        if read_json(destination) != record:
            raise ValueError("Model dimensions differ from the retained capture")
        return destination
    write_private_json(destination, record)
    return destination


def model_dimensions(run: Path, plan: dict) -> None:
    engine = plan_launcher(plan)
    if isinstance(engine, LiveModelDimensions):
        engine.model_dimensions(run, plan)
    else:
        checked_plan(run)
        config_model_dimensions(run, plan, run)


def capture_model_dimensions(run: Path, out: Path) -> Path:
    plan, _, _ = checked_plan(run)
    engine = plan_launcher(plan)
    if isinstance(engine, LiveModelDimensions):
        return engine.capture_model_dimensions(run, out)
    return config_model_dimensions(run, plan, out)
