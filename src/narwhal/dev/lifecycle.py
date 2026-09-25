"""Own local engine generations and qualify them through the fleet commands."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from narwhal.config import FleetConfig
from narwhal.deployment import cache_capture_hook, native_engine, stages
from narwhal.deployment.attestation_contract import finalize_fleet
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine import digest, gpu_memory, prepare
from narwhal.diagnostics.check import verify_directed_kv_evidence

from .template import _check_free_ports, _port_layout, _sha256, check_plugin


def read(path: Path) -> dict:
    """Read an instance document as an object."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def write(path: Path, value: dict) -> None:
    """Replace a private state document atomically."""
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
    """Require the supported local configuration and its original Python environment."""
    value = read(root / "instance.json")
    if value.get("schema") != "narwhal.dev-instance" or value.get("schema_version") != 1:
        raise ValueError("instance requires narwhal.dev-instance schema version 1")
    expected = Path(value["python_executable"])
    current = Path(sys.executable)
    if expected.parent.resolve() != current.parent.resolve() or not expected.samefile(current):
        raise ValueError("run this instance with the Python environment used by dev init")
    return value


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    """Serialize lifecycle mutations while status reads the last atomic state."""
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


def _run(root: Path, module: str, args: list[str], log: str) -> None:
    command = [sys.executable, "-m", module, *args]
    write(root / f"{log}.command.json", {"argv": command})
    result = stages.run(
        command,
        stage=log,
        log=root / f"{log}.log",
        cwd=root,
        retain_descendants=log == "native-start-shared",
    )
    if result.returncode:
        detail = (root / f"{log}.log").read_text(errors="replace")[-3000:]
        raise ValueError(f"{log} exited {result.returncode}: {detail}")


def _spawn(root: Path, state: dict, module: str, args: list[str], name: str, env: dict) -> dict:
    run = Path(state["run"])
    command = [sys.executable, "-m", module, *args]
    write(run / f"{name}.command.json", {"argv": command})
    with (run / f"{name}.log").open("w") as output:
        child = subprocess.Popen(  # noqa: S603 - installed modules and explicit argv
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=run,
            env=env,
        )
    try:
        identity = native_engine.process_identity(child.pid)
    except BaseException:
        child.terminate()
        child.wait()
        raise
    record = {"name": name, "identity": identity}
    state["processes"].append(record)
    write(root / "lifecycle.json", state)
    return record


def _wait(url: str, identity: dict, seconds: int = 30) -> None:
    deadline = time.monotonic() + seconds
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if not native_engine._owns_process(identity):
                raise ValueError(f"process exited before {url} became healthy; inspect its log")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
    raise ValueError(f"health deadline expired: {url}")


@contextlib.contextmanager
def memory_samples(run: Path, gpu: str, name: str) -> Iterator[None]:
    """Retain whole-device VRAM during startup, profiling and routed checks."""
    stopped = threading.Event()

    def sample() -> None:
        with (run / f"{name}-memory.jsonl").open("a") as output:
            while not stopped.is_set():
                try:
                    row: dict[str, object] = {"time": time.time(), **gpu_memory(gpu)}
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    row = {"time": time.time(), "error": str(exc)}
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


def _profiles(run: Path, fleet: dict, spec: dict) -> None:
    count = len(fleet["engines"])
    sources = []
    for prefill in range(1, count):
        measured = json.loads(json.dumps(fleet))
        for index, engine in enumerate(measured["engines"]):
            engine["role"] = "prefill" if index < prefill else "decode"
        profile = run / f"profiles-{prefill}p{count - prefill}d.json"
        measured["profiles"]["path"] = str(profile)
        fleet_path = run / f"profile-{prefill}p{count - prefill}d.fleet.json"
        write(fleet_path, measured)
        args = ["--fleet", str(fleet_path), "--colocated"]
        for key, value in spec["profile"].items():
            rendered = ",".join(map(str, value)) if isinstance(value, list) else str(value)
            args.extend(["--" + key.replace("_", "-"), rendered])
        print(
            f"profiling {prefill} prefill / {count - prefill} decode", file=sys.stderr, flush=True
        )
        _run(run, "narwhal.profiling.probe", args, f"profile-{prefill}p{count - prefill}d")
        sources.append(profile)
    if len(sources) == 1:
        (run / "profiles.json").write_bytes(sources[0].read_bytes())
    else:
        args = ["--fleet", str(run / "fleet.json"), "--out", str(run / "profiles.json")]
        for source in sources:
            args.extend(["--merge", str(source)])
        _run(run, "narwhal.profiling.probe", args, "profile-merge")


