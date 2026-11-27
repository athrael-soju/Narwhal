import contextlib
import os
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
    """A systemd user manager and host, driven through the `Runner` interface."""

    def __init__(self):
        self.boot_id = BOOT
        self.units: dict[str, dict] = {}
        self.files: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.next_pid = 4000
        self.next_invocation = 0
        # Called after each `systemctl show`, to change the unit between two reads.
        self.after_show = None

    def __call__(self, argv, *, input=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["systemd-run", "--user"]:
            unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
            if unit in self.units:
                return self.result(argv, 1, stderr=f"Unit {unit} was already loaded")
            self.units[unit] = {"argv": argv, "restarts": 0}
            self.launch(unit)
            return self.result(argv)
        if argv[:3] == ["systemctl", "--user", "show"]:
            output = self.show(argv[3])
            if self.after_show is not None:
                self.after_show()
            return self.result(argv, stdout=output)
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.units.pop(argv[3], None)
            return self.result(argv)
        if argv == ["cat", "/proc/sys/kernel/random/boot_id"]:
            return self.result(argv, stdout=self.boot_id + "\n")
        if argv[0] == "cat" and argv[1].startswith("/proc/"):
            pid = int(argv[1].split("/")[2])
            unit = next((u for u in self.units.values() if u.get("pid") == pid), None)
            if unit is None:
                return self.result(argv, 1, stderr="No such file or directory")
            return self.result(
                argv,
                stdout=f"{pid} (python) S 1 {pid} {pid} 0 -1 " + "0 " * 13 + f"{unit['ticks']} 0\n",
            )
        if argv[:2] == ["sh", "-c"]:
            self.files[argv[4]] = input
            return self.result(argv)
        raise AssertionError(f"unexpected command {argv}")

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

    def show(self, unit):
        state = self.units.get(unit)
        if state is None:
            values = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}
            values.update(InvocationID="", MainPID="0", NRestarts="0")
        else:
            values = {
                "LoadState": "loaded",
                "ActiveState": state["active"],
                "SubState": state["sub"],
                "InvocationID": state["invocation"],
                "MainPID": str(state["pid"]),
                "NRestarts": str(state["restarts"]),
            }
        return "".join(f"{key}={value}\n" for key, value in values.items())

    def commands(self, prefix):
        return [call for call in self.calls if call[: len(prefix)] == prefix]


def spec(directory="/srv/narwhal/run", **overrides):
    values = {
        "name": "router",
        "argv": ("/opt/narwhal/bin/python", "-m", "narwhal.cli", "--fleet", "fleet.json"),
        "cwd": Path(directory),
        "log": Path(directory) / "router.log",
        "environment": {"NARWHAL_ENGINE_API_KEY": 'secret"$`\\value'},
    }
    values.update(overrides)
    return ProcessSpec(**values)


class SystemdUnitTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeUserManager()
        self.supervisor = SystemdUnits(RUN, self.manager)

    def test_start_records_the_unit_invocation_main_process_and_boot(self):
        identity = self.supervisor.start(spec())
        state = self.manager.units[UNIT]
        self.assertEqual(
            identity,
            SystemdUnit(UNIT, state["invocation"], state["pid"], state["ticks"], BOOT, 0),
        )
        argv = state["argv"]
        for option in (
            "--collect",
            "--property=Type=exec",
            "--property=Restart=on-failure",
            "--property=UMask=0077",
            "--working-directory=/srv/narwhal/run",
            "--property=EnvironmentFile=/srv/narwhal/run/router.env",
            "--property=StandardOutput=append:/srv/narwhal/run/router.log",
        ):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index("--") + 1 :], list(spec().argv))

    def test_environment_values_reach_a_private_file_and_no_argument_list(self):
        self.supervisor.start(spec())
        self.assertEqual(
            self.manager.files["/srv/narwhal/run/router.env"],
            'NARWHAL_ENGINE_API_KEY="secret\\"\\$\\`\\\\value"\n',
        )
        writer = self.manager.commands(["sh", "-c"])[0]
        self.assertIn("umask 077", writer[2])
        self.assertFalse(
            any("secret" in argument for call in self.manager.calls for argument in call)
        )

    def test_arguments_keep_a_literal_dollar_sign(self):
        self.supervisor.start(spec(argv=("/bin/a$b", "--name", "$HOME")))
        argv = self.manager.units[UNIT]["argv"]
        self.assertEqual(argv[argv.index("--") + 1 :], ["/bin/a$b", "--name", "$$HOME"])

    def test_start_refuses_a_loaded_unit(self):
        self.supervisor.start(spec())
        with self.assertRaisesRegex(SupervisionError, "already loaded"):
            self.supervisor.start(spec())
        self.assertEqual(len(self.manager.commands(["systemd-run"])), 1)

    def test_start_stops_a_unit_that_exits_at_once(self):
        def run(argv, *, input=None):
            result = self.manager(argv, input=input)
            if argv[0] == "systemd-run":
                self.manager.crash(UNIT)
            return result

        with self.assertRaisesRegex(SupervisionError, "activating/auto-restart"):
            SystemdUnits(RUN, run).start(spec())
        self.assertEqual(
            self.manager.commands(["systemctl", "--user", "stop"]),
            [["systemctl", "--user", "stop", UNIT]],
        )

    def test_check_finds_the_recorded_invocation_running(self):
        identity = self.supervisor.start(spec())
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
        """
        identity = self.supervisor.start(spec())
        self.manager.crash(UNIT)
        self.assertEqual(self.supervisor.check(identity).state, "pending")
        self.manager.restart(UNIT)
        observation = self.supervisor.check(identity)
        state = self.manager.units[UNIT]
        self.assertEqual(observation.state, "restarted")
        self.assertEqual(
            observation.identity,
            SystemdUnit(UNIT, state["invocation"], state["pid"], state["ticks"], BOOT, 1),
        )

    def test_confirm_records_a_restart_only_after_the_functional_check(self):
        identity = self.supervisor.start(spec())
        self.manager.restart(UNIT)
        checked = []
        confirmed = self.supervisor.confirm(identity, checked.append)
        self.assertEqual(checked, [confirmed])
        self.assertEqual(confirmed.restarts, 1)
        self.assertNotEqual(confirmed.invocation_id, identity.invocation_id)
        self.assertEqual(self.supervisor.check(confirmed).state, "running")

    def test_confirm_keeps_a_running_record_without_a_functional_check(self):
        identity = self.supervisor.start(spec())
        self.assertIs(self.supervisor.confirm(identity, self.fail), identity)

    def test_confirm_refuses_a_restart_that_fails_its_check(self):
        identity = self.supervisor.start(spec())
        self.manager.restart(UNIT)

        def unhealthy(identity):
            raise ValueError("router /health returned 503")

        with self.assertRaisesRegex(ValueError, "503"):
            self.supervisor.confirm(identity, unhealthy)

    def test_confirm_refuses_a_unit_that_restarts_again_during_its_check(self):
        identity = self.supervisor.start(spec())
        self.manager.restart(UNIT)
        with self.assertRaisesRegex(SupervisionError, "after its check"):
            self.supervisor.confirm(identity, lambda new: self.manager.restart(UNIT))

    def test_confirm_refuses_a_unit_waiting_to_restart(self):
        identity = self.supervisor.start(spec())
        self.manager.crash(UNIT)
        with self.assertRaisesRegex(SupervisionError, "auto-restart"):
            self.supervisor.confirm(identity, self.fail)

    def test_a_unit_started_again_by_someone_else_is_a_mismatch(self):
        identity = self.supervisor.start(spec())
        self.manager.launch(UNIT)
        observation = self.supervisor.check(identity)
        self.assertEqual(observation.state, "mismatch")
        self.assertIn("has not restarted it", observation.detail)
        with self.assertRaises(IdentityMismatch):
            self.supervisor.stop(identity)
        self.assertEqual(self.manager.commands(["systemctl", "--user", "stop"]), [])
        self.assertIn(UNIT, self.manager.units)

    def test_a_record_from_another_boot_cannot_stop_the_unit(self):
        identity = self.supervisor.start(spec())
        self.manager.boot_id = "boot-2"
        with self.assertRaisesRegex(IdentityMismatch, "boot-1"):
            self.supervisor.stop(identity)
        self.assertEqual(self.manager.commands(["systemctl", "--user", "stop"]), [])

    def test_a_changed_main_process_is_a_mismatch(self):
        identity = self.supervisor.start(spec())
        self.manager.units[UNIT]["ticks"] += 1
        self.assertEqual(self.supervisor.check(identity).state, "mismatch")

    def test_stop_compares_the_identity_before_stopping_the_unit(self):
        identity = self.supervisor.start(spec())
        self.assertTrue(self.supervisor.stop(identity))
        self.assertEqual(
            self.manager.commands(["systemctl", "--user", "stop"]),
            [["systemctl", "--user", "stop", UNIT]],
        )
        self.assertEqual(self.supervisor.check(identity).state, "stopped")
        self.assertFalse(self.supervisor.stop(identity))
        self.assertEqual(len(self.manager.commands(["systemctl", "--user", "stop"])), 1)

    def test_stop_reaches_a_unit_that_systemd_restarted(self):
        identity = self.supervisor.start(spec())
        self.manager.restart(UNIT)
        self.assertTrue(self.supervisor.stop(identity))
        self.assertNotIn(UNIT, self.manager.units)

    def test_a_unit_of_another_run_is_refused(self):
        identity = SystemdUnits("f" * 32, self.manager).start(spec())
        with self.assertRaisesRegex(SupervisionError, "does not belong to run"):
            self.supervisor.stop(identity)
        self.assertEqual(self.manager.commands(["systemctl", "--user", "stop"]), [])

    def test_a_unit_that_changes_between_reads_raises(self):
        identity = self.supervisor.start(spec())
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
            spec(name="Router A")
        with self.assertRaisesRegex(ValueError, "run ID"):
            SystemdUnits("run-1", self.manager)
        with self.assertRaisesRegex(ValueError, "one line"):
            spec(environment={"KEY": "two\nlines"})


class IdentityRecordTests(unittest.TestCase):
    def test_records_round_trip(self):
        for identity in (
            ProcessGroup(10, BOOT, 20),
            SystemdUnit(UNIT, "a" * 32, 10, 20, BOOT, 2),
        ):
            self.assertEqual(identity_from_record(identity.record()), identity)

    def test_malformed_records_are_refused(self):
        record = SystemdUnit(UNIT, "a" * 32, 10, 20, BOOT, 0).record()
        for change in ({"kind": "container"}, {"main_pid": 0}, {"boot_id": ""}, {"restarts": "1"}):
            with self.subTest(change=change), self.assertRaises(SupervisionError):
                identity_from_record({**record, **change})


class SystemdHostCheckTests(unittest.TestCase):
    def runner(self, **answers):
        replies = {
            "systemd-run": (0, "systemd 255 (255.4-1)\n+PAM +AUDIT\n", ""),
            "systemctl": (0, "running\n", ""),
            "id": (0, "deploy\n", ""),
            "loginctl": (0, "yes\n", ""),
            **answers,
        }
        calls = []

        def run(argv, *, input=None):
            calls.append(list(argv))
            code, stdout, stderr = replies[argv[0]]
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
                ["id", "-un"],
                ["loginctl", "show-user", "deploy", "--property=Linger", "--value"],
            ],
        )

    def test_a_degraded_user_manager_still_answers(self):
        run, _ = self.runner(systemctl=(1, "degraded\n", ""))
        self.assertEqual(check_systemd_host(run), [])

    def test_each_missing_piece_is_named(self):
        cases = {
            "systemd-run is not installed": {"systemd-run": (127, "", "not found")},
            "systemd 239": {"systemd-run": (0, "systemd 239\n", "")},
            "Failed to connect to bus": {"systemctl": (1, "", "Failed to connect to bus")},
            "answered 'offline'": {"systemctl": (1, "offline\n", "")},
            "lingering is off for deploy": {"loginctl": (0, "no\n", "")},
            "loginctl show-user deploy failed": {"loginctl": (1, "", "not logged in")},
            "could not read the user name": {"id": (1, "", "no user")},
        }
        for expected, answers in cases.items():
            with self.subTest(expected=expected):
                run, _ = self.runner(**answers)
                problems = check_systemd_host(run)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(expected, problems[0])
        run, _ = self.runner(loginctl=(0, "no\n", ""))
        self.assertIn("loginctl enable-linger deploy", check_systemd_host(run)[0])

    def test_all_missing_pieces_are_reported_together(self):
        run, _ = self.runner(**{"systemd-run": (127, "", ""), "loginctl": (0, "no\n", "")})
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

    def test_start_runs_in_the_directory_with_the_environment_and_appends_the_log(self):
        script = "import os; print(os.getcwd(), os.environ['NARWHAL_CHECK'], flush=True)"
        log = self.directory / "sidecar-1.log"
        log.write_text("earlier\n")
        identity = self.start(script, NARWHAL_CHECK="value")
        deadline = time.monotonic() + 5
        while "value" not in log.read_text() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(log.read_text(), f"earlier\n{self.directory} value\n")
        self.reap(identity)
        self.assertEqual(self.supervisor.check(identity).state, "stopped")

    def test_start_refuses_a_missing_executable(self):
        spec = ProcessSpec(
            "sidecar-1", ("/nonexistent/python",), self.directory, self.directory / "log"
        )
        with self.assertRaisesRegex(SupervisionError, "could not start"):
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
            self.assertRaisesRegex(SupervisionError, "exited at start"),
        ):
            self.supervisor.start(self.spec("import time; time.sleep(60)"))
        self.assertIsNotNone(started[0].wait(timeout=5))

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
