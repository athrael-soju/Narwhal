"""Start, check and stop the long-lived processes that a deployment leaves running.

Each target has one supervisor, and neither falls back to the other. On SSH hosts,
`SystemdUnits` runs each process as a transient systemd user unit that systemd restarts
on failure. `narwhal dev` runs detached process groups through `ProcessGroups`, which
nothing restarts.

A supervisor returns an identity when it starts a process. `check` compares a recorded
identity with the live process. `confirm` turns a systemd restart into a new identity
once the caller's functional check passes. `stop` signals only a process whose recorded
identity still matches.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Generic, Literal, Protocol, TypeVar

from ..observability.restarts import UNIT_ENV
from . import native_engine, stages

# Names become part of a systemd unit name, so they stay within its safe characters.
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
RUN_ID = re.compile(r"[0-9a-f]{32}")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BOOT_ID = "/proc/sys/kernel/random/boot_id"
# `Type=exec` and `StandardOutput=append:` need systemd 240.
SYSTEMD_VERSION = 240
# Both targets allow a stopped process this long after SIGTERM before they send SIGKILL.
STOP_GRACE_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 60.0
# Characters that a double-quoted value in a systemd environment file escapes.
_ENVIRONMENT_ESCAPES = str.maketrans({c: "\\" + c for c in '\\"`$'})
_UNIT_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Description",
    "InvocationID",
    "MainPID",
    "NRestarts",
)
# Writes standard input to a new file at $1, private to the user, refusing any existing
# path, symbolic links and directories included: mktemp creates the draft exclusively at
# mode 0600, and `ln -T` fails when $1 exists instead of linking inside a directory. The
# draft is removed on every exit, signals included.
_CREATE_PRIVATE = (
    'draft=$(mktemp "$1.XXXXXXXX") || exit 1; '
    "trap 'rm -f \"$draft\"' EXIT; trap 'exit 1' HUP INT TERM; "
    'cat > "$draft" && ln -T "$draft" "$1"'
)

State = Literal["running", "restarted", "pending", "stopped", "mismatch"]


class SupervisionError(ValueError):
    """A supervised process could not be started, read or stopped."""


class IdentityMismatch(SupervisionError):
    """Something other than the recorded process holds its unit or process group."""


@dataclass(frozen=True)
class ProcessSpec:
    """One long-lived process, run from `cwd` with its output in `log`.

    A process group starts a fresh log. A systemd unit appends to it, so the log keeps
    the output of every automatic restart.

    `environment` adds variables to the target's base environment: the supervising
    process's own for process groups, and the user manager's for systemd units.
    """

    name: str
    argv: tuple[str, ...]
    cwd: Path
    log: Path
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            raise ValueError(f"process name {self.name!r} must match {NAME.pattern}")
        if not self.argv:
            raise ValueError(f"{self.name}: the command is empty")
        if not self.cwd.is_absolute() or not self.log.is_absolute():
            raise ValueError(f"{self.name}: the working directory and log need absolute paths")
        for key, value in self.environment.items():
            if not ENVIRONMENT_NAME.fullmatch(key):
                raise ValueError(f"{self.name}: invalid environment variable name {key!r}")
            if any(c in value for c in "\n\r\0"):
                raise ValueError(f"{self.name}: environment value of {key} must fit one line")


@dataclass(frozen=True)
class ProcessGroup:
    """The leader of a detached process group, as `native_engine.process_identity` reads it."""

    pid: int
    boot_id: str
    start_ticks: int
    kind: ClassVar[str] = "process-group"

    def leader(self) -> dict[str, int | str]:
        return {"pid": self.pid, "boot_id": self.boot_id, "start_ticks": self.start_ticks}

    def record(self) -> dict[str, int | str]:
        return {"kind": self.kind, **self.leader()}


@dataclass(frozen=True)
class SystemdUnit:
    """One invocation of a transient user unit and its main process.

    `nonce` is written into the unit's description at start, so it names that unit and
    no later one of the same name. `restarts` is the unit's `NRestarts` counter, which
    counts systemd's automatic restarts of it. `environment_file` is the private file the
    unit reads its environment from.
    """

    unit: str
    nonce: str
    invocation_id: str
    main_pid: int
    start_ticks: int
    boot_id: str
    restarts: int
    environment_file: str
    kind: ClassVar[str] = "systemd-unit"

    def record(self) -> dict[str, int | str]:
        return {
            "kind": self.kind,
            "unit": self.unit,
            "nonce": self.nonce,
            "invocation_id": self.invocation_id,
            "main_pid": self.main_pid,
            "start_ticks": self.start_ticks,
            "boot_id": self.boot_id,
            "restarts": self.restarts,
            "environment_file": self.environment_file,
        }


Identity = TypeVar("Identity", ProcessGroup, SystemdUnit)


def identity_from_record(record: Mapping[str, object]) -> ProcessGroup | SystemdUnit:
    """Read an identity that `record()` wrote, refusing missing or mistyped fields."""

    def text(name: str) -> str:
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            raise SupervisionError(f"supervised process identity needs a nonempty {name}")
        return value

    def count(name: str, minimum: int) -> int:
        value = record.get(name)
        if type(value) is not int or value < minimum:
            raise SupervisionError(f"supervised process identity needs {name} >= {minimum}")
        return value

    kind = record.get("kind")
    if kind == ProcessGroup.kind:
        return ProcessGroup(count("pid", 1), text("boot_id"), count("start_ticks", 0))
    if kind == SystemdUnit.kind:
        return SystemdUnit(
            unit=text("unit"),
            nonce=text("nonce"),
            invocation_id=text("invocation_id"),
            main_pid=count("main_pid", 1),
            start_ticks=count("start_ticks", 0),
            boot_id=text("boot_id"),
            restarts=count("restarts", 0),
            environment_file=text("environment_file"),
        )
    raise SupervisionError(f"unknown supervised process kind {kind!r}")


@dataclass(frozen=True)
class Observation(Generic[Identity]):
    """What `check` found for a recorded identity.

    - `running`: the recorded process is live, and `identity` is the record.
    - `restarted`: systemd restarted the unit since the record, and `identity` is the
      new invocation. The caller repeats its functional check before recording it.
    - `pending`: the unit is starting, stopping or waiting to restart.
    - `stopped`: nothing runs under the recorded identity.
    - `mismatch`: something else holds the unit or process group.
    """

    state: State
    detail: str
    identity: Identity | None = None


class Supervisor(ABC, Generic[Identity]):
    """Start, check and stop named processes for one deployment target."""

    @abstractmethod
    def start(self, spec: ProcessSpec) -> Identity:
        """Start the process and return the identity to record."""

    @abstractmethod
    def check(self, recorded: Identity) -> Observation[Identity]:
        """Compare a recorded identity with what runs now, without changing anything."""

    @abstractmethod
    def _terminate(self, recorded: Identity) -> None:
        """Stop a process that `check` found running, restarted or pending."""

    def _release(self, recorded: Identity) -> None:
        """Remove what the start left beside the process, once nothing runs under it."""

    def confirm(self, recorded: Identity, verify: Callable[[Identity], None]) -> Identity:
        """The identity to record now, after `verify` passes for a restarted process.

        `verify` is the caller's functional check, such as the sidecar verification or the
        router's `/health`. It raises when the restarted process fails it. A process that
        is not running, or that changes again during `verify`, raises `SupervisionError`.
        """
        observation = self.check(recorded)
        if observation.state == "running":
            return recorded
        if observation.state != "restarted" or observation.identity is None:
            raise SupervisionError(observation.detail)
        verify(observation.identity)
        again = self.check(observation.identity)
        if again.state != "running":
            raise SupervisionError(f"{observation.detail}; after its check, {again.detail}")
        return observation.identity

    def stop(self, recorded: Identity) -> bool:
        """Stop the recorded process; return False when nothing ran under its identity.

        Raises `IdentityMismatch` without signalling anything when something else holds
        the unit or process group.
        """
        observation = self.check(recorded)
        if observation.state == "mismatch":
            raise IdentityMismatch(observation.detail)
        running = observation.state != "stopped"
        if running:
            self._terminate(recorded)
        self._release(recorded)
        return running


class ProcessGroups(Supervisor[ProcessGroup]):
    """Detached process groups on this host, identified by the native engine tuple."""

    def __init__(self, grace_seconds: float = STOP_GRACE_SECONDS):
        self.grace_seconds = grace_seconds

    def start(self, spec: ProcessSpec) -> ProcessGroup:
        """Start the group with a fresh log, as `narwhal dev` starts its processes.

        A failed start raises the error that stopped it. When the group started but its
        identity could not be read, its members are stopped and `<name>-spawn.stage.json`
        beside the log records that cleanup, where `narwhal dev` looks for interrupted
        helpers.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with os.fdopen(os.open(spec.log, flags, 0o600), "w") as output:
            # An existing log keeps its mode through open, so it is made private here.
            os.fchmod(output.fileno(), 0o600)
            child = subprocess.Popen(
                spec.argv,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=spec.cwd,
                env={**os.environ, **spec.environment},
            )
        try:
            leader = native_engine.process_identity(child.pid)
        except BaseException as error:
            self._discard(child, spec, error)
            raise
        return ProcessGroup(
            pid=int(leader["pid"]),
            boot_id=str(leader["boot_id"]),
            start_ticks=int(leader["start_ticks"]),
        )

    @staticmethod
    def _discard(child: subprocess.Popen, spec: ProcessSpec, error: BaseException) -> None:
        # The group has no recorded identity yet, so its members are found as the stage
        # runner finds a helper's.
        owned: dict[int, int] = {}
        try:
            with stages._reaper():
                cleanup = stages._cleanup(
                    child,
                    owned,
                    stages.seconds("NARWHAL_STAGE_CLEANUP_GRACE_SECONDS", 10),
                    stages.seconds("NARWHAL_STAGE_KILL_GRACE_SECONDS", 5),
                )
            stages.write_evidence(
                spec.log.parent / f"{spec.name}-spawn.stage.json",
                {
                    "stage": spec.name,
                    "pid": child.pid,
                    "processes": owned,
                    "boot_id": Path(BOOT_ID).read_text().strip(),
                    "status": "cancelled",
                    "cleanup": cleanup,
                },
            )
        except (OSError, ValueError) as cleanup_error:
            error.add_note(f"{spec.name} cleanup: {cleanup_error}")

    def check(self, recorded: ProcessGroup) -> Observation[ProcessGroup]:
        leader = recorded.leader()
        if native_engine.owns_process(leader):
            return Observation("running", f"process group {recorded.pid} is running", recorded)
        if members := native_engine.group_members(leader):
            return Observation(
                "mismatch",
                f"process group {recorded.pid} lost its leader; surviving group PIDs "
                f"{sorted(members)} require operator inspection",
            )
        return Observation("stopped", f"process group {recorded.pid} is not running")

    def _terminate(self, recorded: ProcessGroup) -> None:
        try:
            native_engine.terminate(recorded.leader(), self.grace_seconds)
        except ValueError as error:
            raise SupervisionError(f"process group {recorded.pid}: {error}") from error


