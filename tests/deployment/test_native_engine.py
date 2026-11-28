import contextlib
import io
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

from narwhal.deployment.attestation_contract.evidence import read_native_start
from narwhal.deployment.launch_engine import cli as engine_cli
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.deployment.native_engine import (
    _environment,
    _ports_free,
    group_members,
    process_identity,
    start_each,
    start_shared,
    stop,
    terminate,
    validate_native_runs,
)
from tests.runtime.test_listeners import listener

from .fixtures import process_group_with_worker


class NativeEngineOwnershipTests(unittest.TestCase):
    def test_native_auth_uses_the_prepared_environment(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"VLLM_API_KEY": "inherited-key"}),
        ):
            run = Path(folder)
            (run / "engine.env").write_text("")
            self.assertEqual(_environment(run, {})["VLLM_API_KEY"], "")
            (run / "engine.env").write_text("VLLM_API_KEY=prepared-key\n")
            self.assertEqual(_environment(run, {})["VLLM_API_KEY"], "prepared-key")

    def test_shutdown_escalates_workers_after_the_leader_exits(self):
        with process_group_with_worker() as (leader, worker):
            identity = process_identity(leader.pid)
            self.assertIn(worker, group_members(identity))
            terminate(identity, grace_seconds=0.05)
            leader.wait(timeout=5)
            self.assertEqual(group_members(identity), {})

    def test_exited_leader_reports_survivors_without_signalling_them(self):
        with process_group_with_worker() as (leader, worker):
            identity = process_identity(leader.pid)
            leader.terminate()
            leader.wait(timeout=5)
            with self.assertRaisesRegex(ValueError, f"surviving group PIDs \\[{worker}\\]"):
                terminate(identity, grace_seconds=0.05)
            self.assertIn(worker, group_members(identity))

    def test_stale_group_identity_preserves_the_leader_and_worker(self):
        with process_group_with_worker() as (leader, worker):
            identity = process_identity(leader.pid)
            identity["start_ticks"] += 1
            self.assertEqual(group_members(identity), {})
            with self.assertRaisesRegex(ValueError, "refusing to signal"):
                terminate(identity, grace_seconds=0.05)
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

    def test_configured_endpoint_conflict_precedes_gpu_inspection_and_launch(self):
        with listener("127.0.0.2") as port:
            selected = [
                (
                    Path("unused"),
                    {
                        "role": "engine-1",
                        "endpoint": f"http://127.0.0.2:{port}",
                        "attestation_port": 0,
                        "side_channel_port": 0,
                    },
                )
            ]
            with (
                patch(
                    "narwhal.deployment.native_engine.validate_shared_runs", return_value=selected
                ),
                patch("narwhal.deployment.native_engine.gpu_memory") as memory,
                patch("narwhal.deployment.native_engine.subprocess.Popen") as popen,
                self.assertRaisesRegex(ValueError, f"engine port {port}.*127.0.0.2"),
            ):
                start_shared([Path("unused")])
            memory.assert_not_called()
            popen.assert_not_called()

    def test_each_native_listener_uses_its_configured_address(self):
        with tempfile.TemporaryDirectory() as folder, listener("127.0.0.1") as port:
            run = Path(folder)
            plan = {
                "role": "engine-1",
                "endpoint": "http://127.0.0.2:0",
                "attestation_port": 0,
                "side_channel_port": 0,
            }
            (run / "engine.env").write_text(
                "VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.3\nVLLM_NIXL_SIDE_CHANNEL_PORT=0\n"
            )
            with patch.dict(os.environ, {}, clear=True):
                plan["endpoint"] = f"http://127.0.0.2:{port}"
                _ports_free([(run, plan)])
                plan["endpoint"] = "http://127.0.0.2:0"
                (run / "engine.env").write_text(
                    f"VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.3\nVLLM_NIXL_SIDE_CHANNEL_PORT={port}\n"
                )
                _ports_free([(run, plan)])
                (run / "engine.env").write_text(
                    f"VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.1\nVLLM_NIXL_SIDE_CHANNEL_PORT={port}\n"
                )
                with self.assertRaisesRegex(ValueError, f"NIXL port {port}.*127.0.0.1"):
                    _ports_free([(run, plan)])
                (run / "engine.env").write_text(
                    "VLLM_NIXL_SIDE_CHANNEL_HOST=127.0.0.3\nVLLM_NIXL_SIDE_CHANNEL_PORT=0\n"
                )
                plan["attestation_url"] = f"http://127.0.0.2:{port}/v1/attestation"
                _ports_free([(run, plan)])
                plan["attestation_url"] = f"http://127.0.0.1:{port}/v1/attestation"
                with self.assertRaisesRegex(ValueError, f"attestation port {port}.*127.0.0.1"):
                    _ports_free([(run, plan)])
                with patch.dict(
                    os.environ,
                    {"NARWHAL_NODE_1_ATTESTATION_URL": f"http://127.0.0.2:{port}/v1/attestation"},
                ):
                    _ports_free([(run, plan)])

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
                            "backend": "native",
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
            live = SimpleNamespace(version="0.29.0", process_start_time_seconds=1234.5)
            with (
                patch(
                    "narwhal.deployment.native_engine.validate_shared_runs", return_value=selected
                ),
                patch("narwhal.deployment.native_engine._ports_free"),
                patch(
                    "narwhal.deployment.native_engine._checked_plan",
                    return_value={"engine_version": "0.29.0"},
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
                self.assertEqual(record["engine_version"], "0.29.0")
                self.assertEqual(record["process_start_time_seconds"], 1234.5)
                self.assertEqual(record["budget_mib"], 6000)
                self.assertEqual(record["device_allowance_mib"], 27000)
                self.assertEqual(record["observed_delta_mib"], 5100)
                self.assertEqual(
                    record["aggregate_delta_mib"], 5100 if plan["role"] == "engine-1" else 10200
                )
                self.assertEqual(record["engine_args"], plan["args"])

    def test_stop_requires_recorded_process_start_and_terminates_owned_group(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            sockets = run / "kv-events"
            sockets.mkdir()
            (sockets / "events.sock").touch()
            (run / "launch.json").write_text(
                json.dumps({"kv_events": {"socket_dir": str(sockets)}})
            )
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
                self.assertFalse(sockets.exists())
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
                            "backend": "native",
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
                    return_value={"engine_version": "0.29.0"},
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
                        version="0.29.0", process_start_time_seconds=1234.5
                    ),
                ),
                patch("narwhal.deployment.native_engine.group_members", return_value={102: 20}),
                patch("narwhal.deployment.native_engine.terminate") as terminate,
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


def own_gpu_run(folder: Path, number: int, gpu: str, **changes) -> Path:
    """A checked-looking native launch directory whose engine runs on `gpu` alone."""
    run = folder / f"engine-{number}"
    run.mkdir()
    (run / "engine.env").write_text(f"CUDA_VISIBLE_DEVICES={gpu}\nVLLM_API_KEY=\n")
    plan = {
        "role": f"engine-{number}",
        "backend": "native",
        "endpoint": f"http://127.0.0.1:{8000 + number}",
        "attestation_port": 8100 + number,
        "side_channel_port": 8200 + number,
        "python_executable": sys.executable,
        "args": ["-m", "vllm.entrypoints.openai.api_server", "--gpu-memory-utilization", "0.2"],
        "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
        "model_revision": "pinned-model-commit",
        "model_config_sha256": "a" * 64,
        "env_sha256": digest(run / "engine.env"),
        **changes,
    }
    (run / "launch.json").write_text(json.dumps(plan))
    return run


class OwnGpuStartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_plans_need_one_gpu_each_named_by_uuid(self):
        runs = [own_gpu_run(self.folder, n, f"GPU-{n}") for n in (1, 2)]
        self.assertEqual(
            [plan["role"] for _, plan in validate_native_runs(runs)],
            [
                "engine-1",
                "engine-2",
            ],
        )
        for name, changes, gpu, message in (
            ("same", {}, "GPU-1", "GPU GPU-1 is also the GPU of engine-1"),
            ("ordinal", {}, "0", "must name one GPU by its UUID"),
            ("pair", {}, "GPU-3,GPU-4", "must name one GPU by its UUID"),
            (
                "shared",
                {"shared_device": {"gpu_uuid": "GPU-3"}},
                "GPU-3",
                "shares its GPU; start it with start-shared",
            ),
            ("memory", {"args": ["-m", "vllm"]}, "GPU-3", "one memory fraction argument"),
            ("port", {"attestation_port": 8101}, "GPU-3", "attestation port 8101 collides"),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as other:
                folder = Path(other)
                first = own_gpu_run(folder, 1, "GPU-1")
                second = own_gpu_run(folder, 2, gpu, **changes)
                with self.assertRaisesRegex(ValueError, message):
                    validate_native_runs([first, second])
        (runs[1] / "native-start.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "engine-2: launch directory already has"):
            validate_native_runs(runs)
        with self.assertRaisesRegex(ValueError, "one to eight distinct"):
            validate_native_runs([runs[0], runs[0]])

    def start(self, runs, readings):
        live = SimpleNamespace(version="0.29.0", process_start_time_seconds=1234.5)
        with (
            patch("narwhal.deployment.native_engine._ports_free"),
            patch(
                "narwhal.deployment.native_engine._checked_plan",
                return_value={"engine_version": "0.29.0"},
            ),
            patch("narwhal.deployment.native_engine.gpu_memory") as memory,
            patch("narwhal.deployment.native_engine.subprocess.Popen") as popen,
            patch("narwhal.deployment.native_engine.process_identity") as identity,
            patch("narwhal.deployment.native_engine._wait_ready"),
            patch(
                "narwhal.deployment.native_engine.fetch_engine_identity",
                new_callable=AsyncMock,
                return_value=live,
            ),
        ):
            memory.side_effect = lambda gpu: {
                "used_mib": readings[gpu].pop(0),
                "total_mib": 30000,
            }
            popen.side_effect = [Mock(pid=100 + n) for n in range(len(runs))]
            identity.side_effect = [
                {"pid": 100 + n, "boot_id": "boot", "start_ticks": n} for n in range(len(runs))
            ]
            start_each(runs, ready_seconds=30)
            return memory, popen

    def test_each_engine_reads_and_records_its_own_gpu(self):
        runs = [own_gpu_run(self.folder, n, f"GPU-{n}") for n in (1, 2)]
        # The second engine grows its GPU past its fraction; no allowance bounds it.
        memory, popen = self.start(runs, {"GPU-1": [1000, 7000], "GPU-2": [0, 20000]})
        self.assertEqual(
            [call.args[0] for call in memory.call_args_list], ["GPU-1", "GPU-1", "GPU-2", "GPU-2"]
        )
        self.assertEqual(popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "GPU-2")
        for run, gpu, delta in ((runs[0], "GPU-1", 6000), (runs[1], "GPU-2", 20000)):
            record = json.loads((run / "native-start.json").read_text())
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["gpu_uuid"], gpu)
            self.assertEqual(record["budget_mib"], 6000)
            self.assertEqual(record["observed_delta_mib"], delta)
            self.assertNotIn("device_allowance_mib", record)
            self.assertFalse((run / "shared-start.json").exists())
            self.assertEqual(read_native_start(run), record)

    def test_an_engine_starts_only_when_its_gpu_has_room_for_its_fraction(self):
        runs = [own_gpu_run(self.folder, n, f"GPU-{n}") for n in (1, 2)]
        with (
            patch("narwhal.deployment.native_engine.stop") as stop_first,
            self.assertRaisesRegex(
                ValueError, "engine-2: native launch failed with 25000 MiB used before start"
            ),
        ):
            self.start(runs, {"GPU-1": [0, 6000], "GPU-2": [25000, 25000]})
        stop_first.assert_called_once_with(runs[0])
        failed = json.loads((runs[1] / "native-start.json").read_text())
        self.assertEqual(failed["status"], "failed")
        self.assertIn("free GPU memory is below the 6000.0 MiB allocation", failed["error"])

    def test_the_cli_starts_each_run_on_its_own_gpu(self):
        runs = [own_gpu_run(self.folder, n, f"GPU-{n}") for n in (1, 2)]
        argv = ["start-native", "--ready-seconds", "60"]
        for run in runs:
            argv.extend(["--run", str(run)])
        with (
            patch.object(engine_cli.native_engine, "start_each") as start,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(engine_cli.main(argv), 0)
        start.assert_called_once_with([run.resolve() for run in runs], 60)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as exit,
        ):
            engine_cli.main(["start-native", "--run", str(runs[0]), "--ready-seconds", "0"])
        self.assertEqual(exit.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
