"""Keep deadline and cancellation context through shared-engine launch cleanup."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from narwhal.deployment import launch_engine, native_engine, stages


def failure(exception, root):
    return exception(
        "docker-start",
        {
            "budget_seconds": 0.1,
            "evidence": str(root / "stage.json"),
            "recovery": "inspect recorded resource IDs",
            "docker_reconciliation": {"surviving_resources": ["c" * 64]},
        },
    )


def invoke(runs, backend):
    stdout = io.StringIO()
    argv = ["start-shared", "--backend", backend, "--format", "json"]
    for run in runs:
        argv.extend(["--run", str(run)])
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        code = launch_engine.main(argv)
    result = json.loads(stdout.getvalue())
    assert result["exit_code"] == code
    return result


class SharedStageFailureTests(unittest.TestCase):
    def test_container_shared_start_keeps_timeout_and_cancellation_contracts(self):
        for exception, code, error_code in (
            (stages.StageTimeout, 4, "stage_timeout"),
            (stages.StageCancelled, 130, "stage_cancelled"),
        ):
            with self.subTest(exception=exception), tempfile.TemporaryDirectory() as folder:
                run = Path(folder)
                (run / "launch.json").write_text("{}")
                plan = {
                    "role": "engine-1",
                    "ucx_tls": "tcp,cuda",
                    "shared_device": {
                        "gpu_uuid": "GPU-test",
                        "gpu_memory_utilization": 0.1,
                        "device_allowance": 0.9,
                    },
                }
                error = failure(exception, run)
                with (
                    patch.object(launch_engine, "validate_shared_runs", return_value=[(run, plan)]),
                    patch.object(
                        launch_engine,
                        "gpu_memory",
                        return_value={"total_mib": 10000, "used_mib": 1000},
                    ),
                    patch.object(launch_engine, "_create_container", side_effect=error),
                ):
                    result = invoke([run], "container")
                self.assertEqual(result["exit_code"], code)
                entry = result["errors"][0]
                self.assertEqual(entry["code"], error_code)
                self.assertEqual(entry["stage"], "docker-start")
                self.assertEqual(entry["context"], error.context)
                retained = json.loads((run / "shared-start.json").read_text())
                self.assertEqual(retained["status"], "failed")
                self.assertEqual(retained["failure_stage"], error.stage)
                self.assertEqual(retained["failure_context"], error.context)

    def test_native_shared_failure_stops_previous_engines_and_preserves_type(self):
        for exception, code, error_code in (
            (stages.StageTimeout, 4, "stage_timeout"),
            (stages.StageCancelled, 130, "stage_cancelled"),
        ):
            with self.subTest(exception=exception), tempfile.TemporaryDirectory() as folder:
                selected = []
                for number in (1, 2):
                    run = Path(folder) / f"engine-{number}"
                    run.mkdir()
                    (run / "launch.json").write_text("{}")
                    (run / "engine.env").write_text("")
                    selected.append(
                        (
                            run,
                            {
                                "role": f"engine-{number}",
                                "endpoint": "http://127.0.0.1:1",
                                "python_executable": sys.executable,
                                "args": ["-m", "vllm"],
                                "expected_packages": {"vllm": "test"},
                                "model_revision": "test",
                                "model_config_sha256": "a" * 64,
                                "shared_device": {
                                    "gpu_uuid": "GPU-test",
                                    "gpu_memory_utilization": 0.1,
                                    "device_allowance": 0.9,
                                },
                            },
                        )
                    )
                error = failure(exception, selected[1][0])
                with (
                    patch.object(native_engine, "validate_shared_runs", return_value=selected),
                    patch.object(native_engine, "_ports_free"),
                    patch.object(
                        native_engine, "_checked_plan", return_value={"vllm_api_version": "test"}
                    ),
                    patch.object(
                        native_engine,
                        "gpu_memory",
                        return_value={"total_mib": 10000, "used_mib": 1000},
                    ),
                    patch.object(
                        native_engine.subprocess,
                        "Popen",
                        side_effect=[Mock(pid=101), Mock(pid=102)],
                    ),
                    patch.object(
                        native_engine, "process_identity", side_effect=[{"pid": 101}, {"pid": 102}]
                    ),
                    patch.object(native_engine, "_wait_ready", side_effect=[None, error]),
                    patch.object(
                        native_engine,
                        "fetch_engine_identity",
                        AsyncMock(
                            return_value=SimpleNamespace(
                                vllm_version="test", process_start_time_seconds=1
                            )
                        ),
                    ),
                    patch.object(native_engine, "_group_members", return_value={102: 2}),
                    patch.object(native_engine, "_terminate") as terminate,
                    patch.object(
                        native_engine, "stop", side_effect=ValueError("prior engine cleanup failed")
                    ) as stop,
                ):
                    result = invoke([run for run, _ in selected], "native")
                terminate.assert_called_once_with({"pid": 102})
                stop.assert_called_once_with(selected[0][0])
                self.assertEqual(result["exit_code"], code)
                entry = result["errors"][0]
                self.assertEqual(entry["code"], error_code)
                self.assertEqual(entry["stage"], error.stage)
                self.assertEqual(
                    entry["context"]["docker_reconciliation"],
                    error.context["docker_reconciliation"],
                )
                self.assertIn(
                    "prior engine cleanup failed", entry["context"]["native_cleanup_errors"][0]
                )
                retained = json.loads((selected[1][0] / "shared-start.json").read_text())
                self.assertEqual(retained["failure_stage"], error.stage)
                self.assertEqual(retained["failure_context"]["budget_seconds"], 0.1)