class Runner(Protocol):
    """Run a command on one host and return its exit status and text output.

    A missing executable exits 127, as it does under a shell, so the same caller works
    on this host and over SSH.
    """

    def __call__(
        self, argv: Sequence[str], *, input: str | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def run_local(argv: Sequence[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
    """`Runner` for this host."""
    try:
        return subprocess.run(
            list(argv),
            input=input,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{argv[0]}: {error.strerror}")


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[-2000:]


@dataclass(frozen=True)
class _UnitState:
    load_state: str
    active_state: str
    sub_state: str
    description: str
    invocation_id: str
    main_pid: int
    restarts: int
    boot_id: str
    # Read only while the unit runs its main process.
    start_ticks: int | None

    def describe(self) -> str:
        return f"{self.active_state}/{self.sub_state}"


def _description(nonce: str) -> str:
    return f"narwhal {nonce}"


class SystemdUnits(Supervisor[SystemdUnit]):
    """Transient systemd user units on one host, reached through `runner`.

    Each unit is `narwhal-<run_id>-<name>.service`, described as `narwhal <nonce>` with a
    nonce new to each start. systemd restarts it on failure and unloads it once it stops
    or fails. The unit reads its environment from `<name>.env` beside its log, a file
    private to the user that the start creates and `stop` removes, so that no variable
    value appears in an argument list.
    """

    def __init__(self, run_id: str, runner: Runner = run_local):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("run ID must be 32 lowercase hexadecimal characters")
        self.run_id = run_id
        self.runner = runner

    def unit(self, name: str) -> str:
        if not NAME.fullmatch(name):
            raise ValueError(f"process name {name!r} must match {NAME.pattern}")
        return f"narwhal-{self.run_id}-{name}.service"

    def start(self, spec: ProcessSpec) -> SystemdUnit:
        unit = self.unit(spec.name)
        if self._show(unit)["LoadState"] == "loaded":
            raise SupervisionError(f"{unit} is already loaded")
        nonce = secrets.token_hex(16)
        environment = spec.log.parent / f"{spec.name}.env"
        self._run(
            ["sh", "-c", _CREATE_PRIVATE, "sh", str(environment)],
            f"create {environment}",
            # The process reads its unit's restart count through the unit's name.
            input="".join(
                f'{key}="{value.translate(_ENVIRONMENT_ESCAPES)}"\n'
                for key, value in sorted({**spec.environment, UNIT_ENV: unit}.items())
            ),
        )
        try:
            self._run(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    f"--unit={unit}",
                    f"--description={_description(nonce)}",
                    f"--working-directory={spec.cwd}",
                    "--property=Type=exec",
                    "--property=Restart=on-failure",
                    f"--property=TimeoutStopSec={STOP_GRACE_SECONDS}",
                    "--property=UMask=0077",
                    f"--property=EnvironmentFile={environment}",
                    f"--property=StandardOutput=append:{spec.log}",
                    f"--property=StandardError=append:{spec.log}",
                    "--",
                    spec.argv[0],
                    # systemd expands $NAME in the arguments after the executable, and $$
                    # is a literal $.
                    *(argument.replace("$", "$$") for argument in spec.argv[1:]),
                ],
                f"start {unit}",
            )
            state = self._read(unit)
            if state.description != _description(nonce):
                raise SupervisionError(f"{unit} is not the unit this start created")
            if state.start_ticks is None or state.restarts:
                raise SupervisionError(
                    f"{unit} did not keep running after its start ({state.describe()}, "
                    f"{state.restarts} automatic restarts); inspect {spec.log}"
                )
        except BaseException as error:
            self._discard(unit, nonce, environment, error)
            raise
        return SystemdUnit(
            unit=unit,
            nonce=nonce,
            invocation_id=state.invocation_id,
            main_pid=state.main_pid,
            start_ticks=state.start_ticks,
            boot_id=state.boot_id,
            restarts=state.restarts,
            environment_file=str(environment),
        )

    def discard(self, name: str) -> bool:
        """Stop this run's unit `name` that no record names; return whether one was loaded.

        A start interrupted after `systemd-run` and before its caller recorded the identity
        leaves such a unit. Its name carries the run ID, so no other run's unit can match.
        """
        unit = self.unit(name)
        if self._show(unit)["LoadState"] != "loaded":
            return False
        self._stop_unit(unit)
        return True

    def _discard(self, unit: str, nonce: str, environment: Path, error: BaseException) -> None:
        """Undo a failed start: stop the unit it created, and remove its environment file."""
        try:
            values = self._show(unit)
            if values["LoadState"] == "loaded" and values["Description"] == _description(nonce):
                self._stop_unit(unit)
        except SupervisionError as cleanup_error:
            error.add_note(f"{unit} cleanup: {cleanup_error}")
        try:
            self._remove(str(environment))
        except SupervisionError as cleanup_error:
            error.add_note(f"{unit} cleanup: {cleanup_error}")

    def check(self, recorded: SystemdUnit) -> Observation[SystemdUnit]:
        unit = recorded.unit
        if not unit.startswith(f"narwhal-{self.run_id}-"):
            raise SupervisionError(f"{unit} does not belong to run {self.run_id}")
        state = self._read(unit)
        if state.load_state != "loaded" or state.active_state in {"inactive", "failed"}:
            return Observation("stopped", f"{unit} is not running; its output is in its log")
        if state.description != _description(recorded.nonce):
            return Observation(
                "mismatch",
                f"{unit} was started again after the record: it is described as "
                f"{state.description!r}, not {_description(recorded.nonce)!r}",
            )
        if state.boot_id != recorded.boot_id:
            return Observation(
                "mismatch",
                f"{unit} was recorded in boot {recorded.boot_id}, and a unit of that name "
                f"runs in boot {state.boot_id}",
            )
        restarted = state.invocation_id != recorded.invocation_id
        if restarted and state.restarts <= recorded.restarts:
            return Observation(
                "mismatch",
                f"{unit} runs invocation {state.invocation_id}, not the recorded "
                f"{recorded.invocation_id}, and systemd has not restarted it since",
            )
        if state.start_ticks is None:
            return Observation("pending", f"{unit} is {state.describe()}")
        if not restarted:
            if (state.main_pid, state.start_ticks) != (recorded.main_pid, recorded.start_ticks):
                return Observation(
                    "mismatch",
                    f"{unit} invocation {state.invocation_id} runs main PID {state.main_pid}, "
                    f"not the recorded {recorded.main_pid}",
                )
            return Observation("running", f"{unit} is running", recorded)
        current = replace(
            recorded,
            invocation_id=state.invocation_id,
            main_pid=state.main_pid,
            start_ticks=state.start_ticks,
            restarts=state.restarts,
        )
        return Observation(
            "restarted",
            f"systemd restarted {unit} since the record ({state.restarts - recorded.restarts} "
            f"automatic restarts); invocation {state.invocation_id} needs its check",
            current,
        )

    def _terminate(self, recorded: SystemdUnit) -> None:
        # `systemctl stop` takes a unit name, and systemd offers no stop conditional on an
        # invocation or description. A unit that someone replaces under this name between
        # `check` and this stop is stopped too. The name carries the run ID, so only a unit
        # replaced by hand, or by a second runner on the same run, can land in that window.
        self._stop_unit(recorded.unit)

    def _release(self, recorded: SystemdUnit) -> None:
        name = recorded.unit.removeprefix(f"narwhal-{self.run_id}-").removesuffix(".service")
        path = Path(recorded.environment_file)
        if not path.is_absolute() or path.name != f"{name}.env":
            raise SupervisionError(f"{recorded.unit} records {path} as its environment file")
        self._remove(str(path))

    def _stop_unit(self, unit: str) -> None:
        """Stop the unit; a unit that unloads first, as `--collect` does, counts as stopped."""
        stopped = self.runner(["systemctl", "--user", "stop", unit])
        state = self._show(unit)
        if state["LoadState"] != "loaded":
            return
        if stopped.returncode:
            raise SupervisionError(f"could not stop {unit}: {_output(stopped)}")
        if state["ActiveState"] == "failed":
            reset = self.runner(["systemctl", "--user", "reset-failed", unit])
            if reset.returncode and self._show(unit)["LoadState"] == "loaded":
                raise SupervisionError(f"could not reset the failed {unit}: {_output(reset)}")
        elif state["ActiveState"] != "inactive":
            raise SupervisionError(
                f"{unit} is {state['ActiveState']}/{state['SubState']} after its stop"
            )

    def _remove(self, path: str) -> None:
        self._run(["rm", "-f", "--", path], f"remove {path}")

    def _run(
        self, argv: list[str], action: str, *, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(argv, input=input)
        if result.returncode:
            raise SupervisionError(f"could not {action}: {_output(result)}")
        return result

    def _show(self, unit: str) -> dict[str, str]:
        result = self._run(
            ["systemctl", "--user", "show", unit, *(f"--property={p}" for p in _UNIT_PROPERTIES)],
            f"read {unit}",
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if missing := [name for name in _UNIT_PROPERTIES if name not in values]:
            raise SupervisionError(f"systemctl did not report {', '.join(missing)} for {unit}")
        return values

    def _read(self, unit: str) -> _UnitState:
        """Read the unit, its boot and its main process's start ticks as one state.

        The unit is read again after the main process, and a change between the two reads
        raises, so the start ticks belong to the reported invocation.
        """
        first = self._show(unit)
        boot_id = self._run(["cat", BOOT_ID], "read the boot ID")
        try:
            main_pid, restarts = int(first["MainPID"]), int(first["NRestarts"])
        except ValueError as error:
            raise SupervisionError(f"systemctl reported {unit} with {error}") from error
        start_ticks = None
        if first["ActiveState"] == "active" and first["SubState"] == "running" and main_pid > 0:
            stat = self.runner(["cat", f"/proc/{main_pid}/stat"])
            fields = stat.stdout.rsplit(")", 1)[-1].split()
            if stat.returncode == 0 and len(fields) >= 20 and fields[0] not in {"Z", "X"}:
                start_ticks = int(fields[19])
            if self._show(unit) != first or start_ticks is None:
                raise SupervisionError(f"{unit} changed while its identity was read; check again")
        return _UnitState(
            load_state=first["LoadState"],
            active_state=first["ActiveState"],
            sub_state=first["SubState"],
            description=first["Description"],
            invocation_id=first["InvocationID"],
            main_pid=main_pid,
            restarts=restarts,
            boot_id=boot_id.stdout.strip(),
            start_ticks=start_ticks,
        )


def _systemd_version(reported: str) -> int | None:
    match = re.match(r"(?:systemd )?(\d+)", reported.strip())
    return int(match.group(1)) if match else None


def check_systemd_host(runner: Runner = run_local) -> list[str]:
    """What keeps a host from running supervised units, read-only and without root.

    Each entry names one missing piece: `systemd-run` and a user manager from systemd 240
    or later, a user manager that answers `systemctl --user`, or lingering for the user
    the commands run as, which keeps that manager and its units running after the
    session ends. An empty list means the host is ready.
    """
    problems = []
    client = runner(["systemd-run", "--version"])
    if client.returncode == 127:
        problems.append(f"systemd-run is not installed; install systemd {SYSTEMD_VERSION} or later")
    elif client.returncode:
        problems.append(f"systemd-run --version failed: {_output(client)}")
    else:
        reported = next(iter(client.stdout.strip().splitlines()), "")
        version = _systemd_version(reported) if reported.startswith("systemd ") else None
        if version is None or version < SYSTEMD_VERSION:
            problems.append(
                f"systemd-run reports {reported!r}; supervised units need systemd "
                f"{SYSTEMD_VERSION} or later"
            )
    manager = runner(["systemctl", "--user", "is-system-running"])
    answer = manager.stdout.strip()
    # A degraded manager answers and starts units; another unit of the user's has failed.
    if answer not in {"running", "degraded"}:
        problems.append(
            "user manager: systemctl --user is-system-running answered "
            f"{answer or _output(manager)!r}; the user needs a running systemd user manager, "
            "and a non-interactive SSH session needs XDG_RUNTIME_DIR, which pam_systemd sets"
        )
    else:
        shown = runner(["systemctl", "--user", "show", "--property=Version", "--value"])
        version = _systemd_version(shown.stdout) if shown.returncode == 0 else None
        if version is None or version < SYSTEMD_VERSION:
            problems.append(
                f"user manager reports version {shown.stdout.strip() or _output(shown)!r}; "
                f"supervised units need systemd {SYSTEMD_VERSION} or later"
            )
    user = runner(["id", "-un"])
    name = user.stdout.strip()
    if user.returncode or not name:
        problems.append(f"lingering: could not read the user name: {_output(user)}")
        return problems
    linger = runner(["loginctl", "show-user", name, "--property=Linger", "--value"])
    if linger.returncode:
        problems.append(f"lingering: loginctl show-user {name} failed: {_output(linger)}")
    elif linger.stdout.strip() != "yes":
        problems.append(
            f"lingering is off for {name}, so its units stop when its last session ends; "
            f"enable it with loginctl enable-linger {name}"
        )
    return problems
