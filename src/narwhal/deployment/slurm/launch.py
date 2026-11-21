"""Start a fleet inside one Slurm allocation: engines on every node, the router on the head."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx

from ...backends import DEFAULT_BACKEND
from ...config.loading import load as load_fleet
from ..allocation_start import AllocationEngine, AllocationStart
from .allocation import Allocation

POLL_S = 1.0


@dataclass(frozen=True)
class Settings:
    model: str
    served_name: str
    # Shared directory; each allocation writes into `<run_root>/slurm-<job>`.
    run_root: Path
    slo_ttft_s: float
    slo_tpot_s: float
    backend: str = DEFAULT_BACKEND
    engines_per_node: int = 1
    tensor_parallel: int = 1
    # Engines that open as prefill, counted in placement order. None opens half.
    prefill: int | None = None
    engine_port: int = 8100
    transfer_port: int = 5600
    max_sequences: int | None = None
    engine_args: tuple[str, ...] = ()
    engine_env: Mapping[str, str] = field(default_factory=dict)
    engine_python: str = "python3"
    device_env: str = "CUDA_VISIBLE_DEVICES"
    router_host: str = "0.0.0.0"
    router_port: int = 8000
    profile_args: tuple[str, ...] = ()
    # Extra fleet fields by dotted path, such as {"serving.admission": "open"}.
    fleet_settings: Mapping[str, Any] = field(default_factory=dict)
    health_timeout_s: float = 1800.0
    ready_timeout_s: float = 600.0
    stop_grace_s: float = 30.0


class StageFailed(Exception):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


class Stopped(Exception):
    pass


@dataclass(frozen=True)
class Placement:
    rank: int
    local: int
    devices: tuple[str, ...]
    engine: AllocationEngine


def run_dir(settings: Settings, allocation: Allocation) -> Path:
    return settings.run_root / f"slurm-{allocation.job}"


def place(settings: Settings, allocation: Allocation, addresses: list[str]) -> list[Placement]:
    """Return every engine of the allocation, numbered n1, n2, ... in node order."""
    width = settings.engines_per_node * settings.tensor_parallel
    placements: list[Placement] = []
    for rank, address in enumerate(addresses):
        devices = (
            allocation.devices
            if rank == allocation.rank and allocation.devices
            else tuple(str(index) for index in range(width))
        )
        if len(devices) < width:
            raise ValueError(
                f"{settings.engines_per_node} engines of {settings.tensor_parallel} devices need "
                f"{width} devices, but {settings.device_env} lists {len(devices)}"
            )
        for local in range(settings.engines_per_node):
            number = len(placements) + 1
            engine = AllocationEngine(
                iid=f"n{number}",
                model=settings.model,
                served_name=settings.served_name,
                host=address,
                port=settings.engine_port + local,
                transfer_port=settings.transfer_port + local,
                tensor_parallel=settings.tensor_parallel,
                max_sequences=settings.max_sequences,
                extra_args=settings.engine_args,
            )
            chosen = devices[
                local * settings.tensor_parallel : (local + 1) * settings.tensor_parallel
            ]
            placements.append(Placement(rank, local, chosen, engine))
    total = len(placements)
    prefill = total // 2 if settings.prefill is None else settings.prefill
    if total < 2 or not 1 <= prefill < total:
        raise ValueError(
            f"the fleet needs at least one prefill and one decode engine: {total} engines, "
            f"{prefill} prefill"
        )
    return placements


def opening_prefill(settings: Settings, placements: list[Placement]) -> int:
    return len(placements) // 2 if settings.prefill is None else settings.prefill


# Fleet fields the launch writes itself; --fleet-setting cannot replace them.
LAUNCH_FIELDS = (
    "schema",
    "schema_version",
    "model",
    "engines",
    "slo.ttft_s",
    "slo.tpot_s",
    "engine.backend",
    "profiles.path",
    "recovery.state_path",
)


def check_fleet_setting(path: str) -> None:
    """Refuse a dotted path that names, contains or lies inside a field the launch writes."""
    parts = path.split(".")
    if not path or any(not part for part in parts):
        raise ValueError(f"fleet setting {path!r} must be a dotted field path")
    for owned in LAUNCH_FIELDS:
        prefix = owned.split(".")
        if parts[: len(prefix)] == prefix or prefix[: len(parts)] == parts:
            raise ValueError(
                f"fleet setting {path!r} would replace {owned}, which the launch writes"
            )


def _set_field(document: dict, path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = document
    for name in parents:
        child = node.setdefault(name, {})
        if not isinstance(child, dict):
            raise ValueError(f"fleet setting {path!r}: {name} is not an object")
        node = child
    node[leaf] = value


def fleet_document(settings: Settings, placements: list[Placement], directory: Path) -> dict:
    prefill = opening_prefill(settings, placements)
    document = {
        "schema": "narwhal.fleet",
        "schema_version": 2,
        "model": settings.served_name,
        "engines": [
            {
                "iid": p.engine.iid,
                "url": f"http://{p.engine.host}:{p.engine.port}",
                "role": "prefill" if index < prefill else "decode",
            }
            for index, p in enumerate(placements)
        ],
        "slo": {"ttft_s": settings.slo_ttft_s, "tpot_s": settings.slo_tpot_s},
        "engine": {"backend": settings.backend},
        "profiles": {"path": str(directory / "profiles.json")},
        "recovery": {"state_path": str(directory / "router-state.json")},
    }
    for path, value in settings.fleet_settings.items():
        check_fleet_setting(path)
        _set_field(document, path, value)
    return document


def limits_document(settings: Settings, placements: list[Placement]) -> dict | None:
    if settings.max_sequences is None:
        return None
    return {
        "schema": "narwhal.profiling-limits",
        "schema_version": 1,
        "engines": {p.engine.iid: settings.max_sequences for p in placements},
    }


def engine_environment(
    settings: Settings, placement: Placement, start: AllocationStart, base: Mapping[str, str]
) -> dict[str, str]:
    """The engine's environment: the launch's, without Narwhal's own Python environment."""
    env = {
        key: value
        for key, value in base.items()
        if key not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
    }
    own_bin = str(Path(sys.prefix) / "bin")
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry for entry in env["PATH"].split(os.pathsep) if entry and entry != own_bin
        )
    env.update(settings.engine_env)
    env[settings.device_env] = ",".join(placement.devices)
    env.update(start.environment(placement.engine))
    return env