def _stop(root: Path, state: dict) -> None:
    errors = []
    # The native launcher persists ownership before health checks, including failures.
    run = Path(state["run"])
    records = list(state.get("processes", []))
    for path in sorted(run.glob("engine-*/native-process.json")):
        records.insert(0, {"name": path.parent.name, "identity": read(path)})
    stopped = []
    for record in reversed(records):
        identity = record["identity"]
        if native_engine._group_members(identity):
            try:
                native_engine._terminate(identity)
                stopped.append(record["name"])
            except (OSError, ValueError) as exc:
                errors.append(f"{record['name']}: {exc}")
    state.update(phase="degraded" if errors else "stopped", stopped=stopped, errors=errors)
    write(root / "lifecycle.json", state)
    write(run / "teardown.json", {"time": time.time(), "stopped": stopped, "errors": errors})
    if errors:
        raise ValueError("; ".join(errors))


def up(root: Path) -> dict:
    """Launch a fresh owned generation, profile it and start its router."""
    config = instance(root)
    with locked(root):
        if (root / "lifecycle.json").exists():
            previous = read(root / "lifecycle.json")
            if previous.get("phase") != "stopped":
                raise ValueError("run dev down before starting another generation")
        spec = read(root / "template.json")
        check_plugin(spec["runtime"])
        if _sha256(Path(config["model_path"])) != spec["model"]["sha256"]:
            raise ValueError("GGUF model changed after dev init")
        for name, expected in spec["model"].get("tokenizer_sha256", {}).items():
            if _sha256(Path(config["model_dir"]) / name) != expected:
                raise ValueError(f"tokenizer file {name} changed after dev init")
        _, ports = _port_layout(spec, config["engine_count"])
        _check_free_ports(ports, "127.0.0.1")
        run = root / f"run-{uuid.uuid4().hex[:12]}"
        run.mkdir(mode=0o700)
        state: dict = {"schema_version": 1, "phase": "starting", "run": str(run), "processes": []}
        write(root / "lifecycle.json", state)
        try:
            with memory_samples(run, config["gpu_uuid"], "up"):
                _launch(root, run, config, spec, state)
        except BaseException as error:
            state["failure"] = {
                "message": str(error),
                "stage": getattr(error, "stage", "startup"),
                "context": getattr(error, "context", {}),
            }
            try:
                _stop(root, state)
            except (OSError, ValueError) as cleanup_error:
                state["failure"]["cleanup_error"] = str(cleanup_error)
                write(root / "lifecycle.json", state)
            raise
        state["phase"] = "launched"
        write(root / "lifecycle.json", state)
        return {"status": "launched", "run": str(run), "router": config["router_url"]}


def _launch(root: Path, run: Path, config: dict, spec: dict, state: dict) -> None:
    fleet = read(root / "fleet.json")
    fleet.pop("engine_contract", None)
    settings = fleet.setdefault("engine", {})
    if not settings.get("engine_api_key_env") and os.environ.get("NARWHAL_ENGINE_API_KEY"):
        settings["engine_api_key_env"] = "NARWHAL_ENGINE_API_KEY"
    fleet["profiles"]["path"] = str(run / "profiles.json")
    fleet["recovery"] = {"state_path": str(run / "router-state.json")}
    write(run / "fleet.json", fleet)
    engine_key = FleetConfig.load(run / "fleet.json").resolve_engine_key()
    allocation = read(root / "engine-launch.json")
    hook = Path(cache_capture_hook.__file__)
    runs = []
    for index, engine in enumerate(fleet["engines"]):
        number = index + 1
        name = f"engine-{number}"
        selected = run / f"{name}.selected.json"
        write(selected, selected_launch(allocation, name, {}))
        env = {
            **os.environ,
            "NARWHAL_ENGINE_LAUNCH_CONFIG": str(selected),
            "NARWHAL_MODEL_DIR": config["model_dir"],
            "NARWHAL_MODEL_PATH": config["model_path"],
            "NARWHAL_MODEL_REVISION": config["model_revision"],
            "NARWHAL_MODEL_CONFIG_SHA256": config["model_config_sha256"],
            "NARWHAL_CACHE_CAPTURE_HOOK": str(hook),
            "NARWHAL_CACHE_CAPTURE_HOOK_SHA256": digest(hook),
            "NARWHAL_ENGINE_PORT": str(config["ports"]["engine_first"] + index),
            "NARWHAL_ATTEST_PORT": str(config["ports"]["attestation_first"] + index),
            "NARWHAL_NIXL_SIDE_CHANNEL_PORT": str(config["ports"]["nixl_first"] + index),
            "NARWHAL_UCX_TCP_PORT_RANGE": config["ucx_range"],
            f"NARWHAL_NODE_{number}_IP": config["fabric_address"],
            f"NARWHAL_NODE_{number}_URL": engine["url"],
            "NARWHAL_ENGINE_MODEL_NAME": fleet["model"],
            "NARWHAL_ENGINE_API_KEY": engine_key or "",
            "NARWHAL_DEPLOYMENT_REVISION": digest(root / "template.json"),
        }
        engine_run = run / name
        prepare(engine_run, env, backend="native")
        _run(run, "narwhal.deployment.launch_engine", ["check", "--run", str(engine_run)], name)
        runs.append(engine_run)
    _run(
        run,
        "narwhal.deployment.launch_engine",
        [
            "start-shared",
            "--backend",
            "native",
            *[arg for path in runs for arg in ("--run", str(path))],
        ],
        "native-start-shared",
    )
    for index, engine_run in enumerate(runs):
        _run(
            run,
            "narwhal.deployment.attestation_contract",
            ["native-capture", "--run", str(engine_run)],
            f"attest-{index + 1}",
        )
        url = fleet["engines"][index]["attestation_url"]
        record = _spawn(
            root,
            state,
            "narwhal.deployment.attestation_contract",
            ["serve", "--run", str(engine_run)],
            f"sidecar-{index + 1}",
            {**os.environ, f"NARWHAL_NODE_{index + 1}_ATTESTATION_URL": url},
        )
        _wait(url, record["identity"])
    finalize_fleet(run / "fleet.json")
    fleet = read(run / "fleet.json")
    _profiles(run, fleet, spec)
    router = urlsplit(config["router_url"])
    record = _spawn(
        root,
        state,
        "narwhal.cli",
        [
            "--fleet",
            str(run / "fleet.json"),
            "--host",
            str(router.hostname),
            "--port",
            str(router.port),
            "--journal",
            str(run / "journal.jsonl"),
        ],
        "router",
        dict(os.environ),
    )
    _wait(config["router_url"] + "/health", record["identity"])


