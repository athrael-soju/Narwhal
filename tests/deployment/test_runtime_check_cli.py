"""Keep repeated CLI runtime inspections bound to their prepared serving inputs."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import launch_engine as launcher
from narwhal.deployment import stages
from tests.deployment.fixtures import launcher_inputs


class RuntimeCheckCliTests(unittest.TestCase):
    def test_native_timeout_retains_partial_output_and_names_its_log(self):
        with tempfile.TemporaryDirectory() as folder:
            run, _ = self.prepare(Path(folder), "native")
            log = run / "runtime-check.log"
            log.write_text("earlier inspection\n")
            run_stage = stages.run

            def timeout(command, **kwargs):
                return run_stage(
                    [
                        sys.executable,
                        "-c",
                        "import sys,time; "
                        "print('package versions',flush=True); "
                        "print('connector import started',file=sys.stderr,flush=True); "
                        "time.sleep(30)",
                    ],
                    stage=kwargs["stage"],
                    log=kwargs["log"],
                    timeout=0.3,
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "NARWHAL_STAGE_CLEANUP_GRACE_SECONDS": ".05",
                        "NARWHAL_STAGE_KILL_GRACE_SECONDS": ".2",
                    },
                ),
                patch.object(stages, "run", side_effect=timeout),
            ):
                diagnostic = self.failed_cli(["check", "--run", str(run)])
            self.assertIn("native-runtime-check exhausted 0.3s", diagnostic)
            self.assertIn(str(log), diagnostic)
            self.assertNotIn("Traceback", diagnostic)
            self.assertFalse((run / "checked.json").exists())
            output = log.read_text()
            for text in ("earlier inspection", "package versions", "connector import started"):
                self.assertIn(text, output)

    def prepare(self, root, backend):
        _, env = launcher_inputs(root)
        env["NARWHAL_MODEL_REVISION"] = "a" * 40
        run = root / "launch"
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["prepare", "--backend", backend, "--out", str(run)]), 0)
        return run, env

    def failed_cli(self, arguments):
        with (
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            launcher.main(arguments)
        self.assertEqual(raised.exception.code, 1)
        return stderr.getvalue()

    def test_repeated_checks_preserve_plan_evidence_and_report_later_runtime_failures(self):
        for backend in ("native", "container"):
            for failure in ("package", "tokenizer", "identity"):
                with (
                    self.subTest(backend=backend, failure=failure),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    self.check_repeats(Path(folder), backend, failure)

    def check_repeats(self, root, backend, failure):
        run, env = self.prepare(root, backend)
        plan_bytes = (run / "launch.json").read_bytes()
        plan_hash = launcher.digest(run / "launch.json")
        env_path = run / ("engine.env" if backend == "native" else "container.env")
        env_bytes = env_path.read_bytes()
        attempt = 0
        details = {
            "package": (
                'Traceback (most recent call last):\n  File "runtime.py", line 4\n'
                "AssertionError: image package versions differ"
            ),
            "tokenizer": "ValueError: selected tokenizer has invalid vocabulary",
        }

        def inspect(command, **kwargs):
            failing = attempt == 3
            if command[:3] == ["docker", "image", "inspect"]:
                image = (
                    "sha256:" + "b" * 64
                    if failing and failure == "identity"
                    else env["NARWHAL_ENGINE_IMAGE"]
                )
                return subprocess.CompletedProcess(command, 0, json.dumps([{"Id": image}]), "")
            stdout = f"attempt-{attempt}-stdout\n"
            if failing and failure in details:
                return subprocess.CompletedProcess(command, 1, stdout, details[failure])
            version = "0.30.0" if failing and failure == "identity" else "0.29.0"
            stdout += "NARWHAL_TOKENIZER_READY=1\nNARWHAL_IMAGE_RUNTIME=" + json.dumps(
                {"vllm_api_version": version}
            )
            return subprocess.CompletedProcess(command, 0, stdout, "")

        def stage(command, **kwargs):
            result = inspect(command)
            launcher.append_private(
                kwargs["log"],
                json.dumps({"exit": result.returncode})
                + "\n"
                + result.stdout
                + "\nSTDERR\n"
                + result.stderr
                + "\n",
            )
            return result

        with (
            patch.object(stages, "run", side_effect=stage),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            arguments = ["check", "--run", str(run)]
            attempt = 1
            self.assertEqual(launcher.main(arguments), 0)
            evidence = (run / "checked.json").read_bytes()
            attempt = 2
            self.assertEqual(launcher.main(arguments), 0)
            self.assertEqual((run / "checked.json").read_bytes(), evidence)
            attempt = 3
            diagnostic = self.failed_cli(arguments)

        if failure == "identity":
            expected = (
                "runtime identity changed"
                if backend == "native"
                else "local image identity differs"
            )
        else:
            expected = details[failure].splitlines()[-1]
        self.assertIn(expected, diagnostic)
        self.assertNotIn("Traceback", diagnostic)
        self.assertEqual((run / "launch.json").read_bytes(), plan_bytes)
        self.assertEqual(env_path.read_bytes(), env_bytes)
        self.assertEqual((run / "checked.json").read_bytes(), evidence)
        self.assertEqual(json.loads(evidence)["plan_sha256"], plan_hash)
        log_path = run / ("runtime-check.log" if backend == "native" else "image-check.log")
        output = log_path.read_text()
        self.assertIn("attempt-1-stdout", output)
        self.assertIn("attempt-2-stdout", output)
        if failure in details:
            self.assertIn("attempt-3-stdout", output)
            self.assertIn(details[failure], output)
        headers = [
            json.loads(line) for line in output.splitlines() if line.startswith('{"check_attempt":')
        ]
        self.assertEqual(len(headers), 3)
        self.assertEqual(len({entry["check_attempt"] for entry in headers}), 3)
        self.assertEqual({entry["plan_sha256"] for entry in headers}, {plan_hash})
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)

    def test_recheck_rejects_a_changed_launch_plan_before_runtime_inspection(self):
        for backend in ("native", "container"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as folder:
                run, _ = self.prepare(Path(folder), backend)
                plan = launcher.load(run)
                evidence = {"plan_sha256": launcher.digest(run / "launch.json")}
                (run / "checked.json").write_text(json.dumps(evidence))
                original = (run / "checked.json").read_bytes()
                plan["args"].append("--language-model-only")
                (run / "launch.json").write_text(json.dumps(plan))
                with patch.object(stages, "run") as invoke:
                    diagnostic = self.failed_cli(["check", "--run", str(run)])
                self.assertIn("launch plan changed after its runtime check", diagnostic)
                self.assertEqual((run / "checked.json").read_bytes(), original)
                invoke.assert_not_called()

    def test_cli_collision_names_the_operation_and_existing_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            run, env = self.prepare(Path(folder), "native")
            with patch.dict(os.environ, env):
                diagnostic = self.failed_cli(["prepare", "--backend", "native", "--out", str(run)])
            self.assertIn("prepare: artifact already exists:", diagnostic)
            self.assertIn(str(run), diagnostic)

            plan = launcher.load(run)
            (run / "checked.json").write_text(
                json.dumps(
                    {
                        "plan_sha256": launcher.digest(run / "launch.json"),
                        "backend": "native",
                        "python_executable": plan["python_executable"],
                        "expected_packages": plan["expected_packages"],
                    }
                )
            )
            log = run / "handshake-policy.log"
            log.write_text("prior inspection")
            with patch.object(
                stages,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "inspection", ""),
            ):
                diagnostic = self.failed_cli(["handshake-policy", "--run", str(run)])
            self.assertIn("handshake-policy: artifact already exists:", diagnostic)
            self.assertIn(str(log), diagnostic)
            self.assertEqual(log.read_text(), "prior inspection")
