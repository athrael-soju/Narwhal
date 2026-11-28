from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx

from narwhal.backends import DEFAULT_BACKEND
from narwhal.backends import load as load_backend
from narwhal.config import FleetConfig
from narwhal.deployment import native_engine, stages
from narwhal.deployment.launch_engine.backend import launcher
from narwhal.deployment.launch_engine.start import gpu_memory
from narwhal.diagnostics.check.evidence import verify_directed_kv_evidence
from narwhal.engines import host_process

from .template import (
    _check_free_ports,
    _port_layout,
    _sha256,
    check_instance_ports,
    instance_gpus,
    model_files,
    starts_directly,
)

# An `up` that failed, or that ended without recording its end, resumes in its run.
RESUMABLE = ("failed", "starting")


class LifecycleDocumentError(ValueError):
    pass


class LifecycleOperationError(RuntimeError):
    pass


def read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _validate_identity(identity: dict, path: Path, field: str) -> None:
    if type(identity.get("pid")) is not int or identity["pid"] < 1:
        raise LifecycleDocumentError(f"{path}: {field}.pid must be a positive integer")
    if not isinstance(identity.get("boot_id"), str) or not identity["boot_id"].strip():
        raise LifecycleDocumentError(f"{path}: {field}.boot_id must be a nonempty string")
    if type(identity.get("start_ticks")) is not int or identity["start_ticks"] < 0:
        raise LifecycleDocumentError(f"{path}: {field}.start_ticks must be a nonnegative integer")


def _read_identity(path: Path) -> dict:
    try:
        identity = read(path)
    except ValueError as exc:
        raise LifecycleDocumentError(f"{path}: {exc}") from exc
    _validate_identity(identity, path, "identity")
    return identity


def _read_state(root: Path) -> dict:
    path = root / "lifecycle.json"
    try:
        state = read(path)
    except ValueError as exc:
        raise LifecycleDocumentError(f"{path}: {exc}") from exc
    for name, kind in (("run", str), ("phase", str), ("processes", list)):
        if not isinstance(state.get(name), kind):
            raise LifecycleDocumentError(f"{path}: {name} must be {kind.__name__}")
    if "verification" in state and not isinstance(state["verification"], str):
        raise LifecycleDocumentError(f"{path}: verification must be a path string")
    for index, record in enumerate(state["processes"]):
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or not isinstance(record.get("identity"), dict)
        ):
            raise LifecycleDocumentError(
                f"{path}: processes[{index}] requires a name string and identity object"
            )
        _validate_identity(record["identity"], path, f"processes[{index}].identity")
    stage_failure = state.get("failure")
    if stage_failure is not None and (
        not isinstance(stage_failure, dict) or not isinstance(stage_failure.get("message"), str)
    ):
        raise LifecycleDocumentError(f"{path}: failure requires a message string")
    if "failed_verification" in state and not isinstance(state["failed_verification"], str):
        raise LifecycleDocumentError(f"{path}: failed_verification must be a path string")
    failure = state.get("verification_failure")
    if failure is not None and (
        not isinstance(failure, dict) or not isinstance(failure.get("reason"), str)
    ):
        raise LifecycleDocumentError(f"{path}: verification_failure requires a reason string")
    return state


