"""Keep local lifecycle ownership and readiness tied to the selected instance."""

import contextlib
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import FleetConfig
from narwhal.dev import lifecycle, template
from narwhal.dev.cli import main
from narwhal.profiling.probe import Sweep, bounded_sweep

from .fixtures import process_group_with_worker


class DevTests(unittest.TestCase):
    def test_removed_plugin_reports_runtime_package_and_instance(self):
        self.initialize()
        spec = lifecycle.read(self.root / "template.json")
        spec["runtime"]["gguf_plugin_python_sha256"] = "a" * 64
        lifecycle.write(self.root / "template.json", spec)
        with (
            patch.object(
                template.metadata,
                "distribution",
                side_effect=metadata.PackageNotFoundError("vllm-gguf-plugin"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main(["dev", "up", "--instance", str(self.root)]), 2)
        self.assertEqual(stdout.getvalue(), "")
        for text in ("narwhal: dev up", str(self.root), "vllm-gguf-plugin"):
            self.assertIn(text, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

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

    def initialize(self, **overrides):
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
                **overrides,
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

    def test_init_records_the_engine_credential_reference(self):
        with patch.dict(os.environ, {"NARWHAL_ENGINE_API_KEY": "synthetic-key"}):
            self.initialize()
            fleet_path = self.root / "fleet.json"
            fleet = FleetConfig.load(fleet_path)
            self.assertEqual(fleet.engine_api_key_env, "NARWHAL_ENGINE_API_KEY")
            self.assertEqual(fleet.engine_headers(), {"authorization": "Bearer synthetic-key"})
            self.assertNotIn("synthetic-key", fleet_path.read_text())

    def test_up_uses_the_same_credential_for_engines_and_fleet_clients(self):
        with patch.dict(os.environ, {"NARWHAL_ENGINE_API_KEY": ""}):
            self.initialize()

        class Prepared(Exception):
            pass

        for key_env in ("NARWHAL_ENGINE_API_KEY", "CUSTOM_ENGINE_KEY"):
            with self.subTest(key_env=key_env):
                fleet = lifecycle.read(self.root / "fleet.json")
                if key_env == "CUSTOM_ENGINE_KEY":
                    fleet["engine"]["engine_api_key_env"] = key_env
                    lifecycle.write(self.root / "fleet.json", fleet)
                run = self.root / key_env
                run.mkdir()

                def inspect_preparation(runs, run=run, key_env=key_env):
                    cfg = FleetConfig.load(run / "fleet.json")
                    self.assertEqual(cfg.engine_api_key_env, key_env)
                    for engine_run in runs:
                        values = dict(
                            line.split("=", 1)
                            for line in (engine_run / "engine.env").read_text().splitlines()
                        )
                        self.assertEqual(
                            cfg.engine_headers(),
                            {"authorization": f"Bearer {values['VLLM_API_KEY']}"},
                        )
                    raise Prepared()

                with (
                    patch.dict(
                        os.environ,
                        {
                            "NARWHAL_ENGINE_API_KEY": "synthetic-default-key",
                            "CUSTOM_ENGINE_KEY": "synthetic-custom-key",
                        },
                    ),
                    patch.object(lifecycle, "_run"),
                    patch.object(
                        lifecycle.native_engine, "start_shared", side_effect=inspect_preparation
                    ),
                    self.assertRaises(Prepared),
                ):
                    lifecycle._launch(
                        self.root,
                        run,
                        lifecycle.instance(self.root),
                        self.spec,
                        {"run": str(run), "processes": []},
                    )

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

    def test_up_keeps_profile_progress_on_stderr_and_json_on_stdout(self):
        self.initialize()
        with (
            patch.object(lifecycle, "check_plugin"),
            patch.object(lifecycle, "_check_free_ports"),
            patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
            patch.object(lifecycle.native_engine, "start_shared"),
            patch.object(lifecycle, "finalize_fleet"),
            patch.object(lifecycle, "_spawn", return_value={"identity": {}}),
            patch.object(lifecycle, "_wait"),
            patch.object(lifecycle, "_run") as command,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as progress,
        ):
            self.assertEqual(main(["dev", "up", "--instance", str(self.root)]), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "launched")
        self.assertEqual(Path(result["run"]).parent, self.root)
        self.assertEqual(result["router"], lifecycle.instance(self.root)["router_url"])
        self.assertEqual(
            progress.getvalue().splitlines(),
            [
                f"Prepared engine-{number}; review launch.json and run the native runtime check."
                for number in (1, 2, 3, 4)
            ]
            + [f"profiling {prefill} prefill / {4 - prefill} decode" for prefill in (1, 2, 3)],
        )
        self.assertEqual(
            [call.args[3] for call in command.call_args_list],
            [f"engine-{number}" for number in (1, 2, 3, 4)]
            + [f"attest-{number}" for number in (1, 2, 3, 4)]
            + ["profile-1p3d", "profile-2p2d", "profile-3p1d", "profile-merge"],
        )

    def test_reference_sweep_fits_engine_context_and_concurrency(self):
        profile = self.spec["profile"]
        sweep = Sweep(
            prefill_lens=tuple(profile["prefill_lens"]),
            decode_input_lens=tuple(profile["decode_input_lens"]),
            decode_concurrency=tuple(profile["decode_concurrency"]),
            decode_tokens=profile["decode_tokens"],
        )
        runtime = self.spec["runtime"]
        self.assertEqual(
            bounded_sweep(sweep, runtime["max_model_len"], runtime["max_num_seqs"]), sweep
        )

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

    def test_cli_reuse_reports_all_conflicts_and_preserves_operator_edits(self):
        self.initialize()
        (self.root / "fleet.json").write_text("operator fleet edit")
        (self.root / "notes.txt").write_text("operator notes")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
            patch.object(template, "_gpu_rows", side_effect=AssertionError("reuse is local")),
        ):
            result = main(
                [
                    "dev",
                    "init",
                    "--instance",
                    str(self.root),
                    "--engine-count",
                    "2",
                    "--port-base",
                    "19000",
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("--engine-count", errors.getvalue())
        self.assertIn("--port-base", errors.getvalue())
        self.assertIn("narwhal dev init --instance <new-directory>", errors.getvalue())
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_cli_reuse_checks_every_explicit_setting(self):
        self.initialize()
        changed = lifecycle.read(self.root / "template.json")
        changed["runtime"]["max_model_len"] += 1
        template_path = self.parent / "changed-template.json"
        template_path.write_text(json.dumps(changed))
        for flag, value, detail in (
            ("--model", str(self.parent / "other.gguf"), "--model"),
            ("--model-dir", str(self.parent / "other-model"), "--model-dir"),
            ("--gpu", "GPU-other", "--gpu"),
            ("--interface", "eth0", "--interface"),
            ("--gpu-memory-utilization", "0.11", "--gpu-memory-utilization"),
            ("--device-allowance", "0.6", "--device-allowance"),
            ("--template", str(template_path), "--template runtime.max_model_len"),
        ):
            with (
                self.subTest(flag=flag),
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(
                    main(["dev", "init", "--instance", str(self.root), flag, value]), 2
                )
                self.assertIn(detail, errors.getvalue())
                self.assertEqual(output.getvalue(), "")

    def test_cli_matching_and_omitted_settings_reuse_custom_instance(self):
        self.initialize(engine_count=2, port_base=19000, device_allowance=0.4)
        (self.root / "fleet.json").write_text("operator fleet edit")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        matching = [
            "--model",
            str(self.gguf),
            "--model-dir",
            str(self.model),
            "--interface",
            "lo",
            "--gpu",
            "GPU-test",
            "--engine-count",
            "2",
            "--port-base",
            "19000",
            "--gpu-memory-utilization",
            "0.1",
            "--device-allowance",
            "0.4",
            "--template",
            str(self.root / "template.json"),
        ]
        for flags in ([], matching, matching):
            with (
                self.subTest(flags=flags),
                patch.object(
                    template, "reference", side_effect=AssertionError("use saved settings")
                ),
                patch.object(template, "_gpu_rows", side_effect=AssertionError("reuse is local")),
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(main(["dev", "init", "--instance", str(self.root), *flags]), 0)
            self.assertEqual(
                json.loads(output.getvalue()), {"status": "reused", "instance": str(self.root)}
            )
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_materialize_rejects_conflicting_explicit_settings(self):
        self.initialize()
        with self.assertRaisesRegex(ValueError, "--engine-count"):
            template.materialize(self.root, engine_count=2)
        self.spec["allocation"]["engine_count"] = 2
        with self.assertRaisesRegex(ValueError, "--template allocation.engine_count"):
            self.initialize()

    def test_init_help_explains_reuse_and_configuration_changes(self):
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as exit,
        ):
            main(["dev", "init", "--help"])
        self.assertEqual(exit.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        self.assertIn("report reused", help_text)
        self.assertIn("Reuse preserves operator edits", help_text)
        self.assertIn("select a fresh directory with --instance", help_text)

    def test_invalid_memory_and_overlapping_ports_reject_init(self):
        self.spec["allocation"]["device_allowance"] = 0.3
        with self.assertRaisesRegex(ValueError, "budgets exceed"):
            self.initialize()
        self.spec["allocation"]["device_allowance"] = 0.5
        self.spec["ports"]["nixl_first"] = self.spec["ports"]["engine_first"]
        with self.assertRaisesRegex(ValueError, "collide"):
            self.initialize()
        self.assertFalse(self.root.exists())

    def test_decimal_memory_totals_accept_exact_allowance(self):
        for count, allowance in ((3, 0.3), (6, 0.6), (4, 0.4)):
            with self.subTest(count=count, allowance=allowance):
                self.root = self.parent / f"instance-{count}"
                self.spec["allocation"].update(
                    engine_count=count,
                    gpu_memory_utilization=0.1,
                    device_allowance=allowance,
                )
                self.initialize()
                fleet = FleetConfig.load(self.root / "fleet.json")
                self.assertEqual(len(fleet.engines), count)
                self.assertEqual(
                    lifecycle.read(self.root / "template.json")["allocation"],
                    self.spec["allocation"],
                )

    def test_decimal_memory_totals_reject_strict_overage_and_nonfinite_values(self):
        for fraction, allowance in (
            (0.10000000000000002, 0.3),
            (0.1, 0.29999999999999993),
            (float("nan"), 0.3),
            (0.1, float("nan")),
            (float("inf"), 0.3),
            (0.1, float("inf")),
        ):
            with self.subTest(fraction=fraction, allowance=allowance):
                self.spec["allocation"].update(
                    engine_count=3,
                    gpu_memory_utilization=fraction,
                    device_allowance=allowance,
                )
                with self.assertRaisesRegex(ValueError, "budgets exceed"):
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

    def test_down_waits_for_workers_after_the_leader_exits(self):
        self.initialize()
        with process_group_with_worker() as (leader, _):
            identity = lifecycle.native_engine.process_identity(leader.pid)
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
            terminate = lifecycle.native_engine._terminate
            with patch.object(
                lifecycle.native_engine,
                "_terminate",
                side_effect=lambda owned: terminate(owned, grace_seconds=0.05),
            ):
                self.assertEqual(lifecycle.down(self.root)["status"], "stopped")
            self.assertEqual(lifecycle.native_engine._group_members(identity), {})
            self.assertEqual(lifecycle.read(run / "teardown.json")["stopped"], ["router"])

    def test_exited_leader_keeps_surviving_workers_visible_in_status_and_down(self):
        self.initialize()
        with process_group_with_worker() as (leader, worker):
            identity = lifecycle.native_engine.process_identity(leader.pid)
            leader.terminate()
            leader.wait(timeout=5)
            run = self.root / "run-test"
            run.mkdir()
            (run / "fleet.json").write_bytes((self.root / "fleet.json").read_bytes())
            lifecycle.write(
                self.root / "lifecycle.json",
                {
                    "phase": "stopped",
                    "run": str(run),
                    "processes": [{"name": "router", "identity": identity}],
                },
            )
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
            with patch.object(
                lifecycle.httpx, "Client", return_value=httpx.Client(transport=transport)
            ):
                result = lifecycle.status(self.root)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["surviving_processes"], {"router": [worker]})
            with self.assertRaisesRegex(ValueError, "require operator inspection"):
                lifecycle.down(self.root)
            state = lifecycle.read(self.root / "lifecycle.json")
            self.assertEqual(state["phase"], "degraded")
            self.assertIn(str(worker), state["errors"][0])
            self.assertIn(worker, lifecycle.native_engine._group_members(identity))

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

    def launched_instance(self):
        self.initialize()
        run = self.root / "run-test"
        run.mkdir()
        (run / "fleet.json").write_bytes((self.root / "fleet.json").read_bytes())
        lifecycle.write(
            self.root / "lifecycle.json",
            {
                "phase": "launched",
                "run": str(run),
                "processes": [{"name": f"p{i}", "identity": {}} for i in range(9)],
            },
        )
        return run

    def test_failed_canary_survives_status_until_successful_verification(self):
        run = self.launched_instance()
        answer = "4"
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": answer}}]}
            )
        )
        client = httpx.Client
        with (
            patch.object(lifecycle.native_engine, "_owns_process", return_value=True),
            patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
            patch.object(lifecycle, "_run") as preflight,
            patch.object(
                lifecycle.httpx, "Client", side_effect=lambda **kwargs: client(transport=transport)
            ),
            patch.object(lifecycle, "verify_directed_kv_evidence", return_value=[]),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(main(["dev", "verify", "--instance", str(self.root)]), 1)
            state = lifecycle.read(self.root / "lifecycle.json")
            self.assertEqual(state["phase"], "degraded")
            failure = state["verification_failure"]
            self.assertIn("routed arithmetic canary expected 5", errors.getvalue())
            self.assertIn("routed arithmetic canary expected 5", failure["reason"])
            evidence = Path(failure["evidence"])
            self.assertEqual(evidence.parent, run)
            self.assertEqual(lifecycle.read(evidence / "failure.json"), failure)
            self.assertEqual(
                lifecycle.read(evidence / "completion.json")["choices"][0]["message"]["content"],
                "4",
            )
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["dev", "status", "--instance", str(self.root)]), 1)
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "degraded")
                self.assertEqual(result["verification_failure"], failure)
                self.assertEqual(result["problems"], [f"verification failed: {failure['reason']}"])

            def inspect_retry(*args):
                result = lifecycle.status(self.root)
                self.assertEqual(result["status"], "degraded")
                self.assertEqual(result["verification_failure"], failure)

            preflight.side_effect = inspect_retry
            answer = "5"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["dev", "verify", "--instance", str(self.root)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["problems"], [])
            self.assertNotIn("verification_failure", result)
            state = lifecycle.read(self.root / "lifecycle.json")
            self.assertEqual(state["phase"], "ready")
            self.assertNotIn("verification_failure", state)
            self.assertNotEqual(state["verification"], str(evidence))
            self.assertEqual(lifecycle.read(evidence / "failure.json"), failure)

    def test_completed_teardown_resolves_failed_verification(self):
        run = self.launched_instance()
        state = lifecycle.read(self.root / "lifecycle.json")
        failure = {"reason": "failed qualification", "evidence": str(run / "verify-test")}
        state.update(phase="degraded", verification_failure=failure)
        lifecycle.write(self.root / "lifecycle.json", state)
        with (
            patch.object(lifecycle.native_engine, "_group_members", return_value={1: {}}),
            patch.object(lifecycle.native_engine, "_terminate", side_effect=ValueError("owned")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["dev", "down", "--instance", str(self.root)]), 1)
        state = lifecycle.read(self.root / "lifecycle.json")
        self.assertEqual(state["phase"], "degraded")
        self.assertEqual(state["verification_failure"], failure)
        with (
            patch.object(lifecycle.native_engine, "_group_members", return_value={}),
            patch.object(lifecycle.native_engine, "_owns_process", return_value=False),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["dev", "down", "--instance", str(self.root)]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "stopped")
        self.assertNotIn("verification_failure", lifecycle.read(self.root / "lifecycle.json"))

    def test_new_instance_status_and_down_are_stopped(self):
        self.initialize()
        for operation in (lifecycle.status, lifecycle.down, lifecycle.down):
            self.assertEqual(operation(self.root)["status"], "stopped")
        self.assertEqual(main(["dev", "status", "--instance", str(self.root)]), 0)

    def test_malformed_lifecycle_reports_its_path_and_field_before_operating(self):
        self.initialize()
        path = self.root / "lifecycle.json"
        for document, field in (
            ({}, "run"),
            ({"run": [], "phase": "launched", "processes": []}, "run"),
            ({"run": str(self.root), "phase": "launched", "processes": {}}, "processes"),
            (
                {"run": str(self.root), "phase": "ready", "processes": [], "verification": 123},
                "verification",
            ),
        ):
            lifecycle.write(path, document)
            original = path.read_bytes()
            for action in ("up", "verify", "status", "down"):
                with (
                    self.subTest(document=document, action=action),
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    self.assertEqual(main(["dev", action, "--instance", str(self.root)]), 2)
                self.assertEqual(stdout.getvalue(), "")
                for text in (f"narwhal: dev {action}", str(path), field):
                    self.assertIn(text, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual(path.read_bytes(), original)

    def test_malformed_completion_retains_failure_and_degraded_status(self):
        run = self.launched_instance()
        client = httpx.Client
        for result in ({}, [], {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            transport = httpx.MockTransport(
                lambda request, body=result: httpx.Response(200, json=body)
            )
            with (
                self.subTest(result=result),
                patch.object(lifecycle.native_engine, "_owns_process", return_value=True),
                patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
                patch.object(lifecycle, "_run"),
                patch.object(
                    lifecycle.httpx,
                    "Client",
                    side_effect=lambda transport=transport, **kwargs: client(transport=transport),
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(main(["dev", "verify", "--instance", str(self.root)]), 1)
                self.assertEqual(stdout.getvalue(), "")
                state = lifecycle.read(self.root / "lifecycle.json")
                self.assertEqual(state["phase"], "degraded")
                failure = state["verification_failure"]
                evidence = Path(failure["evidence"])
                self.assertEqual(evidence.parent, run)
                self.assertEqual(json.loads((evidence / "completion.json").read_text()), result)
                self.assertEqual(lifecycle.read(evidence / "failure.json"), failure)
                self.assertIn("choices[0].message.content", failure["reason"])
                self.assertIn(str(evidence / "completion.json"), stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                with contextlib.redirect_stdout(io.StringIO()) as status:
                    self.assertEqual(main(["dev", "status", "--instance", str(self.root)]), 1)
                current = json.loads(status.getvalue())
                self.assertEqual(current["status"], "degraded")
                self.assertEqual(current["verification_failure"], failure)

    def test_unexpected_lifecycle_code_error_propagates(self):
        self.initialize()
        with (
            patch.object(lifecycle, "status", side_effect=KeyError("implementation")),
            self.assertRaisesRegex(KeyError, "implementation"),
        ):
            main(["dev", "status", "--instance", str(self.root)])

    def test_lifecycle_lock_rejects_overlapping_mutations(self):
        self.initialize()
        with lifecycle.locked(self.root), self.assertRaisesRegex(ValueError, "another lifecycle"):
            lifecycle.down(self.root)

    def test_tokenizer_mismatch_rejects_before_gpu_access(self):
        self.spec["model"]["tokenizer_sha256"] = {"config.json": "0" * 64}
        with self.assertRaisesRegex(ValueError, "tokenizer file config.json"):
            self.initialize()
