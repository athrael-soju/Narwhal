"""Synthetic lifecycle controller and workers for the Linux interruption matrix."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.deployment import native_engine, stages
from narwhal.dev import lifecycle

MODULE = "tests.deployment.interruption_harness"


def register(registry: Path) -> None:
    pid = os.getpid()
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    stages.write_evidence(registry / f"pid-{pid}.json", {"pid": pid, "start_ticks": int(stat[19])})


def barrier(root: Path, name: str) -> None:
    stages.write_evidence(root / "barrier.json", {"name": name, "controller": os.getpid()})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.01)
    raise TimeoutError(f"harness barrier {name} expired")


def worker(registry: Path, role: str) -> None:
    register(registry)
    if role in {"delayed-worker", "detached-worker"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(f"retained {role} {os.getpid()}", flush=True)
    if role in {"service", "helper"}:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                MODULE,
                "detached-worker" if role == "helper" else "delayed-worker",
                str(registry),
            ],
            start_new_session=role == "helper",
        )
    time.sleep(15)


def controller(root: Path, selected: str) -> None:
    registry = root / "registry"
    register(registry)
    original_identity = native_engine.process_identity
    original_write = lifecycle.write
    original_replace = Path.replace
    selected_barrier = False

    def child_identity(pid):
        nonlocal selected_barrier
        if selected == "child-created" and not selected_barrier:
            selected_barrier = True
            wait_workers(registry, 2)
            barrier(root, selected)
        return original_identity(pid)

    def write(path, value):
        nonlocal selected_barrier
        original_write(path, value)
        if (
            selected == "owned"
            and path.name == "lifecycle.json"
            and value.get("processes")
            and not selected_barrier
        ):
            selected_barrier = True
            wait_workers(registry, 2)
            barrier(root, selected)

    def replace(path, target):
        nonlocal selected_barrier
        if (
            selected == "ownership-pending"
            and Path(target).name == "lifecycle.json"
            and json.loads(path.read_text()).get("processes")
            and not selected_barrier
        ):
            selected_barrier = True
            wait_workers(registry, 2)
            barrier(root, selected)
        return original_replace(path, target)

    def run_stage(path, module, args, log):
        command = [sys.executable, "-m", MODULE, "helper", str(registry)]
        lifecycle.write(path / f"{log}.command.json", {"argv": command})
        stages.run(command, stage=log, log=path / f"{log}.log")

    def launch(instance, run, config, spec, state):
        lifecycle.write(run / "fleet.json", lifecycle.read(root / "fleet.json"))
        record = lifecycle._spawn(
            root, state, MODULE, ["service", str(registry)], "router", dict(os.environ)
        )
        wait_workers(registry, 2)
        if selected == "readiness":
            with patch.object(
                lifecycle.httpx, "Client", side_effect=lambda **kwargs: barrier(root, selected)
            ):
                lifecycle._wait(config["router_url"] + "/health", record["identity"])
        elif selected == "exited-leader":
            os.kill(record["identity"]["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 2
            while native_engine._owns_process(record["identity"]) and time.monotonic() < deadline:
                time.sleep(0.01)
            barrier(root, selected)
        elif selected == "profiling":
            lifecycle._profiles(run, lifecycle.read(run / "fleet.json"), spec)

    terminate = native_engine._terminate
    with (
        patch.object(lifecycle, "_launch", side_effect=launch),
        patch.object(lifecycle, "_run", side_effect=run_stage),
        patch.object(lifecycle, "check_plugin"),
        patch.object(lifecycle, "_check_free_ports"),
        patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
        patch.object(native_engine, "process_identity", side_effect=child_identity),
        patch.object(
            native_engine, "_terminate", side_effect=lambda identity: terminate(identity, 0.05)
        ),
        patch.object(lifecycle, "write", side_effect=write),
        patch.object(Path, "replace", replace),
    ):
        lifecycle.up(root)
        if selected == "verification":
            lifecycle.verify(root)


def wait_workers(registry: Path, count: int) -> None:
    deadline = time.monotonic() + 4
    # The controller writes its own identity before creating the synthetic service.
    while len(list(registry.glob("pid-*.json"))) < count + 1:
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic workers did not register")
        time.sleep(0.01)


def inspect(root: Path, operation: str) -> None:
    terminate = native_engine._terminate
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.Client(transport=transport)
    with (
        patch.object(lifecycle.httpx, "Client", return_value=client),
        patch.object(
            native_engine, "_terminate", side_effect=lambda identity: terminate(identity, 0.05)
        ),
    ):
        busy = lifecycle._busy(root)
        try:
            result = getattr(lifecycle, operation)(root)
        except ValueError as error:
            print(json.dumps({"busy": busy, "error": str(error)}))
        else:
            print(json.dumps({"busy": busy, "result": result}))


def main() -> None:
    role, path, *rest = sys.argv[1:]
    if role == "controller":
        controller(Path(path), rest[0])
    elif role in {"status", "down"}:
        inspect(Path(path), role)
    else:
        worker(Path(path), role)


if __name__ == "__main__":
    main()
