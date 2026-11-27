"""Check workload budgets, sample rates and the evidence that binds samples to budgets."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from narwhal.deployment.fabric_budget import (
    TRANSFER_SCHEMA,
    Workload,
    budget,
    rdma_gbps,
    received_gbps,
    record_edge,
    reuse_edge,
    runtime_layout_name,
    workload_budget,
)

WORKLOAD = Workload(1024, 1, 1, 1, 1.25)
LINK = {
    "source_host": "node-1",
    "destination_host": "node-2",
    "source_address": "192.0.2.1",
    "destination_address": "192.0.2.2",
    "source_interface": "eth1",
    "destination_interface": "eth1",
    "source_route": '{"dev": "eth1", "dst": "192.0.2.2", "from": "192.0.2.1"}',
    "destination_route": '{"dev": "eth1", "dst": "192.0.2.1", "from": "192.0.2.2"}',
    "transport": "ucx_tcp",
    "tool_version": "iperf 3.16 / iperf 3.16",
    "test_parameters": {"parallel": 8, "omit_s": 3, "duration_s": 10, "port": 5201},
}
TCP_SAMPLE = {
    "start": {"test_start": {"protocol": "TCP"}},
    "end": {"sum_received": {"bits_per_second": 12e9, "seconds": 10}},
}
RDMA_REPORT = """\
 Device         : mlx5_0
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 1048576    111000           0.00               96.95                0.011558
---------------------------------------------------------------------------------------
"""


class FabricBudgetTests(unittest.TestCase):
    def test_a_retained_sample_is_reused_only_for_its_link_and_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "link.json").write_text(json.dumps(LINK))
            (root / "sample.json").write_text(json.dumps(TCP_SAMPLE))
            (root / "budget.json").write_text(json.dumps({"required_gbps": 10}))
            evidence = record_edge(
                root / "link.json",
                root / "budget.json",
                root / "sample.json",
                "engine-1",
                root / "engine-1.evidence.json",
            )
            self.assertEqual((evidence["source_role"], evidence["passed"]), ("engine-1", True))
            self.assertEqual(evidence["schema_version"], 2)
            self.assertEqual((root / "engine-1.evidence.json").stat().st_mode & 0o777, 0o600)
            (root / "revised.json").write_text(json.dumps({"required_gbps": 13}))
            comparison = reuse_edge(
                root / "link.json",
                root / "engine-1.evidence.json",
                root / "sample.json",
                root / "revised.json",
                "engine-3",
                root / "engine-3.comparison.json",
            )
            self.assertEqual(comparison["required_gbps"], 13)
            self.assertEqual((comparison["source_role"], comparison["passed"]), ("engine-3", False))
            self.assertEqual(
                comparison["source_evidence_sha256"],
                hashlib.sha256((root / "engine-1.evidence.json").read_bytes()).hexdigest(),
            )
            (root / "link.json").write_text(json.dumps({**LINK, "tool_version": "iperf 3.17"}))
            with self.assertRaisesRegex(ValueError, "differs from the evidence"):
                reuse_edge(
                    root / "link.json",
                    root / "engine-1.evidence.json",
                    root / "sample.json",
                    root / "revised.json",
                    "engine-1",
                    root / "changed.json",
                )
            self.assertFalse((root / "changed.json").exists())
            with self.assertRaises(FileExistsError):
                record_edge(
                    root / "link.json",
                    root / "budget.json",
                    root / "sample.json",
                    "engine-1",
                    root / "engine-1.evidence.json",
                )

    def test_link_records_need_two_hosts_and_a_known_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "sample.json").write_text(json.dumps(TCP_SAMPLE))
            (root / "budget.json").write_text(json.dumps({"required_gbps": 1}))
            for change, message in (
                ({"destination_host": "node-1"}, "two distinct hosts"),
                ({"transport": "ucx_shm"}, "ucx_tcp or ucx_rdma"),
                ({"test_parameters": {}}, "requires test parameters"),
            ):
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                    (root / "link.json").write_text(json.dumps({**LINK, **change}))
                    record_edge(
                        root / "link.json",
                        root / "budget.json",
                        root / "sample.json",
                        "engine-1",
                        root / "evidence.json",
                    )

    def test_padding_burst_and_decimal_gigabits(self):
        result = workload_budget(256000, Workload(129, 1, 2, 0.5, 1.25))
        self.assertAlmostEqual(result["required_gbps"], 0.01024)
        faster = workload_budget(256000, Workload(129, 8, 2, 0.5, 1.25))
        self.assertGreater(faster["required_gbps"], result["required_gbps"])

    def test_invalid_workload_cannot_produce_a_passing_zero_budget(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Workload(1024, value, 1, 1, 1.25)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            Workload(1024, 1, 1, 1, 0.9)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            Workload(1024, 1, 0, 1, 1.25)

    def test_tcp_rate_uses_received_bytes_and_rejects_iperf_errors(self):
        sample = {
            "start": {"test_start": {"protocol": "TCP"}},
            "end": {
                "sum_received": {"bits_per_second": 2e9, "seconds": 10},
                "sum_sent": {"bits_per_second": 3e9},
            },
        }
        self.assertEqual(received_gbps(sample), 2)
        with self.assertRaisesRegex(ValueError, "ran over UDP, not TCP"):
            received_gbps({**sample, "start": {"test_start": {"protocol": "UDP"}}})
        sample["error"] = "synthetic connection error"
        with self.assertRaisesRegex(ValueError, "iperf3 reported an error"):
            received_gbps(sample)

    def test_rdma_rate_comes_from_the_average_column_of_the_report(self):
        self.assertEqual(rdma_gbps(RDMA_REPORT, 1048576), 96.95)
        with self.assertRaisesRegex(ValueError, "another message size"):
            rdma_gbps(RDMA_REPORT, 65536)
        with self.assertRaisesRegex(ValueError, "no single BW average"):
            rdma_gbps(" Device : mlx5_0\n", 1048576)
        with self.assertRaisesRegex(ValueError, "one result row"):
            rdma_gbps(RDMA_REPORT.replace(" 1048576    111000", " "), 1048576)

    def test_runtime_layout_budget_requires_one_layout_across_ranks(self):
        model, launch = b'{"num_hidden_layers": 2}', b'{"tensor_parallel_size": 2}'
        layers = [{"layer": 0, "page_bytes": 4096, "block_tokens": 16, "extra_blocks": 1}]
        layout = {
            "schema_version": 1,
            "sizing": "runtime_padded_page_upper_bound",
            "model_config_sha256": hashlib.sha256(model).hexdigest(),
            "launch_config_sha256": hashlib.sha256(launch).hexdigest(),
            "image": "sha256:" + "a" * 64,
            "plan_sha256": "b" * 64,
            "ranks": [{"rank": r, "layers": layers, "kv_cache_layout": "HND"} for r in range(2)],
        }
        result = budget(model, launch, WORKLOAD, runtime_layout=json.dumps(layout).encode())
        self.assertEqual(result["payload_bytes_per_handoff"], 2 * 65 * 4096)
        self.assertEqual(result["kv_cache_layout"], "HND")
        layout["ranks"][1]["kv_cache_layout"] = "NHD"
        with self.assertRaisesRegex(ValueError, "different KV cache layouts"):
            budget(model, launch, WORKLOAD, runtime_layout=json.dumps(layout).encode())
        for rank in layout["ranks"]:
            rank.pop("kv_cache_layout")
        self.assertIsNone(runtime_layout_name(layout))
        with self.assertRaisesRegex(ValueError, "differs from the model or launch record"):
            budget(b"{}", launch, WORKLOAD, runtime_layout=json.dumps(layout).encode())
        with self.assertRaisesRegex(ValueError, "one runtime layout or one transfer record"):
            budget(model, launch, WORKLOAD)

    def test_transfer_record_budget_checks_the_measurement(self):
        model, launch = b"{}", b'{"tensor_parallel_size": 1}'
        measurement = {
            "schema": TRANSFER_SCHEMA,
            "schema_version": 1,
            "series": "synthetic:kv_transfer_total_mb",
            "prompt_tokens": 1024,
            "payload_bytes_per_handoff": 5 * 1024 * 1024,
            "handoffs": 4,
            "metrics_sha256": {"before.prom": "a" * 64, "after.prom": "b" * 64},
        }
        for change, message in (
            ({"prompt_tokens": 512}, "measured another prompt length"),
            ({"schema_version": 2}, "unsupported KV transfer measurement record"),
            ({"metrics_sha256": {"before.prom": "a" * 64}}, "SHA-256 of both metric"),
            ({"payload_bytes_per_handoff": 0}, "positive integer"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                budget(
                    model, launch, WORKLOAD, transfer=json.dumps({**measurement, **change}).encode()
                )
        data = json.dumps(measurement).encode()
        result = budget(model, launch, WORKLOAD, transfer=data)
        self.assertEqual(result["layout"], "measured_transfer")
        self.assertEqual(result["measured_handoffs"], 4)
        self.assertEqual(result["transfer_metrics_sha256"], ["a" * 64, "b" * 64])
        self.assertEqual(result["transfer_record_sha256"], hashlib.sha256(data).hexdigest())
        self.assertAlmostEqual(result["required_gbps"], 8 * 5 * 1024 * 1024 * 1.25 / 1e9)


if __name__ == "__main__":
    unittest.main()
