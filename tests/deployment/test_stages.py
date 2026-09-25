"""Bound real CPU helpers, detached descendants and daemon-side Docker resources."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import launch_engine, stages


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "NARWHAL_STAGE_TIMEOUT_SECONDS": "0.3",
                "NARWHAL_STAGE_CLEANUP_GRACE_SECONDS": "0.1",
                "NARWHAL_STAGE_KILL_GRACE_SECONDS": "0.5",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        reaper = stages._reaper()
        reaper.__enter__()
        self.addCleanup(reaper.__exit__, None, None, None)
        self.process_ticks = stages._processes()[os.getpid()][3]
        self.baseline = set(stages._discover({os.getpid(): self.process_ticks}, os.getpid()))
        self.addCleanup(StageTests.cleanup_processes, self)

    def cleanup_processes(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            owned = stages._discover({os.getpid(): self.process_ticks}, os.getpid())
            remaining = {pid: row[3] for pid, row in owned.items() if pid not in self.baseline}
            stages._signal(remaining, signal.SIGKILL)
            for pid in remaining:
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
            if not remaining:
                return
            time.sleep(0.02)
        self.fail(f"test subprocess cleanup exceeded its deadline: {remaining}")

    def test_timeout_retains_output_and_reaps_detached_worker(self):
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(sibling.wait, timeout=2)
        self.addCleanup(sibling.kill)
        worker = (
            "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('worker', os.getpid(), flush=True); time.sleep(30)"
        )
        script = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1]], start_new_session=True); "
            "print('partial stdout',flush=True); "
            "print('partial stderr',file=sys.stderr,flush=True); "
            "time.sleep(30)"
        )
        started = time.monotonic()
        with self.assertRaises(stages.StageTimeout) as caught:
            stages.run(
                [sys.executable, "-c", script, worker], stage="startup", log=self.root / "up.log"
            )
        self.assertLess(time.monotonic() - started, 2)
        context = caught.exception.context
        self.assertEqual(caught.exception.stage, "startup")
        self.assertTrue(context["cleanup"]["escalated"])
        self.assertEqual(context["cleanup"]["surviving_processes"], {})
        output = Path(context["stdout"]).read_text()
        self.assertIn("partial stdout", output)
        self.assertIn("partial stderr", Path(context["stderr"]).read_text())
        pid = int(next(row.split()[1] for row in output.splitlines() if row.startswith("worker ")))
        self.assertFalse(Path(f"/proc/{pid}").exists())
        self.assertFalse(Path(f"/proc/{context['pid']}").exists())
        self.assertIsNone(sibling.poll())
        self.assertEqual(Path(context["evidence"]).stat().st_mode & 0o777, 0o600)

    def test_exited_leader_also_cleans_a_worker_and_retains_exit_status(self):
        worker = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
            "print(p.pid,flush=True); time.sleep(.08); sys.exit(7)"
        )
        result = stages.run(
            [sys.executable, "-c", script, worker],
            stage="inspection",
            log=self.root / "check.log",
            retain_descendants=True,
        )
        self.assertEqual(result.returncode, 7)
        self.assertFalse(Path(f"/proc/{int(result.stdout)}").exists())

    def test_sigterm_cancels_and_reaps_before_returning(self):
        script = (
            "from pathlib import Path; import sys; from narwhal.deployment import stages; "
            "stages.run([sys.executable,'-c',"
            "'import time; print(123,flush=True); time.sleep(30)'], "
            "stage='verify',log=Path(sys.argv[1]))"
        )
        with patch.dict(os.environ, {"NARWHAL_STAGE_TIMEOUT_SECONDS": "10"}):
            controller = subprocess.Popen(
                [sys.executable, "-c", script, str(self.root / "verify.log")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.addCleanup(controller.wait, timeout=2)
        self.addCleanup(lambda: controller.kill() if controller.poll() is None else None)
        deadline = time.monotonic() + 2
        while not list(self.root.glob("*.stage.json")) and time.monotonic() < deadline:
            time.sleep(0.01)
        controller.send_signal(signal.SIGTERM)
        _, stderr = controller.communicate(timeout=2)
        self.assertIn(b"StageCancelled", stderr)
        evidence = json.loads(next(self.root.glob("*.stage.json")).read_text())
        self.assertEqual(evidence["status"], "cancelled")
        self.assertEqual(evidence["cleanup"]["surviving_processes"], {})
        self.assertFalse(Path(f"/proc/{evidence['pid']}").exists())

    def test_immediate_fork_and_setsid_stays_owned_until_reaped(self):
        script = (
            "import os,time; pid=os.fork(); "
            "os._exit(0) if pid else None; os.setsid(); "
            "print(os.getpid(),flush=True); time.sleep(30)"
        )
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                result = stages.run(
                    [sys.executable, "-c", script],
                    stage="fast-fork",
                    log=self.root / f"fork-{attempt}.log",
                )
                self.assertEqual(result.returncode, 0)
                self.assertFalse(Path(f"/proc/{int(result.stdout)}").exists())

    def test_successful_native_style_stage_retains_its_worker(self):
        script = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "start_new_session=True); "
            "print(p.pid,flush=True); time.sleep(.05)"
        )
        result = stages.run(
            [sys.executable, "-c", script],
            stage="native-start-shared",
            log=self.root / "retain.log",
            retain_descendants=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(int(result.stdout), stages._processes())
        record = json.loads(next(self.root.glob("retain.log.*.stage.json")).read_text())
        self.assertEqual(record["status"], "completed")
        self.assertFalse(Path(f"/proc/{record['pid']}").exists())

    def test_release_wait_failure_cleans_children_and_retains_evidence(self):
        original = subprocess.Popen.wait
        for failure in (KeyboardInterrupt(), subprocess.TimeoutExpired("supervisor-release", 0.5)):
            with self.subTest(failure=type(failure).__name__):
                interrupted = False

                def wait(child, timeout=None, failure=failure):
                    nonlocal interrupted
                    if not interrupted:
                        interrupted = True
                        raise failure
                    return original(child, timeout=timeout)

                log = self.root / (type(failure).__name__ + ".log")
                with (
                    patch.object(subprocess.Popen, "wait", wait),
                    self.assertRaises((stages.StageCancelled, stages.StageTimeout)) as caught,
                ):
                    stages.run(
                        [sys.executable, "-c", "print('released')"],
                        stage="release",
                        log=log,
                        retain_descendants=True,
                    )
                self.assertEqual(caught.exception.context["cleanup"]["surviving_processes"], {})
                self.assertTrue(Path(caught.exception.context["evidence"]).exists())

    def test_release_marker_io_failure_cleans_and_records_original_error(self):
        touch = Path.touch

        def fail_release(path, *args, **kwargs):
            if path.suffix == ".release":
                raise OSError("release marker storage unavailable")
            return touch(path, *args, **kwargs)

        with (
            patch.object(Path, "touch", fail_release),
            self.assertRaisesRegex(OSError, "release marker storage unavailable"),
        ):
            stages.run(
                [sys.executable, "-c", "print('release ready')"],
                stage="release-io",
                log=self.root / "release-io.log",
                retain_descendants=True,
            )
        record = json.loads(next(self.root.glob("release-io.log.*.stage.json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["cleanup"]["surviving_processes"], {})
        self.assertEqual(record["release_error"], "release marker storage unavailable")

    def test_relative_log_with_another_child_working_directory(self):
        child_root = self.root / "child"
        child_root.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            result = stages.run(
                [sys.executable, "-c", "print('completed')"],
                stage="relative",
                log=Path("relative.log"),
                cwd=child_root,
            )
        finally:
            os.chdir(previous)
        self.assertEqual(result.stdout.strip(), "completed")
        record = json.loads(next(self.root.glob("relative.log.*.stage.json")).read_text())
        self.assertEqual(Path(record["evidence"]).parent, self.root)

    def test_invalid_budgets_reject_before_launch(self):
        for value in ("0", "-1", "nan", "inf"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"NARWHAL_STAGE_TIMEOUT_SECONDS": value}),
                self.assertRaisesRegex(ValueError, "finite and positive"),
            ):
                stages.run(["missing-helper"], stage="check", log=self.root / "check.log")


FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, pathlib, sys, time
path=pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
state=json.loads(path.read_text())
a=sys.argv[1:]
if a[0] in ('create', 'run'):
    labels=dict(a[i+1].split('=',1) for i,v in enumerate(a) if v == '--label')
    state['a'*64]={'Id':'a'*64,'Config':{'Labels':labels}}
    path.write_text(json.dumps(state))
    print('created before client stalled',flush=True)
    time.sleep(30)
elif a[0] == 'start':
    time.sleep(30)
elif a[0] == 'ps':
    if os.environ.get('FAKE_DOCKER_STALL_INSPECT'): time.sleep(30)
    for cid, record in state.items():
        label=record['Config']['Labels'].get('io.narwhal.launch')
        if '--filter' not in a or label == a[-1].split('=',2)[2]:
            print(cid)
elif a[0] == 'inspect':
    print(json.dumps([state[cid] for cid in a[1:]]))
elif a[0] == 'rm':
    if not os.environ.get('FAKE_DOCKER_KEEP_RESOURCE'):
        for cid in a[2:]: state.pop(cid,None)
        path.write_text(json.dumps(state))
else:
    raise SystemExit(2)
"""