def _write(path: Path, value: object) -> None:
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


class Launch:
    """Run this node's part of the launch until a stop request or a failure."""

    def __init__(
        self,
        settings: Settings,
        allocation: Allocation,
        start: AllocationStart,
        addresses: list[str],
        *,
        stop: threading.Event,
        env: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.allocation = allocation
        self.start = start
        self.placements = place(settings, allocation, addresses)
        self.directory = run_dir(settings, allocation)
        self.stop = stop
        self.env = dict(os.environ if env is None else env)
        self._own_client = client is None
        self.client = client or httpx.Client(timeout=5, trust_env=False)
        self.say = say or (lambda line: print(f"narwhal-slurm: {line}", flush=True))
        self.engines: list[tuple[str, subprocess.Popen]] = []
        self.router: subprocess.Popen | None = None
        self.profiler: subprocess.Popen | None = None
        self._logs: list[IO[Any]] = []

    @property
    def fleet_path(self) -> Path:
        return self.directory / "fleet.json"

    def run(self) -> None:
        """Run the stages; raise StageFailed naming the failed stage, or Stopped."""
        try:
            self._prepare()
            self._start_engines()
            if self.allocation.head:
                self._write_fleet()
                self._wait_health()
                self._profile()
                self._start_router()
                self._wait_ready()
            self._serve()
        finally:
            self.shutdown()

    def _prepare(self) -> None:
        try:
            (self.directory / "logs").mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StageFailed("prepare", f"{self.directory}: {error}") from error
        if self.allocation.head and self.fleet_path.exists():
            raise StageFailed("prepare", f"{self.fleet_path} exists from an earlier launch")

    def _start_engines(self) -> None:
        local = [p for p in self.placements if p.rank == self.allocation.rank]
        for placement in local:
            engine = placement.engine
            command = self.start.command(self.settings.engine_python, engine)
            env = engine_environment(self.settings, placement, self.start, self.env)
            log = open(self.directory / "logs" / f"{engine.iid}.log", "ab")  # noqa: SIM115
            self._logs.append(log)
            try:
                child = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except OSError as error:
                raise StageFailed("engines", f"{engine.iid}: {command[0]}: {error}") from error
            self.engines.append((engine.iid, child))
            self.say(
                f"engines: started {engine.iid} at http://{engine.host}:{engine.port} "
                f"on devices {','.join(placement.devices)} (pid {child.pid})"
            )

    def _write_fleet(self) -> None:
        try:
            _write(self.fleet_path, fleet_document(self.settings, self.placements, self.directory))
            limits = limits_document(self.settings, self.placements)
            if limits is not None:
                _write(self.directory / "profiling-limits.json", limits)
            load_fleet(self.fleet_path)
        except (OSError, ValueError) as error:
            raise StageFailed("fleet", str(error)) from error
        self.say(f"fleet: wrote {self.fleet_path} with {len(self.placements)} engines")

    def _watch(self, stage: str) -> None:
        if self.stop.is_set():
            raise Stopped
        for iid, child in self.engines:
            code = child.poll()
            if code is not None:
                log = self.directory / "logs" / f"{iid}.log"
                raise StageFailed(stage, f"engine {iid} exited with status {code}; see {log}")
        if self.router is not None and (code := self.router.poll()) is not None:
            raise StageFailed(
                stage,
                f"router exited with status {code}; see {self.directory / 'logs' / 'router.log'}",
            )

    def _wait(self, stage: str, done: Callable[[], bool], timeout_s: float | None) -> None:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            self._watch(stage)
            if done():
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise StageFailed(stage, f"not done after {timeout_s:g} seconds")
            self.stop.wait(POLL_S)

    def _ok(self, url: str) -> bool:
        try:
            return self.client.get(url).status_code == 200
        except httpx.HTTPError:
            return False

    def _wait_health(self) -> None:
        pending = {p.engine.iid: f"http://{p.engine.host}:{p.engine.port}" for p in self.placements}

        def healthy() -> bool:
            for iid, url in list(pending.items()):
                if self._ok(url + "/health"):
                    del pending[iid]
            return not pending

        try:
            self._wait("health", healthy, self.settings.health_timeout_s)
        except StageFailed as error:
            if error.detail.startswith("not done"):
                raise StageFailed(
                    "health",
                    f"no /health 200 from {', '.join(sorted(pending))} after "
                    f"{self.settings.health_timeout_s:g} seconds",
                ) from None
            raise
        self.say(f"health: {len(self.placements)} engines answer /health")

    def _child(self, name: str, module: str, args: list[str]) -> subprocess.Popen:
        command = [sys.executable, "-u", "-m", module, *args]
        log = open(self.directory / "logs" / f"{name}.log", "ab")  # noqa: SIM115
        self._logs.append(log)
        return subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=self.directory,
            start_new_session=True,
        )

    def _profile(self) -> None:
        args = ["--fleet", str(self.fleet_path)]
        if self.settings.max_sequences is not None:
            args += ["--limits", str(self.directory / "profiling-limits.json")]
        args += list(self.settings.profile_args)
        self.say("profile: profiling every engine")
        started = time.monotonic()
        self.profiler = self._child("profile", "narwhal.profiling.probe.cli", args)
        profiler = self.profiler
        self._wait("profile", lambda: profiler.poll() is not None, None)
        self.profiler = None
        if profiler.returncode:
            raise StageFailed(
                "profile",
                f"narwhal-profile exited with status {profiler.returncode}; "
                f"see {self.directory / 'logs' / 'profile.log'}",
            )
        self.say(f"profile: done in {time.monotonic() - started:.0f} seconds")

    def _router_url(self) -> str:
        host = self.settings.router_host
        local = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"http://{local}:{self.settings.router_port}"

    def _start_router(self) -> None:
        args = [
            "--fleet",
            str(self.fleet_path),
            "--host",
            self.settings.router_host,
            "--port",
            str(self.settings.router_port),
            "--journal",
            str(self.directory / "journal.jsonl"),
        ]
        self.router = self._child("router", "narwhal.cli", args)
        self.say(f"router: started (pid {self.router.pid})")

    def _wait_ready(self) -> None:
        url = self._router_url() + "/ready"
        self._wait("ready", lambda: self._ok(url), self.settings.ready_timeout_s)
        head = self.placements[0].engine.host if self.placements else "127.0.0.1"
        self.say(f"ready: the router serves at http://{head}:{self.settings.router_port}")

    def _serve(self) -> None:
        self._wait("serve", lambda: False, None)

    def shutdown(self) -> None:
        """Stop the profiler and the router, then every engine this node started."""
        for name, child in (("profiler", self.profiler), ("router", self.router)):
            if child is not None:
                self._terminate(name, child)
        self.profiler = self.router = None
        for iid, child in self.engines:
            self._terminate(f"engine {iid}", child)
        self.engines = []
        for log in self._logs:
            log.close()
        self._logs = []
        if self._own_client:
            self.client.close()

    def _terminate(self, name: str, child: subprocess.Popen) -> None:
        if child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            child.wait(self.settings.stop_grace_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        self.say(f"stop: stopped {name}")
