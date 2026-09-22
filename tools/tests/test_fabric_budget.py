"""Check replica-wide cache sizing, workload bounds and receiver throughput units."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.deployment.fabric_budget import cache_shape, calculate, main, received_gbps


class FabricBudgetTests(unittest.TestCase):
    def test_retained_edge_is_reused_only_for_matching_link_and_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            link = {
                "source_role": "engine-1",
                "destination_role": "engine-2",
                "source_address": "192.0.2.1",
                "destination_address": "192.0.2.2",
                "source_interface": "eth1",
                "destination_interface": "eth1",
                "source_route": "192.0.2.2 from 192.0.2.1 dev eth1",
                "destination_route": "192.0.2.1 from 192.0.2.2 dev eth1",
                "transport": "ucx_tcp",
                "tool_version": "iperf 3.16",
                "test_parameters": {"parallel": 8, "omit_s": 3, "duration_s": 10, "port": 5201},
            }
            sample = {
                "start": {"test_start": {"protocol": "TCP"}},
                "end": {"sum_received": {"bits_per_second": 12e9, "seconds": 10}},
            }
            paths = {
                name: root / name
                for name in (
                    "link.json",
                    "sample.json",
                    "budget.json",
                    "evidence.json",
                    "revised.json",
                    "comparison.json",
                )
            }
            paths["link.json"].write_text(json.dumps(link))
            paths["sample.json"].write_text(json.dumps(sample))
            paths["budget.json"].write_text(json.dumps({"required_gbps": 10}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "record-edge",
                            "--link",
                            str(paths["link.json"]),
                            "--budget",
                            str(paths["budget.json"]),
                            "--sample",
                            str(paths["sample.json"]),
                            "--out",
                            str(paths["evidence.json"]),
                        ]
                    ),
                    0,
                )
            self.assertEqual(paths["evidence.json"].stat().st_mode & 0o777, 0o600)
            paths["revised.json"].write_text(json.dumps({"required_gbps": 11}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "reuse-edge",
                            "--link",
                            str(paths["link.json"]),
                            "--evidence",
                            str(paths["evidence.json"]),
                            "--sample",
                            str(paths["sample.json"]),
                            "--budget",
                            str(paths["revised.json"]),
                            "--out",
                            str(paths["comparison.json"]),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(paths["comparison.json"].read_text())["required_gbps"], 11)
            link["destination_route"] = "192.0.2.1 from 192.0.2.2 dev eth2"
            paths["link.json"].write_text(json.dumps(link))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(
                    [
                        "reuse-edge",
                        "--link",
                        str(paths["link.json"]),
                        "--evidence",
                        str(paths["evidence.json"]),
                        "--sample",
                        str(paths["sample.json"]),
                        "--budget",
                        str(paths["revised.json"]),
                        "--out",
                        str(root / "changed.json"),
                    ]
                )
            self.assertFalse((root / "changed.json").exists())

    def test_link_command_records_route_files_and_measurement_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source-route.txt"
            destination = root / "destination-route.txt"
            output = root / "link.json"
            source.write_text("192.0.2.2 from 192.0.2.1 dev eth1\n")
            destination.write_text("192.0.2.1 from 192.0.2.2 dev eth1\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "link",
                            "--source-role",
                            "engine-1",
                            "--destination-role",
                            "engine-2",
                            "--source-address",
                            "192.0.2.1",
                            "--destination-address",
                            "192.0.2.2",
                            "--source-interface",
                            "eth1",
                            "--destination-interface",
                            "eth1",
                            "--source-route",
                            str(source),
                            "--destination-route",
                            str(destination),
                            "--transport",
                            "ucx_tcp",
                            "--tool-version",
                            "iperf 3.16",
                            "--test-parameters",
                            '{"parallel":8,"duration_s":10}',
                            "--out",
                            str(output),
                        ]
                    ),
                    0,
                )
            record = json.loads(output.read_text())
            self.assertEqual(record["source_route"], source.read_text().strip())
            self.assertEqual(record["destination_route"], destination.read_text().strip())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

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
