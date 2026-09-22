"""Check replica-wide cache sizing, workload bounds and receiver throughput units."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.fabric_budget import cache_shape, calculate, main, received_gbps


class FabricBudgetTests(unittest.TestCase):
    def test_gqa_shards_and_mqa_replicas_count_all_rank_bytes(self):
        model = {
            "num_hidden_layers": 32,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "hidden_size": 4096,
        }
        self.assertEqual(cache_shape(model, 8, 2), ("attention", 131072))
        model["num_key_value_heads"] = 1
        self.assertEqual(cache_shape(model, 8, 2), ("attention", 131072))
        self.assertEqual(cache_shape({"text_config": model}, 1, 2), ("attention", 16384))

    def test_mla_counts_one_latent_per_rank_without_extra_kv_factor(self):
        model = {"num_hidden_layers": 61, "kv_lora_rank": 512, "qk_rope_head_dim": 64}
        self.assertEqual(cache_shape(model, 8, 2), ("mla", 562176))
        with self.assertRaisesRegex(ValueError, "packed MLA"):
            cache_shape(model, 8, 1)

    def test_windowed_cache_and_invalid_shards_require_explicit_correction(self):
        model = {"num_hidden_layers": 32, "num_attention_heads": 32, "hidden_size": 4096}
        with self.assertRaisesRegex(ValueError, "shard"):
            cache_shape(model, 3, 2)
        model["layer_types"] = ["full_attention", "linear_attention"]
        with self.assertRaisesRegex(ValueError, "measured"):
            cache_shape(model, 8, 2)

    def test_padding_burst_and_decimal_gigabits(self):
        budget = calculate(1000, 129, 128, 1, 2, 0.5, 1.25)
        self.assertEqual(budget["padded_tokens"], 256)
        self.assertEqual(budget["payload_bytes_per_handoff"], 256000)
        self.assertAlmostEqual(budget["required_gbps"], 0.01024)
        self.assertGreater(
            calculate(1000, 129, 128, 8, 2, 0.5, 1.25)["required_gbps"], budget["required_gbps"]
        )

    def test_invalid_workload_cannot_produce_a_passing_zero_budget(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(1000, 128, 128, value, 1, 1, 1.25)
        with self.assertRaises(ValueError):
            calculate(1000, 128, 128, 1, 1, 1, 0.9)

    def test_comparison_uses_received_bytes_and_rejects_iperf_errors(self):
        sample = {
            "start": {"test_start": {"protocol": "TCP"}},
            "end": {
                "sum_received": {"bits_per_second": 2e9, "seconds": 10},
                "sum_sent": {"bits_per_second": 3e9},
            },
        }
        self.assertEqual(received_gbps(sample), 2)
        with tempfile.TemporaryDirectory() as folder:
            budget = Path(folder) / "budget.json"
            data = Path(folder) / "sample.json"
            budget.write_text(json.dumps({"required_gbps": 2.5}))
            data.write_text(json.dumps(sample))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["compare", "--budget", str(budget), "--iperf", str(data)]), 1
                )
                self.assertEqual(main(["compare", "--budget", str(budget), "--gbps", "2.5"]), 0)
        sample["error"] = "synthetic connection error"
        with self.assertRaisesRegex(ValueError, "iperf3 reported"):
            received_gbps(sample)

    def test_cli_writes_private_budget_and_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model, launch, out = (
                root / name for name in ("model.json", "launch.json", "budget.json")
            )
            model.write_text(
                json.dumps({"num_hidden_layers": 2, "kv_lora_rank": 8, "qk_rope_head_dim": 4})
            )
            launch.write_text(json.dumps({"tensor_parallel_size": 2}))
            args = [
                "calculate",
                "--uniform-cache",
                "--model-config",
                str(model),
                "--launch-config",
                str(launch),
                "--element-bytes",
                "2",
                "--prompt-tokens",
                "1024",
                "--block-tokens",
                "128",
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
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                initial = out.read_bytes()
                self.assertEqual(out.stat().st_mode & 0o777, 0o600)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(args)
                self.assertEqual(out.read_bytes(), initial)