def write(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def instance(root: Path) -> dict:
    value = read(root / "instance.json")
    if value.get("schema") != "narwhal.dev-instance" or value.get("schema_version") != 1:
        raise ValueError("instance requires narwhal.dev-instance schema version 1")
    if not starts_directly(value):
        check_instance_ports(value, root)
    expected = Path(value["python_executable"])
    current = Path(sys.executable)
    if expected.parent.resolve() != current.parent.resolve() or not expected.samefile(current):
        raise ValueError("run this instance with the Python environment used by dev init")
    return value


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    with (root / "lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another lifecycle command owns this instance") from exc
        yield


def _busy(root: Path) -> bool:
    try:
        with locked(root):
            return False
    except ValueError:
        return True


def _run(root: Path, module: str, args: list[str], log: str, default_timeout: float = 300) -> None:
    # Unbuffered output keeps progress ahead of the final diagnostic.
    command = [sys.executable, "-u", "-m", module, *args]
    write(root / f"{log}.command.json", {"argv": command})
    result = stages.run(
        command,
        stage=log,
        log=root / f"{log}.log",
        cwd=root,
        default_timeout=default_timeout,
        retain_descendants=log in ("native-start-shared", "native-start"),
    )
    if result.returncode:
        path = root / f"{log}.log"
        lines = path.read_text(errors="replace").strip().splitlines()
        detail = lines[-1] if lines else "empty subprocess output"
        raise LifecycleOperationError(f"{log} exited {result.returncode}: {detail}; inspect {path}")


@contextlib.contextmanager
def memory_samples(run: Path, gpus: list[str], name: str) -> Iterator[None]:
    """Sample each GPU's memory every half second; with several GPUs each row names its GPU."""
    stopped = threading.Event()

    def sample() -> None:
        with (run / f"{name}-memory.jsonl").open("a") as output:
            while not stopped.is_set():
                for gpu in gpus:
                    named = {"gpu_uuid": gpu} if len(gpus) > 1 else {}
                    try:
                        row: dict[str, object] = {"time": time.time(), **named, **gpu_memory(gpu)}
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        row = {"time": time.time(), **named, "error": str(exc)}
                    output.write(json.dumps(row) + "\n")
                output.flush()
                stopped.wait(0.5)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=20)


def _helper_records(run: Path) -> list[tuple[Path, dict]]:
    records = []
    for path in sorted(run.rglob("*.stage.json")):
        record = read(path)
        if record.get("status") in {"running", "recovery_required"} or record.get(
            "cleanup", {}
        ).get("surviving_processes"):
            records.append((path, record))
    return records


def _engine_records(run: Path, *, every_attempt: bool) -> list[dict]:
    """The engines of a launched instance, as their launch directories record them.

    The native launcher records each engine before its health checks, failures included. An
    engine's newest launch directory names its current process; `every_attempt` adds the
    processes of its earlier launch directories in the run.
    """
    from . import launch

    records = []
    for engine, directories in launch.launches(run).items():
        for directory in directories if every_attempt else directories[-1:]:
            path = directory / "native-process.json"
            if path.exists():
                records.append({"name": engine, "identity": _read_identity(path)})
    return records


def _stop(root: Path, state: dict) -> None:
    from . import launch

    errors = []
    run = Path(state["run"])
    records = _engine_records(run, every_attempt=True) + list(state.get("processes", []))
    stopped = []
    for path, helper in _helper_records(run):
        try:
            cleanup = stages.recover(path)
            if cleanup["surviving_processes"]:
                errors.append(
                    f"{helper['stage']}: surviving helper PIDs {cleanup['surviving_processes']}"
                )
            else:
                stopped.append("helper:" + helper["stage"])
        except (OSError, ValueError) as error:
            errors.append(f"{helper['stage']}: {error}")
    for record in reversed(records):
        try:
            if launch.SUPERVISOR.stop(launch.process_group(record["identity"])):
                stopped.append(record["name"])
        except (OSError, ValueError) as exc:
            errors.append(f"{record['name']}: {exc}")
    state.update(phase="degraded" if errors else "stopped", stopped=stopped, errors=errors)
    if not errors:
        state.pop("verification_failure", None)
    write(root / "lifecycle.json", state)
    write(run / "teardown.json", {"time": time.time(), "stopped": stopped, "errors": errors})
    if errors:
        raise ValueError("; ".join(errors))


def _mark(root: Path) -> str:
    return f"fleet-{hashlib.sha256(str(root).encode()).hexdigest()[:16]}"


