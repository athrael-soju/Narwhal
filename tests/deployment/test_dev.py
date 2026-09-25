"""Keep local lifecycle ownership and readiness tied to the selected instance."""

import contextlib
import hashlib
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.dev import lifecycle, template
from narwhal.dev.cli import main


class DevTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "instance"
        self.model = self.parent / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}")
        self.gguf = self.model / "synthetic.gguf"
        self.gguf.write_bytes(b"synthetic")
        self.spec = template.reference()
        self.spec["model"].update(
            filename=self.gguf.name,
            sha256=hashlib.sha256(b"synthetic").hexdigest(),
            tokenizer_sha256={},
        )
        self.spec["runtime"].pop("gguf_plugin_python_sha256", None)

    def initialize(self):
        with (
            patch.object(
                template,
                "_gpu_rows",
                return_value=[{"name": self.spec["gpu"]["product"], "uuid": "GPU-test"}],
            ),
            patch.object(
                template, "gpu_memory", return_value={"total_mib": 32607, "used_mib": 2000}
            ),
            patch.object(template, "_check_runtime"),
            patch.object(template, "_address", return_value="127.0.0.1"),
            patch.object(template, "_check_free_ports"),
        ):
            return template.materialize(
                self.root,
                model_dir=self.model,
                model_path=self.gguf,
                fabric_interface="lo",
                template=self.spec,
            )

    def test_four_roles_and_unique_loopback_ports(self):
        self.initialize()
        fleet = lifecycle.read(self.root / "fleet.json")
        self.assertEqual(
            [e["role"] for e in fleet["engines"]], ["prefill", "prefill", "decode", "decode"]
        )
        self.assertTrue(all(e["url"].startswith("http://127.0.0.1:") for e in fleet["engines"]))
        _, ports = template._port_layout(self.spec, 4)
        self.assertEqual(len(ports), 13)

    def test_four_engine_profiles_cover_both_adjacent_splits(self):
        self.initialize()
        fleet = lifecycle.read(self.root / "fleet.json")
        run = self.root / "run-profiles"
        run.mkdir()
        with patch.object(lifecycle, "_run") as command:
            lifecycle._profiles(run, fleet, self.spec)
        measured = {
            tuple(e["role"] for e in lifecycle.read(path)["engines"])
            for path in run.glob("profile-*.fleet.json")
        }
        self.assertEqual(
            measured,
            {
                ("prefill", "decode", "decode", "decode"),
                ("prefill", "prefill", "decode", "decode"),
                ("prefill", "prefill", "prefill", "decode"),
            },
        )
        merge_args = command.call_args.args[2]
        merged = [merge_args[i + 1] for i, arg in enumerate(merge_args) if arg == "--merge"]
        self.assertEqual(set(merged), {str(run / f"profiles-{p}p{4 - p}d.json") for p in (1, 2, 3)})

    def test_repeated_init_preserves_the_existing_instance(self):
        self.initialize()
        path = self.root / "fleet.json"
        path.write_text("operator edit")
        with patch.object(template, "_gpu_rows", side_effect=AssertionError("should preserve")):
            template.materialize(
                self.root,
                model_dir=self.model,
                model_path=self.gguf,
                fabric_interface="lo",
                template=self.spec,
            )
        self.assertEqual(path.read_text(), "operator edit")

    def test_invalid_memory_and_overlapping_ports_reject_init(self):
        self.spec["allocation"]["device_allowance"] = 0.3
        with self.assertRaisesRegex(ValueError, "budgets exceed"):
            self.initialize()
        self.spec["allocation"]["device_allowance"] = 0.5
        self.spec["ports"]["nixl_first"] = self.spec["ports"]["engine_first"]
        with self.assertRaisesRegex(ValueError, "collide"):
            self.initialize()
        self.assertFalse(self.root.exists())

    def test_model_replacement_after_init_rejects_up(self):
        self.initialize()
        self.gguf.write_bytes(b"replacement model")
        with self.assertRaisesRegex(ValueError, "GGUF model changed"):
            lifecycle.up(self.root)
        self.assertFalse((self.root / "lifecycle.json").exists())

    def test_interpreter_aliases_share_an_environment_but_other_venvs_do_not(self):
        self.initialize()
        config = lifecycle.read(self.root / "instance.json")
        aliases = self.parent / "venv" / "bin"
        aliases.mkdir(parents=True)
        for name in ("python", "python3"):
            (aliases / name).symlink_to(sys.executable)
        config["python_executable"] = str(aliases / "python3")
        lifecycle.write(self.root / "instance.json", config)
        with patch.object(lifecycle.sys, "executable", str(aliases / "python")):
            self.assertEqual(lifecycle.instance(self.root), config)
        other = self.parent / "other" / "bin"
        other.mkdir(parents=True)
        (other / "python").symlink_to(sys.executable)
        with (
            patch.object(lifecycle.sys, "executable", str(other / "python")),
            self.assertRaisesRegex(ValueError, "Python environment"),
        ):
            lifecycle.instance(self.root)

    def test_occupied_port_rejects_up_before_process_creation(self):
        self.initialize()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            spec = lifecycle.read(self.root / "template.json")
            spec["ports"]["router"] = occupied.getsockname()[1]
            lifecycle.write(self.root / "template.json", spec)
            with patch.object(lifecycle, "_launch") as launch:
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    lifecycle.up(self.root)
                launch.assert_not_called()
        self.assertFalse((self.root / "lifecycle.json").exists())

    def child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

        self.addCleanup(cleanup)
        return process

    def test_partial_startup_stops_the_process_it_created(self):
        self.initialize()
        process = self.child()
        identity = lifecycle.native_engine.process_identity(process.pid)

        def failed_launch(root, run, config, spec, state):
            engine = run / "engine-1"
            engine.mkdir()
            lifecycle.write(engine / "native-process.json", identity)
            raise ValueError("engine-2 model load failed")

        with (
            patch.object(lifecycle, "_launch", side_effect=failed_launch),
            patch.object(lifecycle, "_check_free_ports"),
            patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
            self.assertRaisesRegex(ValueError, "engine-2 model load failed"),
        ):
            lifecycle.up(self.root)
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(lifecycle.read(self.root / "lifecycle.json")["phase"], "stopped")

    def test_stale_pid_record_cannot_stop_a_reused_process(self):
        self.initialize()
        process = self.child()
        identity = lifecycle.native_engine.process_identity(process.pid)
        identity["start_ticks"] += 1
        run = self.root / "run-test"
        run.mkdir()
        lifecycle.write(
            self.root / "lifecycle.json",
            {
                "phase": "launched",
                "run": str(run),
                "processes": [{"name": "router", "identity": identity}],
            },
        )
        self.assertEqual(lifecycle.down(self.root)["status"], "stopped")
        self.assertIsNone(process.poll())

    def test_saved_ready_state_requires_current_kv_evidence(self):
        self.initialize()
        run = self.root / "run-test"
        run.mkdir()
        (run / "fleet.json").write_bytes((self.root / "fleet.json").read_bytes())
        state = {
            "phase": "ready",
            "run": str(run),
            "verification": str(run / "verify-test"),
            "processes": [{"name": f"p{i}", "identity": {}} for i in range(9)],
        }
        lifecycle.write(self.root / "lifecycle.json", state)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        with (
            patch.object(lifecycle.native_engine, "_owns_process", return_value=True),
            patch.object(lifecycle.httpx, "Client", return_value=httpx.Client(transport=transport)),
            patch.object(
                lifecycle, "verify_directed_kv_evidence", return_value=["consumer process changed"]
            ),
        ):
            result = lifecycle.status(self.root)
        self.assertEqual(result["status"], "degraded")
        self.assertIn("consumer process changed", result["problems"])

    def test_new_instance_status_and_down_are_stopped(self):
        self.initialize()
        for operation in (lifecycle.status, lifecycle.down, lifecycle.down):
            self.assertEqual(operation(self.root)["status"], "stopped")
        self.assertEqual(main(["dev", "status", "--instance", str(self.root)]), 0)

    def test_lifecycle_lock_rejects_overlapping_mutations(self):
        self.initialize()
        with lifecycle.locked(self.root), self.assertRaisesRegex(ValueError, "another lifecycle"):
            lifecycle.down(self.root)

    def test_tokenizer_mismatch_rejects_before_gpu_access(self):
        self.spec["model"]["tokenizer_sha256"] = {"config.json": "0" * 64}
        with self.assertRaisesRegex(ValueError, "tokenizer file config.json"):
            self.initialize()