def verify(root: Path) -> dict:
    """Run current-process preflight, all eligible KV paths and a routed completion."""
    config = instance(root)
    with locked(root):
        state = read(root / "lifecycle.json")
        if state.get("phase") not in {"launched", "ready", "degraded"}:
            raise ValueError("dev verify requires a launched instance")
        run = Path(state["run"])
        state["phase"] = "launched"
        state.pop("verification", None)
        write(root / "lifecycle.json", state)
        evidence = run / f"verify-{uuid.uuid4().hex[:12]}"
        evidence.mkdir(mode=0o700)
        try:
            with memory_samples(run, config["gpu_uuid"], "verify"):
                _run(
                    evidence,
                    "narwhal.diagnostics.check",
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
                    write(evidence / "completion.json", result)
                    if result["choices"][0]["message"]["content"].strip() != "5":
                        raise ValueError(
                            "routed arithmetic canary expected 5; inspect completion.json"
                        )
                    for name, url in [
                        ("router", config["router_url"]),
                        *[(e["iid"], e["url"]) for e in fleet["engines"]],
                    ]:
                        metrics = client.get(url + "/metrics")
                        metrics.raise_for_status()
                        (evidence / f"{name}-metrics.txt").write_text(metrics.text)
                    client.get(config["router_url"] + "/ready").raise_for_status()
            state.pop("failure", None)
            state.pop("failed_verification", None)
            state.update(phase="ready", verification=str(evidence), verified_at=time.time())
            write(root / "lifecycle.json", state)
        except BaseException as error:
            state["failed_verification"] = str(evidence)
            state["failure"] = {
                "message": str(error),
                "stage": getattr(error, "stage", "verification"),
                "context": getattr(error, "context", {}),
            }
            state["phase"] = "degraded"
            write(root / "lifecycle.json", state)
            raise
    return status(root)


def status(root: Path) -> dict:
    """Derive readiness from current owned processes, HTTP health and saved KV evidence."""
    config = instance(root)
    if not (root / "lifecycle.json").exists():
        return {"status": "stopped"}
    state = read(root / "lifecycle.json")
    run = Path(state["run"])
    if state["phase"] == "starting" and _busy(root):
        return {"status": "starting", "run": str(run)}
    records = list(state["processes"])
    records.extend(
        {"name": path.parent.name, "identity": read(path)}
        for path in sorted(run.glob("engine-*/native-process.json"))
    )
    live = [r["name"] for r in records if native_engine._owns_process(r["identity"])]
    survivors = {
        r["name"]: sorted(members)
        for r in records
        if r["name"] not in live and (members := native_engine._group_members(r["identity"]))
    }
    if state["phase"] == "stopped" and not live and not survivors:
        return {"status": "stopped", "run": str(run)}
    problems = [r["name"] + " process identity expired" for r in records if r["name"] not in live]
    problems.extend(
        f"{name}: surviving group PIDs {pids} require operator inspection"
        for name, pids in survivors.items()
    )
    if state.get("failure"):
        problems.append(state["failure"]["message"])
    if len(live) != 2 * config["engine_count"] + 1:
        problems.append(
            "owned process count differs from the configured engines, sidecars and router"
        )
    fleet_path = run / "fleet.json"
    if not fleet_path.exists():
        return {
            "status": "degraded",
            "run": str(run),
            "processes": live,
            "surviving_processes": survivors,
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
    if evidence := state.get("verification"):
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
        "problems": problems,
    }


def down(root: Path) -> dict:
    """Stop the selected instance's matching process groups and retain its measurements."""
    instance(root)
    with locked(root):
        if (root / "lifecycle.json").exists():
            _stop(root, read(root / "lifecycle.json"))
    return status(root)