def _started_elsewhere(root: Path, record: bool) -> list[Path]:
    """The user's other instances with a fleet up, read from the user's record directory.

    Each `up` leaves a mark there naming its instance; a mark whose instance is stopped or
    gone is removed. With `record`, this instance's mark is written.
    """
    try:
        directory = host_process._records()
    except (OSError, ValueError):
        return []
    try:
        others = []
        for entry in os.listdir(directory):
            if not entry.startswith("fleet-") or entry == _mark(root):
                continue
            try:
                flags = os.O_RDONLY | os.O_NOFOLLOW
                with os.fdopen(os.open(entry, flags, dir_fd=directory)) as stream:
                    other = Path(stream.read(4096))
                up_there = _fleet_up(other)
            except (OSError, ValueError, KeyError, TypeError):
                up_there = False
            if up_there:
                others.append(other)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(entry, dir_fd=directory)
        if record:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            with os.fdopen(os.open(_mark(root), flags, 0o600, dir_fd=directory), "w") as stream:
                stream.write(str(root))
        return sorted(others)
    except OSError:
        return []
    finally:
        os.close(directory)


def _fleet_up(root: Path) -> bool:
    """Whether a lifecycle command holds the instance, or a recorded process of its fleet runs."""
    if not root.is_dir():
        return False
    if _busy(root):
        return True
    state = json.loads((root / "lifecycle.json").read_text())
    if state["phase"] == "stopped":
        return False
    for record in state["processes"]:
        try:
            os.kill(record["identity"]["pid"], 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        return True
    return False


def up(root: Path, alongside: bool = False) -> dict:
    config = instance(root)
    others = _started_elsewhere(root, record=False)
    if others and not alongside:
        # A second fleet takes its own share of memory beside the first.
        raise ValueError(
            f"a fleet is already up in {others[0]}; stop it with narwhal dev down --instance "
            f"{others[0]}, or pass --alongside to start this fleet beside it"
        )
    _started_elsewhere(root, record=True)
    from . import launch

    with locked(root):
        # setup may have changed the instance since it was read.
        config = instance(root)
        previous = _read_state(root) if (root / "lifecycle.json").exists() else None
        phase = previous["phase"] if previous is not None else "stopped"
        resuming = phase in RESUMABLE
        if not resuming and phase != "stopped":
            raise ValueError("the fleet is already up; run narwhal dev down to stop it")
        spec = read(root / "template.json")
        ports = _launch_checks(root, config, spec)
        if previous is not None and resuming:
            run = Path(previous["run"])
            state = previous
            state.pop("failure", None)
        else:
            _check_free_ports(ports, "127.0.0.1")
            if starts_directly(config):
                _remove_earlier_runs(root, previous)
            run = root / f"run-{uuid.uuid4().hex[:12]}"
            run.mkdir(mode=0o700)
            state = {"schema_version": 1, "run": str(run), "processes": []}
        state["phase"] = "starting"
        write(root / "lifecycle.json", state)
        samples = (
            contextlib.nullcontext()
            if starts_directly(config)
            else memory_samples(run, instance_gpus(config), "up")
        )
        try:
            with samples:
                launch.run(root, run, config, spec, state, resuming)
        except BaseException as error:
            # The stages that passed keep their processes, so the next up resumes here.
            state["phase"] = "failed"
            state["failure"] = {
                "message": str(error) or type(error).__name__,
                "stage": getattr(error, "stage", "startup"),
                "context": getattr(error, "context", {}),
            }
            write(root / "lifecycle.json", state)
            raise
        state["phase"] = "launched"
        write(root / "lifecycle.json", state)
        return {"status": "launched", "run": str(run), "router": config["router_url"]}


def _launch_checks(root: Path, config: dict, spec: dict) -> set[int]:
    """Check the engine runtime and the model files against the instance; return its ports."""
    if starts_directly(config):
        from . import direct

        start = direct._direct_start(spec)
        binary = direct._binary(Path(config["engine_binary"]))
        if start.check(binary, direct._runtime(spec)) != config["engine_version"]:
            raise ValueError("engine binary changed after dev init")
        direct._model(spec, Path(config["model_path"]))
        return direct._ports(spec, config["engine_count"])[1]
    launcher(spec["runtime"].get("backend")).check_dev_files(spec["runtime"])
    for path, expected in model_files(spec["model"], Path(config["model_path"])).items():
        if _sha256(path) != expected:
            raise ValueError(f"model file {path.name} changed after dev init")
    for name, expected in spec["model"].get("tokenizer_sha256", {}).items():
        if _sha256(Path(config["model_dir"]) / name) != expected:
            raise ValueError(f"tokenizer file {name} changed after dev init")
    return _port_layout(spec, config["engine_count"])[1]


def _remove_earlier_runs(root: Path, previous: dict | None) -> None:
    """Keep the last run's logs for a look after a restart; earlier runs leave."""
    kept = Path(previous["run"]).name if previous is not None else None
    for earlier in root.glob("run-*"):
        if earlier.name == kept:
            continue
        if earlier.is_symlink():
            earlier.unlink()
        elif earlier.is_dir():
            shutil.rmtree(earlier)


def verify(root: Path) -> dict:
    config = instance(root)
    if starts_directly(config):
        from . import direct

        return direct.verify(root, config)
    with locked(root):
        state = _read_state(root)
        if state.get("phase") not in {"launched", "ready", "degraded"}:
            raise ValueError("dev verify requires a launched instance")
        run = Path(state["run"])
        state["phase"] = "degraded" if state.get("verification_failure") else "launched"
        state.pop("verification", None)
        state.pop("verified_at", None)
        write(root / "lifecycle.json", state)
        evidence = run / f"verify-{uuid.uuid4().hex[:12]}"
        evidence.mkdir(mode=0o700)
        try:
            with memory_samples(run, instance_gpus(config), "verify"):
                _run(
                    evidence,
                    "narwhal.diagnostics.check.cli",
                    [
                        "--fleet",
                        str(run / "fleet.json"),
                        "--evidence-out",
                        str(evidence / "kv.json"),
                    ],
                    "preflight",
                )
                with httpx.Client(timeout=30, trust_env=False) as client:
                    fleet = read(run / "fleet.json")
                    payload = {
                        "model": fleet["model"],
                        "temperature": 0,
                        "max_tokens": 32,
                        "messages": [
                            {"role": "user", "content": "Reply with only the number: 2 + 3 = ?"}
                        ],
                    }
                    response = client.post(
                        config["router_url"] + "/v1/chat/completions", json=payload
                    )
                    response.raise_for_status()
                    result = response.json()
                    completion = evidence / "completion.json"
                    write(completion, result)
                    try:
                        content = result["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise LifecycleOperationError(
                            "routed completion requires choices[0].message.content as a string; "
                            f"inspect {completion}"
                        ) from exc
                    if not isinstance(content, str):
                        raise LifecycleOperationError(
                            "routed completion requires choices[0].message.content as a string; "
                            f"inspect {completion}"
                        )
                    if content.strip() != "5":
                        raise LifecycleOperationError(
                            "routed arithmetic canary expected 5; inspect completion.json"
                        )
                    # Each engine serves its metrics on its backend's route.
                    engine_metrics = load_backend(
                        fleet.get("engine", {}).get("backend", DEFAULT_BACKEND)
                    ).metrics.path
                    for name, url in [
                        ("router", config["router_url"] + "/metrics"),
                        *[(e["iid"], e["url"] + engine_metrics) for e in fleet["engines"]],
                    ]:
                        metrics = client.get(url)
                        metrics.raise_for_status()
                        (evidence / f"{name}-metrics.txt").write_text(metrics.text)
                    client.get(config["router_url"] + "/ready").raise_for_status()
            state.pop("failure", None)
            state.pop("failed_verification", None)
            state.update(phase="ready", verification=str(evidence), verified_at=time.time())
            state.pop("verification_failure", None)
            write(root / "lifecycle.json", state)
        except BaseException as error:
            state["failed_verification"] = str(evidence)
            state["failure"] = {
                "message": str(error) or type(error).__name__,
                "stage": getattr(error, "stage", "verification"),
                "context": getattr(error, "context", {}),
            }
            failure = {
                "reason": state["failure"]["message"],
                "evidence": str(evidence),
                "failed_at": time.time(),
            }
            state.update(phase="degraded", verification_failure=failure)
            write(root / "lifecycle.json", state)
            write(evidence / "failure.json", failure)
            raise
    return status(root)


def status(root: Path) -> dict:
    config = instance(root)
    if not (root / "lifecycle.json").exists():
        return {"status": "stopped"}
    state = _read_state(root)
    run = Path(state["run"])
    if state["phase"] == "starting" and _busy(root):
        return {"status": "starting", "run": str(run)}
    records = _engine_records(run, every_attempt=False) + list(state["processes"])
    live = [r["name"] for r in records if native_engine.owns_process(r["identity"])]
    survivors = {
        r["name"]: sorted(members)
        for r in records
        if r["name"] not in live and (members := native_engine.group_members(r["identity"]))
    }
    helpers = {
        str(path.relative_to(run)): sorted(members)
        for path, record in _helper_records(run)
        if (members := stages.active(record))
    }
    # A failed up whose stages left nothing running holds no fleet until it resumes.
    if state["phase"] in ("stopped", "failed") and not live and not survivors and not helpers:
        return {"status": "stopped", "run": str(run)}
    problems = [r["name"] + " process identity expired" for r in records if r["name"] not in live]
    problems.extend(
        f"{name}: surviving group PIDs {pids} require operator inspection"
        for name, pids in survivors.items()
    )
    problems.extend(f"{name}: interrupted helper PIDs {pids}" for name, pids in helpers.items())
    if state.get("failure") and not state.get("verification_failure"):
        problems.append(state["failure"]["message"])
    direct = starts_directly(config)
    if len(live) != (1 if direct else 2) * config["engine_count"] + 1:
        problems.append(
            "owned process count differs from the configured engines and router"
            if direct
            else "owned process count differs from the configured engines, sidecars and router"
        )
    fleet_path = run / "fleet.json"
    if not fleet_path.exists():
        return {
            "status": "degraded",
            "run": str(run),
            "processes": live,
            "surviving_processes": survivors,
            "stage_processes": helpers,
            "problems": problems,
        }
    fleet = read(fleet_path)
    with httpx.Client(timeout=2, trust_env=False) as client:
        for name, url in [
            ("router", config["router_url"]),
            *[(e["iid"], e["url"]) for e in fleet["engines"]],
        ]:
            try:
                client.get(url + "/health").raise_for_status()
            except httpx.HTTPError as exc:
                problems.append(f"{name} health: {exc}")
        try:
            client.get(config["router_url"] + "/ready").raise_for_status()
        except httpx.HTTPError as exc:
            problems.append(f"router readiness: {exc}")
    failure = state.get("verification_failure")
    if failure:
        problems.append(f"verification failed: {failure['reason']}")
    evidence = state.get("verification")
    if evidence and not direct:
        problems.extend(
            asyncio.run(
                verify_directed_kv_evidence(
                    FleetConfig.load(fleet_path), fleet_path, Path(evidence) / "kv.json"
                )
            )
        )
    phase = "degraded" if problems else ("ready" if evidence else "launched")
    return {
        "status": phase,
        "run": str(run),
        "router": config["router_url"],
        "processes": live,
        "surviving_processes": survivors,
        "stage_processes": helpers,
        "problems": problems,
        **({"verification_failure": failure} if failure else {}),
    }


def down(root: Path) -> dict:
    instance(root)
    with locked(root):
        if (root / "lifecycle.json").exists():
            _stop(root, _read_state(root))
    return status(root)


def remove(root: Path) -> dict:
    """Stop the instance's fleet and delete its directory, downloads and logs included."""
    down(root)
    if not (root / "instance.json").is_file():
        raise ValueError(f"{root} holds no instance")
    shutil.rmtree(root)
    return {"status": "removed", "instance": str(root)}
