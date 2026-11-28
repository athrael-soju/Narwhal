"""`narwhal dev up` as stages of the dev stage graphs, run by the deployment stage runner.

Each `up` run directory holds the runner's own directory, `launch`, beside the files of the
running fleet: the router's fleet file, its journal and state, and the memory samples. A stage
that passes keeps the processes it started, and a stage that fails or is interrupted stops
them first, so the next `up` resumes at the stage that did not pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from narwhal import provenance
from narwhal.config import FleetConfig
from narwhal.deployment import native_engine
from narwhal.deployment.attestation_contract.capture import PUBLISHED, published_attempt
from narwhal.deployment.attestation_contract.fleet import finalize_fleet
from narwhal.deployment.dev_graph import DIRECT_GRAPH, ENGINE, LAUNCHED_GRAPH
from narwhal.deployment.direct_start import DirectEngine, measure
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.graph import StageGraph
from narwhal.deployment.launch_engine.backend import cache_capture, launcher
from narwhal.deployment.launch_engine.plan import prepare
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.deployment.launch_engine.settings import KV_SIDE_CHANNEL_PORT_ENV
from narwhal.deployment.launch_engine.start import READY_SECONDS
from narwhal.deployment.run_record import key
from narwhal.deployment.runner import (
    HelpersSurvived,
    Input,
    Outcome,
    RunCancelled,
    RunLocked,
    Runner,
    StageContext,
    StageFailed,
    StaticTopology,
    stale_notice,
)
from narwhal.deployment.supervision import ProcessGroup, ProcessGroups, ProcessSpec
from narwhal.engines.attestation import AttestationDocument, launch_digest
from narwhal.profiling.model import nominal_profile
from narwhal.profiling.store import ProfileStore
from narwhal.serving.control_token import ROUTER_TOKEN_ENV, token_problem

from . import direct, lifecycle
from .template import _check_free_ports, starts_directly

# The runner's directory inside an `up` run directory.
RECORD = "launch"
# Discarded requests each engine on its own GPU serves in each role before profiling.
WARMUP_REQUESTS = 8
# How long a started sidecar or router has to answer its health route.
HEALTH_SECONDS = 30
# How long a router of directly started engines has to report itself ready.
READY_TIMEOUT_S = 60
SUPERVISOR = ProcessGroups()


def launches(run: Path) -> dict[str, list[Path]]:
    """Each engine's launch directories in this run, oldest attempt first."""
    found: dict[str, list[tuple[int, Path]]] = {}
    for attempt in (run / RECORD / "stages" / "engine-prepare").glob("engine-*/attempt-*"):
        engine = attempt.parent.name
        found.setdefault(engine, []).append((int(attempt.name.removeprefix("attempt-")), attempt))
    return {
        engine: [attempt / engine for _, attempt in sorted(attempts)]
        for engine, attempts in sorted(found.items())
    }


def process_group(identity: Mapping[str, Any]) -> ProcessGroup:
    return ProcessGroup(identity["pid"], identity["boot_id"], identity["start_ticks"])


def _running(identity: Mapping[str, Any]) -> bool:
    return SUPERVISOR.check(process_group(identity)).state == "running"


def _started(launch: Path) -> bool:
    return any((launch / name).exists() for name in native_engine.STARTUP_RECORDS)


def _engine_running(launch: Path) -> bool:
    path = launch / "native-process.json"
    return path.is_file() and _running(lifecycle._read_identity(path))


# The instance's router control token, which `narwhal dev` draws when the environment holds none.
ROUTER_TOKEN_FILE = "router-token"  # noqa: S105 - a file name


def router_token(root: Path) -> str:
    """The router's control token: `NARWHAL_ROUTER_TOKEN`, or else the instance's own.

    The instance's token is drawn once and kept at mode 0600 in `root/router-token`, so a
    restarted router takes the token the operator already exported from it.
    """
    if not token_problem(os.environ.get(ROUTER_TOKEN_ENV, "")):
        return os.environ[ROUTER_TOKEN_ENV]
    path = root / ROUTER_TOKEN_FILE
    if not path.is_file():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    token = path.read_text().strip()
    if problem := token_problem(token, str(path)):
        raise ValueError(problem)
    return token


