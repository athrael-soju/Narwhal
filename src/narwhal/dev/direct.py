"""A development fleet whose engines start as plain processes and take nominal profiles."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from narwhal.backends import load as load_backend
from narwhal.config.loading import load as load_fleet
from narwhal.deployment.direct_start import DirectEngine, DirectStart, NominalFigures
from narwhal.deployment.launch_engine.runtime import write_private
from narwhal.deployment.launch_engine.start import READY_SECONDS
from narwhal.profiling.model import nominal_profile
from narwhal.profiling.store import ProfileStore

from . import lifecycle
from .template import _check_free_ports, _sha256

HOST = "127.0.0.1"
PROBE_TIMEOUT_S = 120.0
READY_TIMEOUT_S = 60.0
MAX_THREADS = 8
VERIFY_BODY = {"prompt": "The capital of France is", "max_tokens": 8, "temperature": 0}


def _direct_start(spec: dict) -> DirectStart:
    return load_backend(spec["runtime"]["backend"]).direct_start()


def _ports(spec: dict, count: int) -> tuple[dict[str, int], set[int]]:
    if not 2 <= count <= 8:
        raise ValueError("a development fleet requires 2 to 8 engines")
    ports = {name: spec["ports"][name] for name in ("router", "engine_first")}
    router, first = ports["router"], ports["engine_first"]
    if type(router) is not int or not 1 <= router <= 65535:
        raise ValueError("router port is invalid")
    if type(first) is not int or not 1 <= first <= 65536 - count:
        raise ValueError(f"engine_first cannot fit {count} engine ports")
    used = {router, *range(first, first + count)}
    if len(used) != count + 1:
        raise ValueError(f"template ports collide at {router}")
    return ports, used


def _pinned(spec: dict) -> dict:
    # The sections a flag cannot change.
    return {name: spec.get(name) for name in ("model", "runtime", "slo", "controller")}


def _runtime(spec: dict) -> dict:
    runtime = spec["runtime"]
    for name in ("build", "context_tokens", "slots", "memory_mib"):
        if type(runtime.get(name)) is not int or runtime[name] < 1:
            raise ValueError(f"template runtime.{name} must be a positive integer")
    extra = runtime.get("extra_args", [])
    if not isinstance(extra, list) or not all(isinstance(arg, str) for arg in extra):
        raise ValueError("template runtime.extra_args must be a list of strings")
    return runtime


def _threads(count: int) -> int:
    # One CPU stays with the router, which polls every engine.
    usable = len(os.sched_getaffinity(0)) - 1
    return max(1, min(MAX_THREADS, usable // count))


def _available_mib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise ValueError("/proc/meminfo reports no MemAvailable")


def _model(spec: dict, model_path: Path) -> Path:
    model = spec["model"]
    path = model_path.expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"model path must name a copy of {model['filename']}")
    if _sha256(path) != model["sha256"]:
        raise ValueError("model file SHA-256 differs from the pinned model revision")
    return path


def _binary(path: Path) -> Path:
    binary = path.expanduser().absolute()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"engine binary must name an executable file: {binary}")
    return binary


def materialize(
    output: Path,
    spec: dict,
    *,
    existing: dict | None,
    saved: dict | None,
    engine_binary: Path | None,
    model_path: Path | None,
    engine_count: int | None,
    port_base: int | None,
    accelerated: bool | None,
    template_given: bool = False,
    prepared: bool = False,
    recorded: dict | None = None,
) -> Path:
    spec = copy.deepcopy(spec)
    if engine_count is not None:
        spec["allocation"]["engine_count"] = engine_count
    if port_base is not None:
        spec["ports"].update(router=port_base, engine_first=port_base + 1)
    count = spec["allocation"]["engine_count"]
    ports, used = _ports(spec, count)
    if existing is not None:
        conflicts = [
            flag
            for flag, changed in (
                (
                    "--engine-binary",
                    engine_binary is not None
                    and str(_binary(engine_binary)) != existing["engine_binary"],
                ),
                (
                    "--model",
                    model_path is not None
                    and str(model_path.expanduser().absolute()) != existing["model_path"],
                ),
                (
                    "--engine-count",
                    engine_count is not None and count != existing["engine_count"],
                ),
                ("--port-base", port_base is not None and ports != existing["ports"]),
                ("--no-accelerator", accelerated not in (None, existing["accelerated"])),
                ("--template", template_given and _pinned(spec) != _pinned(saved or {})),
            )
            if changed
        ]
        if conflicts:
            raise ValueError(
                f"existing instance {output} conflicts with {', '.join(conflicts)}; "
                "create a fresh instance with narwhal dev init --instance <new-directory> "
                "and the requested settings"
            )
        return output
    if engine_binary is None or model_path is None:
        raise ValueError("this template requires --engine-binary and --model")
    binary = _binary(engine_binary)
    runtime = _runtime(spec)
    version = _direct_start(spec).check(binary, runtime)
    model = _model(spec, model_path)
    # With the model on an accelerator its weights are in device memory, which this host
    # figure does not measure.
    required, available = count * runtime["memory_mib"], _available_mib()
    if accelerated is False and available < required:
        raise ValueError(
            f"host memory check failed: {available} MiB available, {required} MiB required"
        )
    _check_free_ports(used, HOST)
    fleet: dict[str, Any] = {
        "schema": "narwhal.fleet",
        "schema_version": 2,
        "model": spec["model"]["served_name"],
        "hardware": {
            "accelerator": "local",
            "accelerators_per_engine": 1,
            "tensor_parallel": 1,
        },
        "engines": [
            {
                "iid": f"n{index + 1}",
                "url": f"http://{HOST}:{ports['engine_first'] + index}",
                "role": "prefill" if index < count // 2 else "decode",
            }
            for index in range(count)
        ],
        "slo": spec["slo"],
        "controller": spec["controller"],
        "profiles": {"path": str(output / "profiles.json")},
        "recovery": {"state_path": str(output / "router-state.json")},
        "engine": {"backend": runtime["backend"], "first_token_timeout_s": 10.0},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="narwhal-dev-check-") as temporary:
        trial = Path(temporary) / "fleet.json"
        trial.write_text(json.dumps(fleet))
        load_fleet(trial)
    output.mkdir(mode=0o700, exist_ok=prepared)
    written: list[str] = []
    try:
        for name, document in (("template.json", spec), ("fleet.json", fleet)):
            write_private(output / name, json.dumps(document, indent=2) + "\n")
            written.append(name)
        written.append("instance.json")
        instance = {
            "schema": "narwhal.dev-instance",
            "schema_version": 1,
            "start": "direct",
            "backend": runtime["backend"],
            "engine_binary": str(binary),
            "engine_version": version,
            "model_path": str(model),
            "router_url": f"http://{HOST}:{ports['router']}",
            "ports": ports,
            "engine_count": count,
            "accelerated": accelerated is not False,
            "narwhal_version": metadata.version("narwhal-inference"),
            "python_executable": sys.executable,
            # Facts the caller keeps with the instance, such as the engine build it downloaded.
            **(recorded or {}),
        }
        write_private(output / "instance.json", json.dumps(instance, indent=2) + "\n")
    except BaseException:
        if prepared:
            # write_private refuses a file that exists, so each of these is this call's own.
            for name in written:
                (output / name).unlink(missing_ok=True)
        else:
            shutil.rmtree(output, ignore_errors=True)
        raise
    return output


def up(root: Path, config: dict) -> dict:
    with lifecycle.locked(root):
        if (root / "lifecycle.json").exists():
            previous = lifecycle._read_state(root)
            if previous.get("phase") != "stopped":
                raise ValueError("run dev down before starting another generation")
        spec = lifecycle.read(root / "template.json")
        start = _direct_start(spec)
        binary = _binary(Path(config["engine_binary"]))
        if start.check(binary, spec["runtime"]) != config["engine_version"]:
            raise ValueError("engine binary changed after dev init")
        _model(spec, Path(config["model_path"]))
        _check_free_ports(_ports(spec, config["engine_count"])[1], HOST)
        run = root / f"run-{uuid.uuid4().hex[:12]}"
        run.mkdir(mode=0o700)
        state: dict = {"schema_version": 1, "phase": "starting", "run": str(run), "processes": []}
        lifecycle.write(root / "lifecycle.json", state)
        try:
            _launch(root, run, config, spec, state, start, binary)
        except BaseException as error:
            state["failure"] = {"message": str(error), "stage": "startup", "context": {}}
            try:
                lifecycle._stop(root, state)
            except (OSError, ValueError) as cleanup_error:
                state["failure"]["cleanup_error"] = str(cleanup_error)
                lifecycle.write(root / "lifecycle.json", state)
            raise
        state["phase"] = "launched"
        lifecycle.write(root / "lifecycle.json", state)
        return {"status": "launched", "run": str(run), "router": config["router_url"]}


def _launch(
    root: Path,
    run: Path,
    config: dict,
    spec: dict,
    state: dict,
    start: DirectStart,
    binary: Path,
) -> None:
    fleet = lifecycle.read(root / "fleet.json")
    fleet["profiles"]["path"] = str(run / "profiles.json")
    fleet["recovery"] = {"state_path": str(run / "router-state.json")}
    lifecycle.write(run / "fleet.json", fleet)
    shared = run / "slot-files"
    shared.mkdir(mode=0o700)
    threads = _threads(config["engine_count"])
    environment = {**os.environ, **start.environment(binary)}
    records = []
    for index, engine in enumerate(fleet["engines"]):
        port = urlsplit(engine["url"]).port
        if port is None:
            raise ValueError(f"engine url names no port: {engine['url']}")
        command = start.command(
            binary,
            DirectEngine(
                model=Path(config["model_path"]),
                served_name=fleet["model"],
                host=HOST,
                port=port,
                runtime=spec["runtime"],
                shared_dir=shared,
                accelerated=config["accelerated"],
                threads=threads,
            ),
        )
        records.append(
            lifecycle._spawn_command(root, state, command, f"engine-{index + 1}", environment)
        )
    for engine, record in zip(fleet["engines"], records, strict=True):
        lifecycle._wait(engine["url"] + "/health", record["identity"], READY_SECONDS)
    store = ProfileStore(run / "profiles.json")
    for iid, figures in _measure(start, fleet, spec["runtime"]).items():
        store.put(
            nominal_profile(
                iid,
                prefill_token_s=figures.prefill_token_s,
                token_interval_s=figures.token_interval_s,
                kv_capacity_tokens=figures.kv_capacity_tokens,
                max_requests=figures.max_requests,
            )
        )
    router = urlsplit(config["router_url"])
    record = lifecycle._spawn(
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
    lifecycle._wait(config["router_url"] + "/health", record["identity"])
    lifecycle._wait(config["router_url"] + "/ready", record["identity"], int(READY_TIMEOUT_S))


def _measure(start: DirectStart, fleet: dict, runtime: dict) -> dict[str, NominalFigures]:
    """Each engine's figures with every engine serving its full number of requests.

    The engines share one device, so an engine measured alone reports a speed it loses as
    soon as its neighbours serve.
    """
    endpoint, body = start.probe(fleet["model"])
    each = start.probe_requests(runtime)
    engines = [engine for engine in fleet["engines"] for _ in range(each)]

    def one(engine: dict) -> NominalFigures:
        reply = client.post(engine["url"] + endpoint, json=body)
        reply.raise_for_status()
        return start.figures(reply.json(), runtime)

    with (
        httpx.Client(
            timeout=PROBE_TIMEOUT_S,
            trust_env=False,
            limits=httpx.Limits(max_connections=len(engines)),
        ) as client,
        ThreadPoolExecutor(max_workers=len(engines)) as pool,
    ):
        measured = list(pool.map(one, engines))
    figures = {}
    for index, engine in enumerate(fleet["engines"]):
        own = measured[index * each : (index + 1) * each]
        figures[engine["iid"]] = replace(
            own[0],
            prefill_token_s=sum(f.prefill_token_s for f in own) / each,
            token_interval_s=sum(f.token_interval_s for f in own) / each,
        )
    return figures


def verify(root: Path, config: dict) -> dict:
    with lifecycle.locked(root):
        state = lifecycle._read_state(root)
        if state["phase"] not in {"launched", "ready", "degraded"}:
            raise ValueError("dev verify requires a launched instance")
        run = Path(state["run"])
        state["phase"] = "degraded" if state.get("verification_failure") else "launched"
        state.pop("verification", None)
        lifecycle.write(root / "lifecycle.json", state)
        evidence = run / f"verify-{uuid.uuid4().hex[:12]}"
        evidence.mkdir(mode=0o700)
        try:
            fleet = lifecycle.read(run / "fleet.json")
            started = time.monotonic()
            with httpx.Client(timeout=PROBE_TIMEOUT_S, trust_env=False) as client:
                client.get(config["router_url"] + "/ready").raise_for_status()
                reply = client.post(
                    config["router_url"] + "/v1/completions",
                    json={"model": fleet["model"], **VERIFY_BODY},
                )
                reply.raise_for_status()
            choices = reply.json().get("choices")
            text = choices[0].get("text") if isinstance(choices, list) and choices else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError("the routed request returned no text")
            record = {"reply": text, "seconds": time.monotonic() - started, "time": time.time()}
            lifecycle.write(evidence / "routed.json", record)
        except BaseException as error:
            state["verification_failure"] = {"reason": str(error)}
            state.update(phase="degraded", failed_verification=str(evidence))
            lifecycle.write(root / "lifecycle.json", state)
            raise
        state.pop("verification_failure", None)
        state.pop("failed_verification", None)
        state.update(phase="ready", verification=str(evidence))
        lifecycle.write(root / "lifecycle.json", state)
    return {**lifecycle.status(root), "reply": text}
