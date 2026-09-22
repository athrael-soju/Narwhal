"""Exercise hybrid page budgeting and the isolated runtime probe with synthetic specs."""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from tools.fabric_budget import main, runtime_payload
from tools.launch_engine import (
    cache_groups,
    digest,
    measure_cache,
    model_dimensions,
    runtime_cache_probe,
    runtime_model_dimensions,
)
from tools.tests import test_launch_engine


def allocation():
    attention = type("FullAttentionSpec", (), {"block_size": 768, "page_size_bytes": 1536})()
    mamba = type(
        "MambaSpec",
        (),
        {
            "block_size": 768,
            "page_size_bytes": 1536,
            "num_speculative_blocks": 0,
            "num_prefill_checkpoint_blocks": 0,
        },
    )()
    return [
        SimpleNamespace(layer_names=["attention"], kv_cache_spec=attention),
        SimpleNamespace(layer_names=["state"], kv_cache_spec=mamba),
    ]


def layout():
    return {
        "schema_version": 1,
        "sizing": "runtime_padded_page_upper_bound",
        "ranks": [{"rank": rank, "layers": cache_groups(allocation())} for rank in range(2)],
    }


class CacheSizingTests(unittest.TestCase):
    def test_model_contract_uses_runtime_getters_for_mla_dimensions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = test_launch_engine.EngineLauncherTests().inputs(root)
            from tools.launch_engine import prepare

            prepare(root / "plan", env)
            path = root / "plan/launch.json"
            for head_size, use_mla in ((576, True), (192, False)):
                model = SimpleNamespace(
                    get_head_size=Mock(return_value=head_size),
                    get_total_num_kv_heads=Mock(return_value=8),
                    get_total_num_hidden_layers=Mock(return_value=48),
                    use_mla=use_mla,
                    hf_text_config=SimpleNamespace(
                        qk_nope_head_dim=128, qk_rope_head_dim=64, v_head_dim=128, kv_lora_rank=512
                    ),
                )
                with (
                    self.subTest(use_mla=use_mla),
                    patch(
                        "tools.launch_engine.runtime_config",
                        return_value=SimpleNamespace(model_config=model),
                    ),
                ):
                    value = runtime_model_dimensions(path)
                self.assertEqual(
                    value["contract"], {"head_size": head_size, "kv_heads": 8, "hidden_layers": 48}
                )
                self.assertEqual(value["sources"]["head_size"], "ModelConfig.get_head_size()")
                self.assertEqual(value["use_mla"], use_mla)
                self.assertEqual(value["plan_sha256"], digest(path))
                model.get_head_size.assert_called_once_with()

    def test_dimension_inspection_checks_plan_and_preserves_existing_capture(self):
        from tools.launch_engine import load, prepare

        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                _, env = test_launch_engine.EngineLauncherTests().inputs(root)
                run = root / "plan"
                prepare(run, env)
                plan = load(run)
                (run / "checked.json").write_text(
                    json.dumps({"plan_sha256": digest(run / "launch.json")})
                )
                value = {
                    "plan_sha256": "0" * 64 if stale else digest(run / "launch.json"),
                    "contract": {"head_size": 576, "kv_heads": 8, "hidden_layers": 48},
                }
                with patch(
                    "tools.launch_engine.docker",
                    return_value="runtime log\nNARWHAL_MODEL_DIMENSIONS=" + json.dumps(value),
                ) as docker:
                    if stale:
                        with self.assertRaisesRegex(ValueError, "must match this plan"):
                            model_dimensions(run, plan)
                        self.assertFalse((run / "model-dimensions.json").exists())
                    else:
                        model_dimensions(run, plan)
                        capture = run / "model-dimensions.json"
                        self.assertEqual(json.loads(capture.read_text()), value)
                        self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                        with self.assertRaisesRegex(ValueError, "dimensions exist"):
                            model_dimensions(run, plan)
                    self.assertEqual(docker.call_count, 1)
                    command = docker.call_args.args[0]
                    self.assertIn("_model-dimensions", command)
                    self.assertIn("--rm", command)

    def test_hybrid_pages_round_each_rank_and_include_padded_state_boundary(self):
        # 1024 tokens: two attention pages and three padded state pages per rank.
        self.assertEqual(runtime_payload(layout(), 2, 1024), 2 * (2 + 3) * 1536)
        self.assertEqual(runtime_payload(layout(), 2, 768), 2 * (1 + 2) * 1536)
        self.assertEqual(runtime_payload(layout(), 2, 769), 2 * (2 + 3) * 1536)

    def test_uniform_groups_expand_per_layer_and_unknown_specs_stop_sizing(self):
        groups = allocation()
        combined = SimpleNamespace(
            kv_cache_specs={"a": groups[0].kv_cache_spec, "b": groups[1].kv_cache_spec}
        )
        self.assertEqual(len(cache_groups([SimpleNamespace(kv_cache_spec=combined)])), 2)
        groups[0].kv_cache_spec = SimpleNamespace(block_size=768, page_size_bytes=1536)
        with self.assertRaisesRegex(ValueError, "page bound"):
            cache_groups(groups)

    def test_missing_ranks_duplicates_and_invalid_pages_fail(self):
        for failure in (
            "missing",
            "duplicate_rank",
            "duplicate_layer",
            "zero_page",
            "negative_extra",
        ):
            value = layout()
            if failure == "missing":
                value["ranks"].pop()
            elif failure == "duplicate_rank":
                value["ranks"][1]["rank"] = 0
            elif failure == "duplicate_layer":
                value["ranks"][0]["layers"] *= 2
            elif failure == "zero_page":
                value["ranks"][0]["layers"][0]["page_bytes"] = 0
            else:
                value["ranks"][0]["layers"][0]["extra_blocks"] = -1
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                runtime_payload(value, 2, 1024)

    def test_runtime_budget_binds_sources_and_recompares_retained_sample(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            model, launch, record, out = [
                root / name for name in ("model", "launch", "layout", "budget")
            ]
            model.write_text('{"layer_types": ["attention", "mamba"]}')
            launch.write_text('{"tensor_parallel_size": 2}')
            value = layout()
            value.update(
                model_config_sha256=digest(model),
                launch_config_sha256=digest(launch),
                image="sha256:" + "a" * 64,
                plan_sha256="b" * 64,
            )
            record.write_text(json.dumps(value))
            args = [
                "calculate",
                "--model-config",
                str(model),
                "--launch-config",
                str(launch),
                "--runtime-layout",
                str(record),
                "--prompt-tokens",
                "1024",
                "--handoffs-per-s",
                "1",
                "--burst",
                "1",
                "--transfer-budget-s",
                "1",
                "--headroom",
                "1.25",
                "--out",
                str(out),
            ]
            self.assertEqual(main(args), 0)
            budget = json.loads(out.read_text())
            self.assertEqual(budget["payload_bytes_per_handoff"], 15360)
            self.assertAlmostEqual(budget["required_gbps"], 0.0001536)
            self.assertEqual(budget["runtime_layout_sha256"], digest(record))
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            sample = root / "retained-sample.json"
            sample.write_text(
                json.dumps(
                    {
                        "start": {"test_start": {"protocol": "TCP"}},
                        "end": {"sum_received": {"bits_per_second": 116.44e9, "seconds": 10}},
                    }
                )
            )
            original = sample.read_bytes()
            self.assertEqual(main(["compare", "--budget", str(out), "--iperf", str(sample)]), 0)
            self.assertEqual(sample.read_bytes(), original)
            out.unlink()
            launch.write_text('{"tensor_parallel_size": 8}')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
            self.assertFalse(out.exists())

    def test_runtime_probe_collects_final_allocation_then_shuts_down(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan_path = root / "launch.json"
            plan = {
                "args": [
                    "-m",
                    "vllm.entrypoints.openai.api_server",
                    "--model",
                    "/model",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--block-size",
                    "128",
                ],
                "model_config_sha256": hashlib.sha256(b"{}").hexdigest(),
                "image": "sha256:" + "a" * 64,
                "expected_packages": {"vllm": "0.29.0"},
                "revision": "b" * 40,
                "launch_sha256": "c" * 64,
                "launcher_sha256": "d" * 64,
            }
            plan_path.write_text(json.dumps(plan))
            config = SimpleNamespace(parallel_config=SimpleNamespace(tensor_parallel_size=2))
            shutdown = Mock()

            class Executor:
                def __init__(self, config):
                    pass

                def shutdown(self):
                    shutdown()

            parser = Mock()
            args_class = Mock()
            args_class.add_cli_args.return_value = parser
            args_class.from_cli_args.return_value.create_engine_config.return_value = config
            factory = Mock()
            factory.get_class.return_value = Executor

            def core(config, executor_class, log_stats):
                executor_class(config).initialize_from_config(
                    [SimpleNamespace(kv_cache_groups=allocation()) for _ in range(2)]
                )
                self.fail("Probe continued beyond final cache planning")

            modules = {}
            for name, attr, value in (
                ("vllm.engine.arg_utils", "EngineArgs", args_class),
                ("vllm.utils.argparse_utils", "FlexibleArgumentParser", Mock()),
                ("vllm.v1.engine.core", "EngineCore", core),
                ("vllm.v1.executor.abstract", "Executor", factory),
            ):
                module = ModuleType(name)
                setattr(module, attr, value)
                modules[name] = module
            real_digest = digest
            with (
                patch.dict(sys.modules, modules),
                patch(
                    "tools.launch_engine.digest",
                    side_effect=lambda path: (
                        plan["model_config_sha256"]
                        if str(path) == "/model/config.json"
                        else real_digest(path)
                    ),
                ),
            ):
                runtime_cache_probe(plan_path)
            shutdown.assert_called_once()
            parser.parse_args.assert_called_once_with(["--model", "/model", "--block-size", "128"])
            result = json.loads((root / "cache-layout.pending.json").read_text())
            self.assertEqual(runtime_payload(result, 2, 1024), 15360)
            self.assertEqual(result["plan_sha256"], digest(plan_path))

    def test_failed_probe_retains_id_and_success_requires_exit_and_matching_plan(self):
        from tools.launch_engine import load, prepare

        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                _, env = test_launch_engine.EngineLauncherTests().inputs(root)
                run = root / "launch"
                prepare(run, env)
                plan = load(run)
                (run / "checked.json").write_text(
                    json.dumps({"plan_sha256": digest(run / "launch.json")})
                )
                value = layout()
                value["plan_sha256"] = digest(run / "launch.json")
                (run / "cache-layout.pending.json").write_text(json.dumps(value))
                responses = [
                    "c" * 64,
                    "probe output",
                    json.dumps({"Running": False, "ExitCode": int(failed)}),
                    "removed",
                ]
                with patch("tools.launch_engine.docker", side_effect=responses) as docker:
                    if failed:
                        with self.assertRaisesRegex(ValueError, "cache probe failed"):
                            measure_cache(run, plan)
                        self.assertEqual(docker.call_count, 3)
                    else:
                        measure_cache(run, plan)
                        self.assertEqual(docker.call_args.args[0], ["rm", "c" * 64])
                self.assertEqual((run / "cache-probe.id").read_text().strip(), "c" * 64)
                self.assertEqual((run / "cache-layout.json").exists(), not failed)
