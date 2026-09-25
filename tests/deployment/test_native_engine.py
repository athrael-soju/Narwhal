"""Verify native shutdown cannot signal a reused or unrelated PID."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from narwhal.deployment.native_engine import (
    _environment,
    _group_members,
    _ports_free,
    _terminate,
    process_identity,
    start_shared,
    stop,
)

from .fixtures import process_group_with_worker


class NativeEngineOwnershipTests(unittest.TestCase):
    def test_native_auth_uses_the_prepared_environment(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"VLLM_API_KEY": "inherited-key"}),
        ):
            run = Path(folder)
            (run / "engine.env").write_text("")
            self.assertEqual(_environment(run)["VLLM_API_KEY"], "")
            (run / "engine.env").write_text("VLLM_API_KEY=prepared-key\n")
            self.assertEqual(_environment(run)["VLLM_API_KEY"], "prepared-key")

    def test_shutdown_escalates_workers_after_the_leader_exits(self):
        with process_group_with_worker() as (leader, worker):
            identity = process_identity(leader.pid)
            self.assertIn(worker, _group_members(identity))
            _terminate(identity, grace_seconds=0.05)
            leader.wait(timeout=5)
            self.assertEqual(_group_members(identity), {})

    def test_exited_leader_reports_survivors_without_signalling_them(self):
        with process_group_with_worker() as (leader, worker):
            identity = process_identity(leader.pid)
            leader.terminate()
            leader.wait(timeout=5)
            with self.assertRaisesRegex(ValueError, f"surviving group PIDs \\[{worker}\\]"):
                _terminate(identity, grace_seconds=0.05)
            self.assertIn(worker, _group_members(identity))

    def test_stale_group_identity_preserves_the_leader_and_worker(self):
        with process_group_with_worker() as (leader, worker):
            identity = process_identity(leader.pid)
            identity["start_ticks"] += 1
            self.assertEqual(_group_members(identity), {})
            with self.assertRaisesRegex(ValueError, "refusing to signal"):
                _terminate(identity, grace_seconds=0.05)
            self.assertIsNone(leader.poll())
            os.kill(worker, 0)

    def test_live_port_owner_blocks_native_start(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, f"port {port} is unavailable"):
                _ports_free(
                    [
                        (
                            Path("unused"),
                            {
                                "role": "engine-1",
                                "endpoint": f"http://127.0.0.1:{port}",
                                "attestation_port": 0,
                                "side_channel_port": 0,
                            },
                        )
                    ]
                )

    def test_checked_native_engines_record_live_process_and_vllm_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = []
            for number in (1, 2):
                run = Path(folder) / f"engine-{number}"
                run.mkdir()
                (run / "launch.json").write_text("{}")
                (run / "engine.env").write_text("VLLM_API_KEY=test-key\n")
                selected.append(
                    (
                        run,
                        {
                            "role": f"engine-{number}",
                            "endpoint": f"http://127.0.0.1:{8000 + number}",
                            "python_executable": sys.executable,
                            "args": ["-m", "vllm.entrypoints.openai.api_server"],
                            "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
                            "model_revision": "pinned-model-commit",
                            "model_config_sha256": "a" * 64,
                            "shared_device": {
                                "group": "kimchi:GPU-test",
                                "gpu_uuid": "GPU-test",
                                "device_allowance": 0.9,
                                "gpu_memory_utilization": 0.2,
                            },
                        },
                    )
                )
            readings = iter((1000, 6100, 6100, 11200))
            live = SimpleNamespace(vllm_version="0.29.0", process_start_time_seconds=1234.5)
            with (
                patch(
                    "narwhal.deployment.native_engine.validate_shared_runs", return_value=selected
                ),
                patch("narwhal.deployment.native_engine._ports_free"),
                patch(
                    "narwhal.deployment.native_engine._checked_plan",
                    return_value={"vllm_api_version": "0.29.0"},
                ),
                patch("narwhal.deployment.native_engine.gpu_memory") as memory,
                patch("narwhal.deployment.native_engine.subprocess.Popen") as popen,
                patch("narwhal.deployment.native_engine.process_identity") as identity,
                patch("narwhal.deployment.native_engine._wait_ready"),
                patch(
                    "narwhal.deployment.native_engine.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=live,
                ) as fetch,
            ):
                memory.side_effect = lambda _: {
                    "used_mib": next(readings),
                    "total_mib": 30000,
                }
                popen.side_effect = [Mock(pid=101), Mock(pid=102)]
                identity.side_effect = [
                    {"pid": 101, "boot_id": "boot", "start_ticks": 10},
                    {"pid": 102, "boot_id": "boot", "start_ticks": 20},
                ]
                start_shared([run for run, _ in selected], ready_seconds=30)
                self.assertEqual(popen.call_count, 2)
                self.assertEqual(fetch.await_count, 2)
                self.assertEqual(
                    fetch.await_args.kwargs["headers"], {"Authorization": "Bearer test-key"}
                )
            for run, plan in selected:
                record = json.loads((run / "shared-start.json").read_text())
                self.assertEqual(record["status"], "running")
                self.assertEqual(record["model_revision"], "pinned-model-commit")
                self.assertEqual(record["vllm_version"], "0.29.0")
                self.assertEqual(record["process_start_time_seconds"], 1234.5)
                self.assertEqual(record["budget_mib"], 6000)
                self.assertEqual(record["device_allowance_mib"], 27000)
                self.assertEqual(record["observed_delta_mib"], 5100)
                self.assertEqual(
                    record["aggregate_delta_mib"], 5100 if plan["role"] == "engine-1" else 10200
                )
                self.assertEqual(record["vllm_args"], plan["args"])

    def test_stop_requires_recorded_process_start_and_terminates_owned_group(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            try:
                identity = process_identity(process.pid)
                stale = {**identity, "start_ticks": identity["start_ticks"] + 1}
                (run / "native-process.json").write_text(json.dumps(stale))
                with self.assertRaisesRegex(ValueError, "refusing to signal"):
                    stop(run)
                self.assertIsNone(process.poll())
                (run / "native-process.json").write_text(json.dumps(identity))
                stop(run)
                self.assertIsNotNone(process.wait(timeout=5))
                self.assertTrue((run / "native-stop.json").exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_observed_shared_memory_overage_stops_owned_processes(self):
        with tempfile.TemporaryDirectory() as folder:
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
                            "endpoint": f"http://127.0.0.1:{8000 + number}",
                            "python_executable": sys.executable,
                            "args": ["-m", "vllm.entrypoints.openai.api_server"],
                            "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
                            "model_revision": "pinned-model-commit",
                            "model_config_sha256": "a" * 64,
                            "shared_device": {
                                "group": "kimchi:GPU-test",
                                "gpu_uuid": "GPU-test",
                                "device_allowance": 0.3,
                                "gpu_memory_utilization": 0.1,
                            },
                        },
                    )
                )
            readings = iter((1000, 6100, 6100, 12000, 6500))
            with (
                patch(
                    "narwhal.deployment.native_engine.validate_shared_runs", return_value=selected
                ),
                patch("narwhal.deployment.native_engine._ports_free"),
                patch(
                    "narwhal.deployment.native_engine._checked_plan",
                    return_value={"vllm_api_version": "0.29.0"},
                ),
                patch(
                    "narwhal.deployment.native_engine.gpu_memory",
                    side_effect=lambda _: {"used_mib": next(readings), "total_mib": 30000},
                ),
                patch(
                    "narwhal.deployment.native_engine.subprocess.Popen",
                    side_effect=[Mock(pid=101), Mock(pid=102)],
                ),
                patch(
                    "narwhal.deployment.native_engine.process_identity",
                    side_effect=[
                        {"pid": 101, "boot_id": "boot", "start_ticks": 10},
                        {"pid": 102, "boot_id": "boot", "start_ticks": 20},
                    ],
                ),
                patch("narwhal.deployment.native_engine._wait_ready"),
                patch(
                    "narwhal.deployment.native_engine.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=SimpleNamespace(
                        vllm_version="0.29.0", process_start_time_seconds=1234.5
                    ),
                ),
                patch("narwhal.deployment.native_engine._group_members", return_value={102: 20}),
                patch("narwhal.deployment.native_engine._terminate") as terminate,
                patch("narwhal.deployment.native_engine.stop") as stop_first,
                self.assertRaisesRegex(
                    ValueError, "engine-2: native launch failed with 6100 MiB used before start"
                ) as failure,
            ):
                start_shared([run for run, _ in selected], ready_seconds=30)
            self.assertIn("3000.0 MiB allocation", str(failure.exception))
            self.assertIn("5900 MiB observed increase", str(failure.exception))
            self.assertIn("observed shared GPU use 11000 MiB exceeds", str(failure.exception))
            terminate.assert_called_once()
            stop_first.assert_called_once_with(selected[0][0])
            failed = json.loads((selected[1][0] / "shared-start.json").read_text())
            self.assertEqual(failed["aggregate_delta_mib"], 11000)
            self.assertEqual(failed["gpu_after_cleanup"]["used_mib"], 6500)


if __name__ == "__main__":
    unittest.main()
