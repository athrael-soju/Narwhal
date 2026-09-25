"""Qualify persisted ownership with real signals and fresh recovery controllers."""

import contextlib
import hashlib
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

from narwhal.deployment import stages
from narwhal.dev import lifecycle, template

MODULE = "tests.deployment.interruption_harness"
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == "linux", "Linux process ownership contract")
class InterruptionTests(unittest.TestCase):
    def initialize(self, root):
        root.mkdir()
        (root / "registry").mkdir()
        model = root / "model.gguf"
        model.write_bytes(b"synthetic")
        spec = template.reference()
        spec["model"].update(sha256=hashlib.sha256(b"synthetic").hexdigest(), tokenizer_sha256={})
        lifecycle.write(root / "template.json", spec)
        lifecycle.write(
            root / "instance.json",
            {
                "schema": "narwhal.dev-instance",
                "schema_version": 1,
                "python_executable": sys.executable,
                "model_path": str(model),
                "model_dir": str(root),
                "engine_count": 2,
                "gpu_uuid": "synthetic",
                "router_url": "http://127.0.0.1:1",
            },
        )
        lifecycle.write(
            root / "fleet.json",
            {
                "model": "synthetic",
                "profiles": {},
                "engines": [
                    {"iid": "engine-1", "url": "http://127.0.0.1:1", "role": "prefill"},
                    {"iid": "engine-2", "url": "http://127.0.0.1:2", "role": "decode"},
                ],
            },
        )

    def live(self, root):
        processes = stages._processes()
        return {
            item["pid"]: item["start_ticks"]
            for path in (root / "registry").glob("pid-*.json")
            if (item := json.loads(path.read_text()))
            and item["pid"] in processes
            and processes[item["pid"]][3] == item["start_ticks"]
            and processes[item["pid"]][0] not in {"Z", "X"}
        }

    def cleanup(self, root, controller):
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stages._signal(self.live(root), signal.SIGKILL)
            for path in (root / "registry").glob("pid-*.json"):
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(json.loads(path.read_text())["pid"], os.WNOHANG)
            if not self.live(root):
                return
            time.sleep(0.02)
        self.fail(f"harness cleanup survivors: {self.live(root)}")

    def inspect(self, root, operation):
        result = subprocess.run(
            [sys.executable, "-m", MODULE, operation, str(root)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        value = json.loads(result.stdout)
        self.assertFalse(value["busy"], result.stdout)
        return value

    def await_barrier(self, root, selected, controller):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if controller.poll() is not None:
                self.fail(
                    f"controller exited before {selected}: {(root / 'controller.log').read_text()}"
                )
            if selected in {"profiling", "verification"}:
                evidence = list(root.glob("run-*/**/*.stage.json"))
                if evidence and len(list((root / "registry").glob("pid-*.json"))) >= 5:
                    # Wait for the parent to observe the detached helper worker.
                    time.sleep(0.06)
                    return
            elif (root / "barrier.json").exists():
                self.assertEqual(json.loads((root / "barrier.json").read_text())["name"], selected)
                return
            time.sleep(0.01)
        self.fail(f"controller did not reach {selected}")

    def exercise(self, selected, sig):
        with tempfile.TemporaryDirectory() as folder, stages._reaper():
            root = Path(folder) / "instance"
            self.initialize(root)
            sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
            controller = None
            try:
                with (root / "controller.log").open("w") as output:
                    controller = subprocess.Popen(
                        [sys.executable, "-m", MODULE, "controller", str(root), selected],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
                self.await_barrier(root, selected, controller)
                controller.send_signal(sig)
                controller.wait(timeout=5)
                self.assertNotEqual(controller.returncode, 0)
                if sig == signal.SIGKILL or (sig == signal.SIGTERM and selected == "owned"):
                    self.assertEqual(controller.returncode, -sig)
                for path in root.glob("**/*.json"):
                    self.assertIsInstance(json.loads(path.read_text()), dict, str(path))
                state = lifecycle.read(root / "lifecycle.json")
                self.assertTrue(list(Path(state["run"]).glob("router.log")))
                self.assertIn("retained service", (Path(state["run"]) / "router.log").read_text())
                before = self.live(root)
                status = self.inspect(root, "status")["result"]
                self.assertIsNone(sibling.poll())
                if selected in {"child-created", "ownership-pending"} and sig == signal.SIGKILL:
                    self.assertTrue(before)
                    self.assertEqual(state["processes"], [])
                    self.assertEqual(status["status"], "degraded")
                    self.assertEqual(self.inspect(root, "down")["result"]["status"], "stopped")
                    self.assertEqual(self.live(root), before)
                elif selected == "exited-leader":
                    self.assertEqual(status["status"], "degraded")
                    self.assertTrue(status["surviving_processes"])
                    self.assertIn("operator inspection", self.inspect(root, "down")["error"])
                    self.assertEqual(self.live(root), before)
                else:
                    if selected in {"profiling", "verification"}:
                        stage = json.loads(next(root.glob("run-*/**/*.stage.json")).read_text())
                        self.assertEqual(
                            stage["status"], "running" if sig == signal.SIGKILL else "cancelled"
                        )
                        self.assertIn("retained helper", Path(stage["stdout"]).read_text())
                        if sig == signal.SIGKILL:
                            self.assertTrue(status["stage_processes"])
                    if selected != "verification" and sig == signal.SIGINT:
                        self.assertEqual(status["status"], "stopped")
                    self.assertEqual(self.inspect(root, "down")["result"]["status"], "stopped")
                    self.assertEqual(self.live(root), {})
                    self.assertEqual(self.inspect(root, "down")["result"]["status"], "stopped")
                self.assertIsNone(sibling.poll())
            finally:
                if controller is not None:
                    self.cleanup(root, controller)
                sibling.kill()
                sibling.wait(timeout=3)

    def test_stale_stage_identity_preserves_unrelated_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stage.json"
            sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
            try:
                ticks = stages._processes()[sibling.pid][3]
                boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                for recorded_boot, recorded_ticks in (("previous-boot", ticks), (boot, ticks + 1)):
                    with self.subTest(boot=recorded_boot, ticks=recorded_ticks):
                        stages.write_evidence(
                            path,
                            {
                                "pid": sibling.pid,
                                "processes": {sibling.pid: recorded_ticks},
                                "boot_id": recorded_boot,
                                "status": "running",
                                "stage": "stale",
                            },
                        )
                        self.assertEqual(stages.active(json.loads(path.read_text())), {})
                        self.assertEqual(stages.recover(path)["surviving_processes"], {})
                        self.assertIsNone(sibling.poll())
            finally:
                sibling.kill()
                sibling.wait(timeout=3)

    def test_signal_and_persistence_matrix(self):
        matrix = [
            ("child-created", signal.SIGINT),
            ("child-created", signal.SIGKILL),
            ("ownership-pending", signal.SIGKILL),
            ("owned", signal.SIGINT),
            ("owned", signal.SIGTERM),
            ("readiness", signal.SIGKILL),
            ("profiling", signal.SIGINT),
            ("profiling", signal.SIGTERM),
            ("profiling", signal.SIGKILL),
            ("verification", signal.SIGTERM),
            ("verification", signal.SIGKILL),
            ("exited-leader", signal.SIGKILL),
        ]
        with patch.dict(
            os.environ,
            {
                "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT),
                "NARWHAL_STAGE_TIMEOUT_SECONDS": "10",
                "NARWHAL_STAGE_CLEANUP_GRACE_SECONDS": ".05",
                "NARWHAL_STAGE_KILL_GRACE_SECONDS": ".5",
            },
        ):
            for selected, sig in matrix:
                with self.subTest(stage=selected, signal=sig.name):
                    self.exercise(selected, sig)
