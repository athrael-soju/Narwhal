"""Bound real CPU helpers, detached descendants and daemon-side Docker resources."""

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
if a[0] == 'create':
    owner=a[a.index('--label')+1].split('=',1)[1]
    state['a'*64]={'Id':'a'*64,'Config':{'Labels':{'io.narwhal.launch':owner}}}
    path.write_text(json.dumps(state))
    print('created before client stalled',flush=True)
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
                "NARWHAL_DOCKER_RECONCILE_SECONDS": "0.7",
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

    def test_daemon_survivor_is_recorded_after_successful_rm_response(self):
        self.fake_docker(FAKE_DOCKER_KEEP_RESOURCE="1")
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["create", "image"], self.root, "docker.log")
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["surviving_resources"], ["a" * 64])
        self.assertEqual(report["removed"], [])

    def test_stalled_reconciliation_records_uncertain_daemon_state(self):
        self.fake_docker(FAKE_DOCKER_STALL_INSPECT="1")
        started = time.monotonic()
        with self.assertRaises(stages.StageTimeout) as caught:
            launch_engine.docker(["create", "image"], self.root, "docker.log")
        self.assertLess(time.monotonic() - started, 3)
        report = caught.exception.context["docker_reconciliation"]
        self.assertEqual(report["status"], "inspection_required")
        self.assertIn("exhausted", report["error"])