def _key_variable(fleet: dict) -> str | None:
    """The variable that holds the engines' API key, as the run's fleet file names it."""
    configured = fleet.get("engine", {}).get("engine_api_key_env")
    if configured:
        return str(configured)
    return "NARWHAL_ENGINE_API_KEY" if os.environ.get("NARWHAL_ENGINE_API_KEY") else None


def _wait(url: str, identity: Mapping[str, Any], seconds: float = HEALTH_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if not native_engine.owns_process(dict(identity)):
                raise ValueError(f"process exited before {url} became healthy; inspect its log")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
    raise ValueError(f"health deadline expired: {url}")


def _free(url: str) -> None:
    address = urlsplit(url)
    if address.hostname is None or address.port is None:
        raise ValueError(f"{url} names no host and port")
    _check_free_ports({address.port}, address.hostname)


@contextlib.contextmanager
def _undisturbed() -> Iterator[None]:
    """Ignore SIGINT and SIGTERM while a stage stops what it started, as `stages.run` does."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _profiles(run: Path, fleet: dict, spec: dict) -> None:
    count = len(fleet["engines"])
    # Neighbour load follows the split's roles; without it, one split's profiles cover every split.
    # Engines on their own GPUs have no neighbours.
    shared = any("shared_device" in engine for engine in fleet["engines"])
    colocated = shared and "neighbour_prefill_rps" in spec["profile"]
    opening = sum(engine["role"] == "prefill" for engine in fleet["engines"])
    sources = []
    for prefill in range(1, count) if colocated else (opening,):
        measured = json.loads(json.dumps(fleet))
        for index, engine in enumerate(measured["engines"]):
            engine["role"] = "prefill" if index < prefill else "decode"
        profile = run / f"profiles-{prefill}p{count - prefill}d.json"
        measured["profiles"]["path"] = str(profile)
        fleet_path = run / f"profile-{prefill}p{count - prefill}d.fleet.json"
        lifecycle.write(fleet_path, measured)
        args = ["--fleet", str(fleet_path)]
        if colocated:
            args.append("--colocated")
        if not shared:
            # Without neighbour load, nothing else exercises an engine before its sweep.
            args.extend(["--warmup-requests", str(WARMUP_REQUESTS)])
        for name, value in spec["profile"].items():
            if name.startswith("neighbour_") and not colocated:
                continue
            rendered = ",".join(map(str, value)) if isinstance(value, list) else str(value)
            args.extend(["--" + name.replace("_", "-"), rendered])
        print(
            f"profiling {prefill} prefill / {count - prefill} decode", file=sys.stderr, flush=True
        )
        lifecycle._run(
            run, "narwhal.profiling.probe.cli", args, f"profile-{prefill}p{count - prefill}d"
        )
        sources.append(profile)
    if len(sources) == 1:
        (run / "profiles.json").write_bytes(sources[0].read_bytes())
    else:
        args = ["--fleet", str(run / "fleet.json"), "--out", str(run / "profiles.json")]
        for source in sources:
            args.extend(["--merge", str(source)])
        lifecycle._run(run, "narwhal.profiling.probe.cli", args, "profile-merge")


class _Launch:
    """The actions, live checks and inputs of one instance's `up`."""

    def __init__(
        self, root: Path, run: Path, config: dict, spec: dict, state: dict, resuming: bool
    ) -> None:
        self.root = root
        self.run = run
        self.config = config
        self.spec = spec
        self.state = state
        self.resuming = resuming
        self.direct = starts_directly(config)
        self.engines = tuple(f"engine-{number}" for number in range(1, config["engine_count"] + 1))

    @property
    def graph(self) -> StageGraph:
        return DIRECT_GRAPH if self.direct else LAUNCHED_GRAPH

    def execute(self) -> None:
        actions: dict[str, Callable[[StageContext], Outcome | None]] = (
            {
                "package": self.package,
                "engine-start": self.start_direct_engines,
                "profile": self.profile_direct,
                "router-start": self.start_router,
            }
            if self.direct
            else {
                "package": self.package,
                "engine-prepare": self.prepare_engine,
                "engine-check": self.check_engine,
                "engine-start": self.start_engines,
                "attest-capture": self.capture_attestation,
                "sidecar-start": self.start_sidecar,
                "finalize-fleet": self.finalize,
                "profile": self.profile,
                "router-start": self.start_router,
            }
        )
        probes = {
            "engine_identity": self.engines_running,
            "router_identity": self.process_running,
            **(
                {}
                if self.direct
                else {"launch": self.launch_usable, "sidecar_identity": self.process_running}
            ),
        }
        runner = Runner(
            self.graph,
            self.run / RECORD,
            target="dev",
            revision=provenance.source_digest(),
            actions={name: self._announced(action) for name, action in actions.items()},
            topology=StaticTopology({ENGINE: self.engines}),
            probes=probes,
        )
        try:
            result = runner.run(self.inputs())
        except (StageFailed, RunCancelled) as error:
            _report_stale(error.stale_lock)
            # The stage's own error is what `up` reports; the run record keeps the stage.
            cause = error.__cause__
            if cause is None:
                raise
            raise cause from cause.__cause__
        except (HelpersSurvived, RunLocked) as error:
            _report_stale(getattr(error, "stale_lock", None))
            raise lifecycle.LifecycleOperationError(str(error)) from error
        _report_stale(result.stale_lock)

    def inputs(self) -> dict[str, Input]:
        files: tuple[str, ...] = ("instance.json", "template.json", "fleet.json")
        if not self.direct:
            files += ("engine-launch.json",)
        values = {
            "instance": Input.value(
                {name: digest(self.root / name) for name in files}, str(self.root)
            ),
            "release": Input(provenance.source_digest(), str(Path(provenance.__file__).parent)),
        }
        if not self.direct:
            variable = _key_variable(lifecycle.read(self.root / "fleet.json"))
            secret = os.environ.get(variable, "") if variable else ""
            values["engine_key"] = Input.value(
                {"variable": variable, "key": secret}, variable or "no engine API key"
            )
        return values

    def _announced(
        self, action: Callable[[StageContext], Outcome | None]
    ) -> Callable[[StageContext], Outcome | None]:
        def announced(context: StageContext) -> Outcome | None:
            if self.resuming:
                # The first stage that runs again is where this `up` resumes.
                self.resuming = False
                print(
                    f"resuming {self.run} at {key(context.stage, context.instance)}",
                    file=sys.stderr,
                    flush=True,
                )
            return action(context)

        return announced

    # Live checks: each returns the recorded value while it still holds, and None once not.

    def process_running(self, instance: str, recorded: Any) -> Any:
        return recorded if _running(recorded) else None

    def engines_running(self, instance: str, recorded: Any) -> Any:
        return recorded if all(map(_running, recorded.values())) else None

    def launch_usable(self, instance: str, recorded: Any) -> Any:
        """A launch directory holds while no engine started from it, or while every engine of
        the fleet runs from the newest launch directory of its own."""
        directory = self.run / RECORD / recorded["directory"]
        if not (directory / "launch.json").is_file():
            return None
        if not _started(directory):
            return recorded
        newest = launches(self.run)
        for engine in self.engines:
            latest = newest.get(engine, [])
            if not latest or not _started(latest[-1]) or not _engine_running(latest[-1]):
                return None
        return recorded

    # Process records: `lifecycle.json` names each process as it starts, so `down` reaches it
    # even when this `up` dies before the run record does.

    def _record(self, name: str, group: ProcessGroup) -> None:
        processes = [*self.state["processes"], {"name": name, "identity": group.leader()}]
        lifecycle.write(self.root / "lifecycle.json", {**self.state, "processes": processes})
        self.state["processes"] = processes

    def _forget(self, name: str) -> None:
        """Stop the recorded process `name`, from an earlier attempt, and drop its record."""
        for record in [r for r in self.state["processes"] if r["name"] == name]:
            SUPERVISOR.stop(process_group(record["identity"]))
            self.state["processes"].remove(record)
            lifecycle.write(self.root / "lifecycle.json", self.state)

    def _abandon(self, names: Sequence[str], error: BaseException) -> None:
        """Stop what a failed stage started, keeping the stage's error."""
        with _undisturbed():
            for name in names:
                try:
                    self._forget(name)
                except (OSError, ValueError) as cleanup:
                    error.add_note(f"{name} cleanup: {cleanup}")

    def _start(
        self,
        context: StageContext,
        name: str,
        argv: list[str],
        environment: Mapping[str, str],
        url: str,
    ) -> ProcessGroup:
        """Start `name` in place of an earlier attempt's process, once `url`'s port is free.

        The process runs from the run directory and logs to the stage's attempt directory. A
        start interrupted before `lifecycle.json` names the process stops it again.
        """
        self._forget(name)
        _free(url)
        lifecycle.write(context.directory / f"{name}.command.json", {"argv": argv})
        group = SUPERVISOR.start(
            ProcessSpec(name, tuple(argv), self.run, self._log(context, name), dict(environment))
        )
        try:
            self._record(name, group)
        except BaseException as error:
            with _undisturbed():
                try:
                    SUPERVISOR.stop(group)
                except (OSError, ValueError) as cleanup:
                    error.add_note(f"{name} cleanup: {cleanup}")
            raise
        return group

    def _serve(
        self,
        context: StageContext,
        name: str,
        argv: list[str],
        environment: Mapping[str, str],
        url: str,
        checks: Sequence[tuple[str, float]],
    ) -> Outcome:
        """Start one process and wait for each route; a process that fails a check stops."""
        group = self._start(context, name, argv, environment, url)
        try:
            for route, seconds in checks:
                _wait(route, group.leader(), seconds)
        except BaseException as error:
            self._abandon([name], error)
            raise
        return Outcome(
            processes=({"name": name, **group.record()},), live=(self._log(context, name),)
        )

    @staticmethod
    def _log(context: StageContext, name: str) -> Path:
        """The log that the process `name` writes for as long as it runs."""
        return context.directory / f"{name}.log"

    # Stages shared by both graphs.

    def package(self, context: StageContext) -> None:
        """Write the run's fleet file and, for launched engines, each engine's launch record."""
        fleet = lifecycle.read(self.root / "fleet.json")
        fleet["recovery"] = {"state_path": str(self.run / "router-state.json")}
        if not self.direct:
            fleet.pop("engine_contract", None)
            variable = _key_variable(fleet)
            if variable:
                fleet.setdefault("engine", {})["engine_api_key_env"] = variable
            allocation = lifecycle.read(self.root / "engine-launch.json")
            for engine in self.engines:
                lifecycle.write(
                    context.directory / f"{engine}.selected.json",
                    selected_launch(allocation, engine, {}),
                )
        lifecycle.write(context.directory / "fleet.json", fleet)

    def start_router(self, context: StageContext) -> Outcome:
        """Start the router with the control token `router_token` gives."""
        source = "package" if self.direct else "finalize-fleet"
        fleet = lifecycle.read(context.outputs(source) / "fleet.json")
        fleet["profiles"]["path"] = str(context.outputs("profile") / "profiles.json")
        lifecycle.write(self.run / "fleet.json", fleet)
        url = self.config["router_url"]
        router = urlsplit(url)
        checks = [(url + "/health", HEALTH_SECONDS)]
        if self.direct:
            checks.append((url + "/ready", READY_TIMEOUT_S))
        outcome = self._serve(
            context,
            "router",
            [
                sys.executable,
                "-m",
                "narwhal.cli",
                "--fleet",
                str(self.run / "fleet.json"),
                "--host",
                str(router.hostname),
                "--port",
                str(router.port),
                "--journal",
                str(self.run / "journal.jsonl"),
            ],
            {ROUTER_TOKEN_ENV: router_token(self.root)},
            url,
            checks,
        )
        return Outcome(
            {"router_identity": dict(outcome.processes[0])}, outcome.processes, outcome.live
        )

    # Stages of launched engines.

    def _engine_run(self, context: StageContext, engine: str) -> Path:
        return context.outputs("engine-prepare", engine) / engine

    def prepare_engine(self, context: StageContext) -> Outcome:
        package = context.outputs("package")
        fleet = lifecycle.read(package / "fleet.json")
        index = self.engines.index(context.instance)
        number = index + 1
        engine = fleet["engines"][index]
        capture = cache_capture(launcher(self.spec["runtime"].get("backend")))
        hook = (
            {}
            if capture is None
            else {
                "NARWHAL_CACHE_CAPTURE_HOOK": str(capture.cache_hook),
                "NARWHAL_CACHE_CAPTURE_HOOK_SHA256": digest(capture.cache_hook),
            }
        )
        engine_key = FleetConfig.load(package / "fleet.json").resolve_engine_key()
        ports = self.config["ports"]
        env = {
            **os.environ,
            "NARWHAL_ENGINE_LAUNCH_CONFIG": str(package / f"{context.instance}.selected.json"),
            "NARWHAL_MODEL_DIR": self.config["model_dir"],
            "NARWHAL_MODEL_PATH": self.config["model_path"],
            "NARWHAL_MODEL_REVISION": self.config["model_revision"],
            "NARWHAL_MODEL_CONFIG_SHA256": self.config["model_config_sha256"],
            **hook,
            "NARWHAL_ENGINE_PORT": str(ports["engine_first"] + index),
            "NARWHAL_ATTEST_PORT": str(ports["attestation_first"] + index),
            KV_SIDE_CHANNEL_PORT_ENV: str(ports["side_channel_first"] + index),
            "NARWHAL_UCX_TCP_PORT_RANGE": self.config["ucx_range"],
            f"NARWHAL_NODE_{number}_IP": self.config["fabric_address"],
            f"NARWHAL_NODE_{number}_URL": engine["url"],
            f"NARWHAL_NODE_{number}_ATTESTATION_URL": engine["attestation_url"],
            "NARWHAL_ENGINE_MODEL_NAME": fleet["model"],
            "NARWHAL_ENGINE_API_KEY": engine_key or "",
            "NARWHAL_DEPLOYMENT_REVISION": digest(self.root / "template.json"),
        }
        engine_run = context.directory / context.instance
        with contextlib.redirect_stdout(sys.stderr):
            prepare(engine_run, env, backend="native")
        return Outcome(
            {
                "launch": {
                    "directory": str(engine_run.relative_to(self.run / RECORD)),
                    "plan_sha256": digest(engine_run / "launch.json"),
                }
            }
        )

    def check_engine(self, context: StageContext) -> None:
        lifecycle._run(
            context.directory,
            "narwhal.deployment.launch_engine.cli",
            ["check", "--run", str(self._engine_run(context, context.instance))],
            context.instance,
        )

    def start_engines(self, context: StageContext) -> Outcome:
        runs = [self._engine_run(context, engine) for engine in self.engines]
        started = [_started(run) for run in runs]
        if any(started):
            # A start that passed before this `up` recorded it left every engine running.
            if not all(started) or not all(map(_engine_running, runs)):
                raise ValueError(
                    "the engines of an interrupted start no longer all run; run narwhal dev up "
                    "again to start them from fresh launch directories"
                )
        else:
            # Engines from earlier launch directories of this run give way to the new ones.
            for attempts in launches(self.run).values():
                for run in attempts:
                    if _engine_running(run):
                        SUPERVISOR.stop(
                            process_group(lifecycle._read_identity(run / "native-process.json"))
                        )
            # An engine that compiles kernels on its first start needs a longer window.
            ready = self.spec["runtime"].get("ready_seconds", READY_SECONDS)
            # Engines on one GPU start as its shared group; engines on their own GPUs one each.
            start, stage = (
                ("start-native", "native-start")
                if "gpu_uuids" in self.config
                else ("start-shared", "native-start-shared")
            )
            lifecycle._run(
                context.directory,
                "narwhal.deployment.launch_engine.cli",
                [
                    start,
                    *(["--backend", "native"] if start == "start-shared" else []),
                    "--ready-seconds",
                    str(ready),
                    *[arg for run in runs for arg in ("--run", str(run))],
                ],
                stage,
                default_timeout=ready * len(runs),
            )
        identities = {
            engine: lifecycle._read_identity(run / "native-process.json")
            for engine, run in zip(self.engines, runs, strict=True)
        }
        return Outcome(
            {"engine_identity": identities},
            tuple(
                {"name": engine, **process_group(identity).record()}
                for engine, identity in identities.items()
            ),
        )

    def capture_attestation(self, context: StageContext) -> Outcome:
        engine_run = self._engine_run(context, context.instance)
        # A capture that published before this `up` recorded it stands for this engine.
        if not (engine_run / PUBLISHED).exists():
            lifecycle._run(
                context.directory,
                "narwhal.deployment.attestation_contract.cli",
                ["capture", "--run", str(engine_run)],
                f"attest-{self.engines.index(context.instance) + 1}",
            )
        document = AttestationDocument.load(
            published_attempt(engine_run) / "engine-attestation.json"
        )
        if document.launch is None:
            raise ValueError(f"{context.instance}: the attestation holds no launch evidence")
        contract = document.contract.fields()
        return Outcome(
            {"binding": launch_digest(contract, document.launch), "engine_contract": contract}
        )

    def start_sidecar(self, context: StageContext) -> Outcome:
        number = self.engines.index(context.instance) + 1
        fleet = lifecycle.read(context.outputs("package") / "fleet.json")
        url = fleet["engines"][number - 1]["attestation_url"]
        outcome = self._serve(
            context,
            f"sidecar-{number}",
            [
                sys.executable,
                "-m",
                "narwhal.deployment.attestation_contract.cli",
                "serve",
                "--run",
                str(self._engine_run(context, context.instance)),
            ],
            {f"NARWHAL_NODE_{number}_ATTESTATION_URL": url},
            url,
            [(url, HEALTH_SECONDS)],
        )
        return Outcome(
            {"sidecar_identity": dict(outcome.processes[0])}, outcome.processes, outcome.live
        )

    def finalize(self, context: StageContext) -> None:
        path = context.directory / "fleet.json"
        lifecycle.write(path, lifecycle.read(context.outputs("package") / "fleet.json"))
        finalize_fleet(path)

    def profile(self, context: StageContext) -> None:
        fleet = lifecycle.read(context.outputs("finalize-fleet") / "fleet.json")
        # The merge of several role splits reads the fleet beside the profiles it merges.
        lifecycle.write(context.directory / "fleet.json", fleet)
        _profiles(context.directory, fleet, self.spec)

    # Stages of engines that start directly.

    def start_direct_engines(self, context: StageContext) -> Outcome:
        fleet = lifecycle.read(context.outputs("package") / "fleet.json")
        start = direct._direct_start(self.spec)
        binary = Path(self.config["engine_binary"])
        shared = context.directory / "slot-files"
        shared.mkdir(mode=0o700)
        threads = direct._threads(self.config["engine_count"])
        environment = start.environment(binary)
        groups: dict[str, ProcessGroup] = {}
        try:
            for name, engine in zip(self.engines, fleet["engines"], strict=True):
                port = urlsplit(engine["url"]).port
                if port is None:
                    raise ValueError(f"engine url names no port: {engine['url']}")
                command = start.command(
                    binary,
                    DirectEngine(
                        model=Path(self.config["model_path"]),
                        served_name=fleet["model"],
                        host=direct.HOST,
                        port=port,
                        runtime=self.spec["runtime"],
                        shared_dir=shared,
                        accelerated=self.config["accelerated"],
                        threads=threads,
                    ),
                )
                groups[name] = self._start(context, name, command, environment, engine["url"])
            for name, engine in zip(self.engines, fleet["engines"], strict=True):
                _wait(engine["url"] + "/health", groups[name].leader(), READY_SECONDS)
        except BaseException as error:
            self._abandon(list(groups), error)
            raise
        return Outcome(
            {"engine_identity": {name: group.leader() for name, group in groups.items()}},
            tuple({"name": name, **group.record()} for name, group in groups.items()),
            # The engines write their logs and the handed-over KV slots while they serve.
            (*(self._log(context, name) for name in groups), shared),
        )

    def profile_direct(self, context: StageContext) -> None:
        fleet = lifecycle.read(context.outputs("package") / "fleet.json")
        store = ProfileStore(context.directory / "profiles.json")
        start = direct._direct_start(self.spec)
        for iid, figures in measure(start, fleet, self.spec["runtime"]).items():
            store.put(
                nominal_profile(
                    iid,
                    prefill_token_s=figures.prefill_token_s,
                    token_interval_s=figures.token_interval_s,
                    kv_capacity_tokens=figures.kv_capacity_tokens,
                    max_requests=figures.max_requests,
                )
            )


def run(root: Path, run: Path, config: dict, spec: dict, state: dict, resuming: bool) -> None:
    """Run the instance's stage graph in `run`, resuming the stages an earlier `up` left."""
    _Launch(root, run, config, spec, state, resuming).execute()


def _report_stale(stale: Mapping[str, Any] | None) -> None:
    if stale is not None:
        print(f"narwhal: dev up: {stale_notice(stale)}", file=sys.stderr, flush=True)
