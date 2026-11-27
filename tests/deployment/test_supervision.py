import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import native_engine
from narwhal.deployment.supervision import (
    IdentityMismatch,
    ProcessGroup,
    ProcessGroups,
    ProcessSpec,
    SupervisionError,
    SystemdUnit,
    SystemdUnits,
    check_systemd_host,
    identity_from_record,
    run_local,
)

from .fixtures import process_group_with_worker

RUN = "0123456789abcdef0123456789abcdef"
UNIT = f"narwhal-{RUN}-router.service"
BOOT = "boot-1"


class FakeUserManager:
    """A systemd 255 user manager, driven through the `Runner` interface.

    Files are real: the environment file commands run on this host, so tests see the
    mode and contents that a host would.
    """

    def __init__(self):
        self.boot_id = BOOT
        self.units: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.next_pid = 4000
        self.next_invocation = 0
        # Called after each `systemctl show`, to change the unit between two reads.
        self.after_show = None
        # When set, systemd-run creates the unit and then fails as a missing working
        # directory makes it fail: the unit stays loaded and cycles through restarts.
        self.start_failure = None
        # Called before `systemctl stop` acts, to change the unit first.
        self.before_stop = None

    def __call__(self, argv, *, input=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] in {"sh", "rm"}:
            return run_local(argv, input=input)
        if argv[:2] == ["systemd-run", "--user"]:
            unit = self.option(argv, "--unit")
            if unit in self.units:
                return self.result(argv, 1, stderr=f"Unit {unit} was already loaded")
            self.units[unit] = {
                "argv": argv,
                "restarts": 0,
                "description": self.option(argv, "--description"),
            }
            self.launch(unit)
            if self.start_failure is not None:
                self.restart(unit)
                self.crash(unit)
                return self.result(argv, 1, stderr=self.start_failure)
            return self.result(argv)
        if argv[:3] == ["systemctl", "--user", "show"]:
            output = self.show(argv[3])
            if self.after_show is not None:
                self.after_show()
            return self.result(argv, stdout=output)
        if argv[:3] == ["systemctl", "--user", "stop"]:
            if self.before_stop is not None:
                self.before_stop()
            if self.units.pop(argv[3], None) is None:
                return self.result(argv, 5, stderr=f"Unit {argv[3]} not loaded.")
            return self.result(argv)
        if argv == ["cat", "/proc/sys/kernel/random/boot_id"]:
            return self.result(argv, stdout=self.boot_id + "\n")
        if argv[0] == "cat" and argv[1].startswith("/proc/"):
            pid = int(argv[1].split("/")[2])
            unit = next((u for u in self.units.values() if u.get("pid") == pid), None)
            if unit is None:
                return self.result(argv, 1, stderr="No such file or directory")
            fields = f"{pid} (python) S 1 {pid} {pid} 0 -1 " + "0 " * 13
            return self.result(argv, stdout=fields + f"{unit['ticks']} 0\n")
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def option(argv, name):
        return next(a.split("=", 1)[1] for a in argv if a.startswith(name + "="))

    @staticmethod
    def result(argv, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def launch(self, unit):
        self.next_pid += 1
        self.next_invocation += 1
        self.units[unit].update(
            pid=self.next_pid,
            ticks=self.next_pid * 10,
            invocation=f"{self.next_invocation:032x}",
            active="active",
            sub="running",
        )

    def crash(self, unit):
        """The main process exits and systemd waits to restart it."""
        self.units[unit].update(pid=0, active="activating", sub="auto-restart")

    def restart(self, unit):
        """systemd's automatic restart: a new invocation and main process."""
        self.units[unit]["restarts"] += 1
        self.launch(unit)

    def recreate(self, unit, description="narwhal another-start"):
        """Someone stops the unit and starts a new one under the same name."""
        self.units[unit] = {"argv": [], "restarts": 0, "description": description}
        self.launch(unit)

    def show(self, unit):
        state = self.units.get(unit)
        if state is None:
            values = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}
            values.update(Description=unit, InvocationID="", MainPID="0", NRestarts="0")
        else:
            values = {
                "LoadState": "loaded",
                "ActiveState": state["active"],
                "SubState": state["sub"],
                "Description": state["description"],
                "InvocationID": state["invocation"],
                "MainPID": str(state["pid"]),
                "NRestarts": str(state["restarts"]),
            }
        return "".join(f"{key}={value}\n" for key, value in values.items())

    def commands(self, prefix):
        return [call for call in self.calls if call[: len(prefix)] == prefix]


class SystemdUnitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.environment_file = self.directory / "router.env"
        self.manager = FakeUserManager()
        self.supervisor = SystemdUnits(RUN, self.manager)

    def spec(self, **overrides):
        values = {
            "name": "router",
            "argv": ("/opt/narwhal/bin/python", "-m", "narwhal.cli", "--fleet", "fleet.json"),
            "cwd": self.directory,
            "log": self.directory / "router.log",
            "environment": {"NARWHAL_ENGINE_API_KEY": 'secret"$`\\value'},
        }
        values.update(overrides)
        return ProcessSpec(**values)

    def stops(self):
        return self.manager.commands(["systemctl", "--user", "stop"])

    def test_start_records_the_unit_nonce_invocation_main_process_and_boot(self):
        identity = self.supervisor.start(self.spec())
        state = self.manager.units[UNIT]
        self.assertEqual(state["description"], f"narwhal {identity.nonce}")
        self.assertEqual(len(identity.nonce), 32)
        self.assertEqual(
            identity,
            SystemdUnit(
                unit=UNIT,
                nonce=identity.nonce,
                invocation_id=state["invocation"],
                main_pid=state["pid"],
                start_ticks=state["ticks"],
                boot_id=BOOT,
                restarts=0,
                environment_file=str(self.environment_file),
            ),
        )
        argv = state["argv"]
        for option in (
            "--collect",
            "--property=Type=exec",
            "--property=Restart=on-failure",
            "--property=UMask=0077",
            f"--working-directory={self.directory}",
            f"--property=EnvironmentFile={self.environment_file}",
            f"--property=StandardOutput=append:{self.directory}/router.log",
        ):
            self.assertIn(option, argv)
        self.assertNotIn("--quiet", argv)
        self.assertEqual(argv[argv.index("--") + 1 :], list(self.spec().argv))

    def test_each_start_gets_a_new_nonce(self):
        first = self.supervisor.start(self.spec())
        self.supervisor.stop(first)
        second = self.supervisor.start(self.spec())
        self.assertNotEqual(first.nonce, second.nonce)

    def test_the_environment_file_is_private_and_no_value_reaches_an_argument_list(self):
        self.supervisor.start(self.spec())
        self.assertEqual(
            self.environment_file.read_text(),
            'NARWHAL_ENGINE_API_KEY="secret\\"\\$\\`\\\\value"\n',
        )
        self.assertEqual(stat.S_IMODE(self.environment_file.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["router.env"])
        self.assertFalse(
            any("secret" in argument for call in self.manager.calls for argument in call)
        )

    def test_start_refuses_an_existing_environment_path(self):
        target = self.directory / "elsewhere"
        folder = self.directory / "folder"
        folder.mkdir()
        cases = {
            "file": (lambda: self.environment_file.write_text("KEEP=1\n"), Path.unlink),
            "link to a missing file": (
                lambda: self.environment_file.symlink_to(target),
                Path.unlink,
            ),
            "link to a directory": (
                lambda: self.environment_file.symlink_to(folder, target_is_directory=True),
                Path.unlink,
            ),
            "directory": (lambda: self.environment_file.mkdir(), Path.rmdir),
        }
        for case, (prepare, remove) in cases.items():
            with self.subTest(case=case):
                prepare()
                with self.assertRaisesRegex(SupervisionError, "could not create"):
                    self.supervisor.start(self.spec())
                self.assertEqual(self.manager.commands(["systemd-run"]), [])
                self.assertFalse(target.exists())
                self.assertEqual(list(folder.iterdir()), [])
                if self.environment_file.is_dir() and not self.environment_file.is_symlink():
                    self.assertEqual(list(self.environment_file.iterdir()), [])
                remove(self.environment_file)
                self.assertEqual([p.name for p in self.directory.iterdir()], ["folder"])

    def test_arguments_keep_a_literal_dollar_sign(self):
        self.supervisor.start(self.spec(argv=("/bin/a$b", "--name", "$HOME")))
        argv = self.manager.units[UNIT]["argv"]
        self.assertEqual(argv[argv.index("--") + 1 :], ["/bin/a$b", "--name", "$$HOME"])

    def test_start_refuses_a_loaded_unit(self):
        self.supervisor.start(self.spec())
        with self.assertRaisesRegex(SupervisionError, "already loaded"):
            self.supervisor.start(self.spec())
        self.assertEqual(len(self.manager.commands(["systemd-run"])), 1)
        self.assertTrue(self.environment_file.exists())

    def test_a_failed_systemd_run_stops_the_unit_it_left_restarting(self):
        self.manager.start_failure = "Job failed because the control process exited"
        with self.assertRaisesRegex(SupervisionError, "control process exited"):
            self.supervisor.start(self.spec())
        self.assertEqual(self.stops(), [["systemctl", "--user", "stop", UNIT]])
        self.assertNotIn(UNIT, self.manager.units)
        self.assertFalse(self.environment_file.exists())

    def test_start_stops_a_unit_that_exits_at_once(self):
        def run(argv, *, input=None):
            result = self.manager(argv, input=input)
            if argv[0] == "systemd-run":
                self.manager.crash(UNIT)
            return result

        with self.assertRaisesRegex(SupervisionError, "activating/auto-restart"):
            SystemdUnits(RUN, run).start(self.spec())
        self.assertEqual(self.stops(), [["systemctl", "--user", "stop", UNIT]])
        self.assertFalse(self.environment_file.exists())

    def test_a_failed_start_leaves_a_unit_it_did_not_create(self):
        def run(argv, *, input=None):
            if argv[0] == "systemd-run":
                self.manager.recreate(UNIT)
                return FakeUserManager.result(argv, 1, stderr="Unit was already loaded")
            return self.manager(argv, input=input)

        with self.assertRaisesRegex(SupervisionError, "already loaded"):
            SystemdUnits(RUN, run).start(self.spec())
        self.assertEqual(self.stops(), [])
        self.assertIn(UNIT, self.manager.units)

    def test_check_finds_the_recorded_invocation_running(self):
        identity = self.supervisor.start(self.spec())
        observation = self.supervisor.check(identity)
        self.assertEqual((observation.state, observation.identity), ("running", identity))

    def test_check_reads_the_new_identity_after_an_automatic_restart(self):
        """systemd gives each automatic restart a new InvocationID and main PID.

        Checked against systemd 255 with `systemd-run --user -p Restart=on-failure` and a
        command that exits 3 after two seconds. `systemctl --user show -p
        InvocationID,MainPID,NRestarts` read, at one-second intervals:

            invocation A, MainPID 787956, NRestarts 0, active/running
            invocation A, MainPID 0,      NRestarts 0, activating/auto-restart
            invocation B, MainPID 788193, NRestarts 1, active/running
            invocation C, MainPID 788294, NRestarts 2, active/running

        During the restart delay the unit keeps the old InvocationID with no main process.
        The description, and so the nonce, stays the same across restarts.
        """
        identity = self.supervisor.start(self.spec())
        self.manager.crash(UNIT)
        self.assertEqual(self.supervisor.check(identity).state, "pending")
        self.manager.restart(UNIT)
        observation = self.supervisor.check(identity)
        state = self.manager.units[UNIT]
        self.assertEqual(observation.state, "restarted")
        self.assertEqual(
            observation.identity.record(),
            {
                **identity.record(),
                "invocation_id": state["invocation"],
                "main_pid": state["pid"],
                "start_ticks": state["ticks"],
                "restarts": 1,
            },
        )

    def test_confirm_records_a_restart_only_after_the_functional_check(self):
        identity = self.supervisor.start(self.spec())
        self.manager.restart(UNIT)
        checked = []
        confirmed = self.supervisor.confirm(identity, checked.append)
        self.assertEqual(checked, [confirmed])
        self.assertEqual(confirmed.restarts, 1)
        self.assertNotEqual(confirmed.invocation_id, identity.invocation_id)
        self.assertEqual(self.supervisor.check(confirmed).state, "running")

    def test_confirm_keeps_a_running_record_without_a_functional_check(self):
        identity = self.supervisor.start(self.spec())
        self.assertIs(self.supervisor.confirm(identity, self.fail), identity)

    def test_confirm_refuses_a_restart_that_fails_its_check(self):
        identity = self.supervisor.start(self.spec())
        self.manager.restart(UNIT)

        def unhealthy(identity):
            raise ValueError("router /health returned 503")

        with self.assertRaisesRegex(ValueError, "503"):
            self.supervisor.confirm(identity, unhealthy)

    def test_confirm_refuses_a_unit_that_restarts_again_during_its_check(self):
        identity = self.supervisor.start(self.spec())
        self.manager.restart(UNIT)
        with self.assertRaisesRegex(SupervisionError, "after its check"):
            self.supervisor.confirm(identity, lambda new: self.manager.restart(UNIT))

    def test_confirm_refuses_a_unit_waiting_to_restart(self):
        identity = self.supervisor.start(self.spec())
        self.manager.crash(UNIT)
        with self.assertRaisesRegex(SupervisionError, "auto-restart"):
            self.supervisor.confirm(identity, self.fail)

    def test_a_record_cannot_reach_a_recreated_unit_after_its_restarts(self):
        identity = self.supervisor.start(self.spec())
        self.supervisor.stop(identity)
        self.manager.recreate(UNIT)
        self.manager.restart(UNIT)
        self.manager.restart(UNIT)
        observation = self.supervisor.check(identity)
        self.assertEqual(observation.state, "mismatch")
        self.assertIn("started again", observation.detail)
        with self.assertRaisesRegex(SupervisionError, "started again"):
            self.supervisor.confirm(identity, self.fail)
        with self.assertRaises(IdentityMismatch):
            self.supervisor.stop(identity)
        self.assertEqual(len(self.stops()), 1)
        self.assertIn(UNIT, self.manager.units)

    def test_a_manual_restart_is_a_mismatch(self):
        identity = self.supervisor.start(self.spec())
        self.manager.launch(UNIT)
        observation = self.supervisor.check(identity)
        self.assertEqual(observation.state, "mismatch")
        self.assertIn("has not restarted it", observation.detail)
        with self.assertRaises(IdentityMismatch):
            self.supervisor.stop(identity)
        self.assertEqual(self.stops(), [])
        self.assertTrue(self.environment_file.exists())

    def test_a_record_from_another_boot_cannot_stop_the_unit(self):
        identity = self.supervisor.start(self.spec())
        self.manager.boot_id = "boot-2"
        with self.assertRaisesRegex(IdentityMismatch, "boot-1"):
            self.supervisor.stop(identity)
        self.assertEqual(self.stops(), [])

    def test_a_changed_main_process_is_a_mismatch(self):
        identity = self.supervisor.start(self.spec())
        self.manager.units[UNIT]["ticks"] += 1
        self.assertEqual(self.supervisor.check(identity).state, "mismatch")

    def test_stop_compares_the_identity_and_removes_the_environment_file(self):
        identity = self.supervisor.start(self.spec())
        self.assertTrue(self.supervisor.stop(identity))
        self.assertEqual(self.stops(), [["systemctl", "--user", "stop", UNIT]])
        self.assertFalse(self.environment_file.exists())
        self.assertEqual(self.supervisor.check(identity).state, "stopped")
        self.assertFalse(self.supervisor.stop(identity))
        self.assertEqual(len(self.stops()), 1)

    def test_stop_removes_the_environment_file_of_a_unit_that_already_ended(self):
        identity = self.supervisor.start(self.spec())
        del self.manager.units[UNIT]
        self.assertFalse(self.supervisor.stop(identity))
        self.assertFalse(self.environment_file.exists())

    def test_a_unit_that_unloads_before_its_stop_counts_as_stopped(self):
        identity = self.supervisor.start(self.spec())
        self.manager.before_stop = lambda: self.manager.units.pop(UNIT, None)
        self.assertTrue(self.supervisor.stop(identity))
        self.assertEqual(self.stops(), [["systemctl", "--user", "stop", UNIT]])
        self.assertFalse(self.environment_file.exists())

    def test_stop_reaches_a_unit_that_systemd_restarted(self):
        identity = self.supervisor.start(self.spec())
        self.manager.restart(UNIT)
        self.assertTrue(self.supervisor.stop(identity))
        self.assertNotIn(UNIT, self.manager.units)

    def test_discard_stops_an_unrecorded_unit_of_this_run_only(self):
        self.supervisor.start(self.spec())
        other = SystemdUnits("f" * 32, self.manager)
        self.assertFalse(other.discard("router"))
        self.assertIn(UNIT, self.manager.units)
        self.assertTrue(self.supervisor.discard("router"))
        self.assertEqual(self.stops(), [["systemctl", "--user", "stop", UNIT]])
        self.assertNotIn(UNIT, self.manager.units)
        self.assertFalse(self.supervisor.discard("router"))

    def test_a_unit_of_another_run_is_refused(self):
        identity = SystemdUnits("f" * 32, self.manager).start(self.spec())
        with self.assertRaisesRegex(SupervisionError, "does not belong to run"):
            self.supervisor.stop(identity)
        self.assertEqual(self.stops(), [])

    def test_a_unit_that_changes_between_reads_raises(self):
        identity = self.supervisor.start(self.spec())
        shows = []

        def restart_after_the_first_read():
            shows.append(None)
            if len(shows) == 1:
                self.manager.restart(UNIT)

        self.manager.after_show = restart_after_the_first_read
        with self.assertRaisesRegex(SupervisionError, "changed while its identity was read"):
            self.supervisor.check(identity)

    def test_names_and_run_ids_fit_a_unit_name(self):
        with self.assertRaisesRegex(ValueError, "process name"):
            self.spec(name="Router A")
        with self.assertRaisesRegex(ValueError, "run ID"):
            SystemdUnits("run-1", self.manager)
        with self.assertRaisesRegex(ValueError, "one line"):
            self.spec(environment={"KEY": "two\nlines"})


class IdentityRecordTests(unittest.TestCase):
    unit = SystemdUnit(UNIT, "b" * 32, "a" * 32, 10, 20, BOOT, 2, "/srv/run/router.env")

    def test_records_round_trip(self):
        for identity in (ProcessGroup(10, BOOT, 20), self.unit):
            self.assertEqual(identity_from_record(identity.record()), identity)

    def test_malformed_records_are_refused(self):
        record = self.unit.record()
        for change in (
            {"kind": "container"},
            {"main_pid": 0},
            {"boot_id": ""},
            {"restarts": "1"},
            {"nonce": ""},
        ):
            with self.subTest(change=change), self.assertRaises(SupervisionError):
                identity_from_record({**record, **change})


class SystemdHostCheckTests(unittest.TestCase):
    def runner(self, **answers):
        replies = {
            ("systemd-run", "--version"): (0, "systemd 255 (255.4-1)\n+PAM +AUDIT\n", ""),
            ("systemctl", "--user", "is-system-running"): (0, "running\n", ""),
            ("systemctl", "--user", "show"): (0, "255.4-1ubuntu8.18\n", ""),
            ("id", "-un"): (0, "deploy\n", ""),
            ("loginctl", "show-user"): (0, "yes\n", ""),
        }
        names = {
            "client": ("systemd-run", "--version"),
            "state": ("systemctl", "--user", "is-system-running"),
            "version": ("systemctl", "--user", "show"),
            "user": ("id", "-un"),
            "linger": ("loginctl", "show-user"),
        }
        replies.update({names[name]: answer for name, answer in answers.items()})
        calls = []

        def run(argv, *, input=None):
            calls.append(list(argv))
            key = next(k for k in replies if tuple(argv[: len(k)]) == k)
            code, stdout, stderr = replies[key]
            return subprocess.CompletedProcess(argv, code, stdout, stderr)

        return run, calls

    def test_a_ready_host_reports_nothing_and_runs_only_reads(self):
        run, calls = self.runner()
        self.assertEqual(check_systemd_host(run), [])
        self.assertEqual(
            calls,
            [
                ["systemd-run", "--version"],
                ["systemctl", "--user", "is-system-running"],
                ["systemctl", "--user", "show", "--property=Version", "--value"],
                ["id", "-un"],
                ["loginctl", "show-user", "deploy", "--property=Linger", "--value"],
            ],
        )

    def test_a_degraded_user_manager_still_answers(self):
        run, _ = self.runner(state=(1, "degraded\n", ""))
        self.assertEqual(check_systemd_host(run), [])

    def test_each_missing_piece_is_named(self):
        cases = {
            "systemd-run is not installed": {"client": (127, "", "not found")},
            "systemd 239": {"client": (0, "systemd 239\n", "")},
            "Failed to connect to bus": {"state": (1, "", "Failed to connect to bus")},
            "answered 'offline'": {"state": (1, "offline\n", "")},
            "user manager reports version '239'": {"version": (0, "239\n", "")},
            "user manager reports version 'Access denied'": {"version": (1, "", "Access denied")},
            "lingering is off for deploy": {"linger": (0, "no\n", "")},
            "loginctl show-user deploy failed": {"linger": (1, "", "not logged in")},
            "could not read the user name": {"user": (1, "", "no user")},
        }
        for expected, answers in cases.items():
            with self.subTest(expected=expected):
                run, _ = self.runner(**answers)
                problems = check_systemd_host(run)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(expected, problems[0])
        run, _ = self.runner(linger=(0, "no\n", ""))
        self.assertIn("loginctl enable-linger deploy", check_systemd_host(run)[0])

    def test_all_missing_pieces_are_reported_together(self):
        run, _ = self.runner(client=(127, "", ""), linger=(0, "no\n", ""))
        self.assertEqual(len(check_systemd_host(run)), 2)

    def test_the_local_runner_reports_a_missing_executable_as_a_shell_does(self):
        result = run_local(["narwhal-no-such-command", "--version"])
        self.assertEqual(result.returncode, 127)


class ProcessGroupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.supervisor = ProcessGroups(grace_seconds=0.05)

    def spec(self, script, **environment):
        return ProcessSpec(
            name="sidecar-1",
            argv=(sys.executable, "-c", script),
            cwd=self.directory,
            log=self.directory / "sidecar-1.log",
            environment=environment,
        )

    def start(self, script="import time; time.sleep(60)", **environment):
        identity = self.supervisor.start(self.spec(script, **environment))
        self.addCleanup(self.reap, identity)
        return identity

    def reap(self, identity):
        if native_engine.group_members(identity.leader()):
            native_engine.terminate(identity.leader(), grace_seconds=0.05)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(identity.pid, 0)

    def test_start_records_the_native_identity_of_a_detached_group(self):
        identity = self.start()
        self.assertEqual(identity.leader(), native_engine.process_identity(identity.pid))
        self.assertEqual(os.getsid(identity.pid), identity.pid)
        observation = self.supervisor.check(identity)
        self.assertEqual((observation.state, observation.identity), ("running", identity))

    def test_start_runs_in_the_directory_with_the_environment_and_a_fresh_private_log(self):
        script = "import os; print(os.getcwd(), os.environ['NARWHAL_CHECK'], flush=True)"
        log = self.directory / "sidecar-1.log"
        log.write_text("earlier\n")
        identity = self.start(script, NARWHAL_CHECK="value")
        deadline = time.monotonic() + 5
        while "value" not in log.read_text() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(log.read_text(), f"{self.directory} value\n")
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.reap(identity)
        self.assertEqual(self.supervisor.check(identity).state, "stopped")

    def test_start_raises_the_error_of_a_missing_executable(self):
        spec = ProcessSpec(
            "sidecar-1", ("/nonexistent/python",), self.directory, self.directory / "log"
        )
        with self.assertRaises(FileNotFoundError):
            self.supervisor.start(spec)

    def test_start_stops_a_group_whose_identity_cannot_be_read(self):
        started = []
        real = subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        with (
            patch("narwhal.deployment.supervision.subprocess.Popen", side_effect=popen),
            patch.object(native_engine, "process_identity", side_effect=ValueError("exited")),
            self.assertRaisesRegex(ValueError, "exited"),
        ):
            self.supervisor.start(self.spec("import time; time.sleep(60)"))
        self.assertIsNotNone(started[0].wait(timeout=5))
        record = json.loads((self.directory / "sidecar-1-spawn.stage.json").read_text())
        self.assertEqual(
            (record["stage"], record["pid"], record["status"]),
            ("sidecar-1", started[0].pid, "cancelled"),
        )
        self.assertEqual(record["cleanup"]["surviving_processes"], {})

    def test_stop_signals_the_matching_group_only_once(self):
        identity = self.start()
        self.assertTrue(self.supervisor.stop(identity))
        self.assertEqual(native_engine.group_members(identity.leader()), {})
        self.assertEqual(self.supervisor.check(identity).state, "stopped")
        self.assertFalse(self.supervisor.stop(identity))

    def test_a_stale_record_leaves_a_reused_pid_alone(self):
        identity = self.start()
        stale = ProcessGroup(identity.pid, identity.boot_id, identity.start_ticks + 1)
        self.assertEqual(self.supervisor.check(stale).state, "stopped")
        self.assertFalse(self.supervisor.stop(stale))
        self.assertTrue(native_engine.owns_process(identity.leader()))
        with self.assertRaisesRegex(SupervisionError, "not running"):
            self.supervisor.confirm(stale, self.fail)

    def test_a_group_that_lost_its_leader_is_refused(self):
        with process_group_with_worker() as (leader, worker):
            identity = ProcessGroup(**native_engine.process_identity(leader.pid))
            leader.terminate()
            leader.wait(timeout=5)
            observation = self.supervisor.check(identity)
            self.assertEqual(observation.state, "mismatch")
            self.assertIn(str(worker), observation.detail)
            with self.assertRaises(IdentityMismatch):
                self.supervisor.stop(identity)
            os.kill(worker, 0)

    def test_confirm_keeps_a_running_group(self):
        identity = self.start()
        self.assertIs(self.supervisor.confirm(identity, self.fail), identity)


if __name__ == "__main__":
    unittest.main()
