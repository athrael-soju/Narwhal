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

from narwhal.backends.vllm import dev as vllm_dev
from narwhal.deployment import native_engine, stages
from narwhal.deployment.runner import Outcome
from narwhal.dev import launch, lifecycle

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

    def fleet(context):
        lifecycle.write(context.directory / "fleet.json", lifecycle.read(root / "fleet.json"))

    def profile(self, context):
        fleet(context)
        if selected == "profiling":
            launch._profiles(context.directory, lifecycle.read(root / "fleet.json"), self.spec)

    def start_router(self, context):
        # The router stage of a launched fleet, with a synthetic service as its router.
        url = self.config["router_url"]
        argv = [sys.executable, "-m", MODULE, "service", str(registry)]
        if selected == "readiness":
            with patch.object(
                launch.httpx,
                "Client",
                side_effect=lambda **kwargs: (wait_workers(registry, 2), barrier(root, selected)),
            ):
                outcome = self._serve(context, "router", argv, {}, url, [(url + "/health", 30)])
        else:
            outcome = self._serve(context, "router", argv, {}, url, [])
        wait_workers(registry, 2)
        if selected == "exited-leader":
            identity = outcome.processes[0]
            os.kill(identity["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 2
            while native_engine.owns_process(dict(identity)) and time.monotonic() < deadline:
                time.sleep(0.01)
            barrier(root, selected)
        return Outcome({"router_identity": dict(outcome.processes[0])}, outcome.processes)

    # The stages before the router's pass at once; the router and profiling run their helpers.
    stubs = {
        "package": lambda self, context: fleet(context),
        "prepare_engine": lambda self, context: Outcome(
            {"launch": {"directory": context.instance, "plan_sha256": "synthetic"}}
        ),
        "check_engine": lambda self, context: None,
        "start_engines": lambda self, context: Outcome({"engine_identity": {}}),
        "capture_attestation": lambda self, context: Outcome(
            {"binding": "synthetic", "engine_contract": {}}
        ),
        "start_sidecar": lambda self, context: Outcome({"sidecar_identity": {}}),
        "finalize": lambda self, context: fleet(context),
        "profile": profile,
        "start_router": start_router,
    }
    terminate = native_engine.terminate
    with contextlib.ExitStack() as stack:
        for name, stub in stubs.items():
            stack.enter_context(patch.object(launch._Launch, name, stub))
        for patched in (
            patch.object(lifecycle, "_run", side_effect=run_stage),
            patch.object(vllm_dev, "check_plugin"),
            patch.object(lifecycle, "_check_free_ports"),
            patch.object(launch, "_check_free_ports"),
            patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
            patch.object(native_engine, "process_identity", side_effect=child_identity),
            patch.object(
                native_engine,
                "terminate",
                side_effect=lambda identity, grace=10.0: terminate(identity, 0.05),
            ),
            patch.object(lifecycle, "write", side_effect=write),
            patch.object(Path, "replace", replace),
        ):
            stack.enter_context(patched)
        lifecycle.up(root)
        if selected == "verification":
            lifecycle.verify(root)


def wait_workers(registry: Path, count: int) -> None:
    deadline = time.monotonic() + 4
    # The registry also holds the controller's own identity.
    while len(list(registry.glob("pid-*.json"))) < count + 1:
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic workers did not register")
        time.sleep(0.01)


def inspect(root: Path, operation: str) -> None:
    terminate = native_engine.terminate
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.Client(transport=transport)
    with (
        patch.object(lifecycle.httpx, "Client", return_value=client),
        patch.object(
            native_engine,
            "terminate",
            side_effect=lambda identity, grace=10.0: terminate(identity, 0.05),
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