class DockerStageTests(unittest.TestCase):
    setUp = StageTests.setUp

    def fake_docker(self, **extra):
        executable = self.root / "docker"
        executable.write_text(FAKE_DOCKER)
        executable.chmod(0o700)
        state = self.root / "daemon.json"
        state.write_text(
            json.dumps(
                {"b" * 64: {"Id": "b" * 64, "Config": {"Labels": {"io.narwhal.launch": "other"}}}}
            )
        )
        environment = patch.dict(
            os.environ,
            {
                "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                "FAKE_DOCKER_STATE": str(state),
                "NARWHAL_DOCKER_RECONCILE_SECONDS": "1.5",
                **extra,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        return state

    def test_stalled_create_reconciles_label_and_preserves_unrelated_container(self):
        state = self.fake_docker()
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["create", "image"], self.root, "docker.log")
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["removed"], ["a" * 64])
        self.assertEqual(report["surviving_resources"], [])
        self.assertEqual(set(json.loads(state.read_text())), {"b" * 64})
        self.assertIn("created before client stalled", (self.root / "docker.log").read_text())

    def test_stalled_inspection_preserves_the_existing_serving_container(self):
        state = self.fake_docker()
        owner = launch_engine._docker_owner(self.root)
        records = json.loads(state.read_text())
        records["b" * 64]["Config"]["Labels"]["io.narwhal.launch"] = owner
        records["b" * 64]["State"] = {"Running": True}
        state.write_text(json.dumps(records))
        (self.root / "container.id").write_text("b" * 64)
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["run", "--rm", "image"], self.root, "inspection.log")
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["removed"], ["a" * 64])
        self.assertEqual(report["preserved_resources"], ["b" * 64])
        self.assertEqual(report["surviving_resources"], ["b" * 64])
        self.assertEqual(json.loads(state.read_text()), {"b" * 64: records["b" * 64]})

    def test_stalled_start_cleans_only_the_named_container(self):
        state = self.fake_docker()
        owner = launch_engine._docker_owner(self.root)
        records = {
            cid: {"Id": cid, "Config": {"Labels": {"io.narwhal.launch": owner}}}
            for cid in ("a" * 64, "b" * 64)
        }
        state.write_text(json.dumps(records))
        (self.root / "container.id").write_text("b" * 64)
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["start", "a" * 64], self.root, "start.log")
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["removed"], ["a" * 64])
        self.assertEqual(report["preserved_resources"], ["b" * 64])
        self.assertEqual(set(json.loads(state.read_text())), {"b" * 64})

    def test_daemon_survivor_is_recorded_after_successful_rm_response(self):
        self.fake_docker(FAKE_DOCKER_KEEP_RESOURCE="1")
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["create", "image"], self.root, "docker.log")
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["surviving_resources"], ["a" * 64])
        self.assertEqual(report["removed"], [])

    def test_stalled_reconciliation_records_uncertain_daemon_state(self):
        self.fake_docker(FAKE_DOCKER_STALL_INSPECT="1", NARWHAL_DOCKER_RECONCILE_SECONDS="0.2")
        started = time.monotonic()
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["create", "image"], self.root, "docker.log")
        self.assertLess(time.monotonic() - started, 3)
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["status"], "inspection_required")
        self.assertIn("exhausted", report["error"])
