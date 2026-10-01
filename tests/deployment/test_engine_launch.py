"""Engine launch records bind the allocation, TP size and exposed devices."""

import copy
import unittest

from tests.deployment.fixtures import cuda_engine, launch_document, runtime
from tools.deployment.engine_launch import expose_colocated_gpus, selected_launch


class EngineLaunchTests(unittest.TestCase):
    def test_selected_record_resolves_transport_and_generates_matching_tp_arguments(self):
        record = selected_launch(
            launch_document(), "engine-1", {"NARWHAL_FABRIC_INTERFACE": "fabric0"}
        )
        self.assertEqual(
            record["environment"], {"ROCR_VISIBLE_DEVICES": "0,1", "UCX_NET_DEVICES": "fabric0"}
        )
        self.assertEqual(record["vllm_args"], ["--tensor-parallel-size", "2"])
        self.assertEqual(record["transfer"]["devices"], [])
        self.assertEqual(record["transfer"]["gpu_tls"], "rocm")

    def test_gpu_transport_matches_runtime(self):
        document = launch_document()
        document["engines"]["engine-1"]["transfer"]["gpu_tls"] = "cuda_copy"
        with self.assertRaisesRegex(ValueError, "gpu_tls is incompatible"):
            selected_launch(document, "engine-1", {"NARWHAL_FABRIC_INTERFACE": "fabric0"})

    def test_allocation_rejects_duplicate_devices_and_tp_overallocation(self):
        for changes in (
            {"gpu_ids": ["0", "0"]},
            {"tensor_parallel_size": 3},
            {"accelerator": "<accelerator-model>"},
        ):
            document = launch_document()
            document["engines"]["engine-1"].update(changes)
            with self.assertRaises(ValueError):
                selected_launch(document, "engine-1", {"NARWHAL_FABRIC_INTERFACE": "fabric0"})

    def test_rdma_requires_its_character_device_mappings(self):
        document = launch_document()
        transfer = document["engines"]["engine-1"]["transfer"]
        transfer.update(transport="ucx_rdma", net_devices="mlx5_0:1")
        with self.assertRaisesRegex(ValueError, "RDMA device mappings"):
            selected_launch(document, "engine-1", {})
        transfer["devices"] = ["/dev/infiniband/rdma_cm", "/dev/infiniband/uverbs0"]
        self.assertEqual(
            selected_launch(document, "engine-1", {})["transfer"],
            {**transfer, "gpu_tls": "rocm"},
        )

    def test_management_variable_cannot_be_resolved_into_launch_record(self):
        document = launch_document()
        document["engines"]["engine-1"]["transfer"]["net_devices"] = (
            "${NARWHAL_NODE_1_SSH_PASSWORD}"
        )
        with self.assertRaisesRegex(ValueError, "FABRIC_INTERFACE"):
            selected_launch(
                document, "engine-1", {"NARWHAL_NODE_1_SSH_PASSWORD": "synthetic-secret"}
            )

    def test_each_engine_gets_an_independent_record(self):
        document = launch_document()
        document["engines"]["engine-2"] = copy.deepcopy(document["engines"]["engine-1"])
        one = selected_launch(document, "engine-1", {"NARWHAL_FABRIC_INTERFACE": "fabric0"})
        two = selected_launch(document, "engine-2", {"NARWHAL_FABRIC_INTERFACE": "fabric1"})
        self.assertEqual(one["transfer"]["net_devices"], "fabric0")
        self.assertEqual(two["transfer"]["net_devices"], "fabric1")
        self.assertEqual(
            document["engines"]["engine-1"]["transfer"]["net_devices"],
            "${NARWHAL_FABRIC_INTERFACE}",
        )

    def cuda_engines(self, count, **changes):
        document = launch_document()
        base = cuda_engine(document["engines"]["engine-1"])
        base.update(changes)
        env = {"NARWHAL_FABRIC_INTERFACE": "fabric0"}
        launches = {}
        for n in range(1, count + 1):
            entry = copy.deepcopy(base)
            entry["gpu_ids"] = [str(n - 1)]
            document["engines"][f"engine-{n}"] = entry
            launches[f"engine-{n}"] = selected_launch(document, f"engine-{n}", env)
        return launches

    def test_colocated_cuda_engines_see_peer_gpus_after_their_own(self):
        launches = self.cuda_engines(3)
        expose_colocated_gpus(launches, [["engine-1", "engine-2", "engine-3"]])
        visible = {role: r["environment"]["CUDA_VISIBLE_DEVICES"] for role, r in launches.items()}
        self.assertEqual(visible, {"engine-1": "0,1,2", "engine-2": "1,0,2", "engine-3": "2,0,1"})
        self.assertEqual(launches["engine-2"]["vllm_args"], ["--tensor-parallel-size", "1"])

    def test_shared_device_engine_keeps_its_gpu_out_of_peer_lists(self):
        document = launch_document()
        base = document["engines"]["engine-1"]
        for n in range(1, 4):
            document["engines"][f"engine-{n}"] = cuda_engine(copy.deepcopy(base), gpu=str(n - 1))
        shared = document["engines"]["engine-2"]
        shared["runtime"] = runtime()
        shared["runtime"]["extra_args"] += ["--gpu-memory-utilization", "0.4"]
        shared["shared_device"] = {
            "group": "node-1:GPU-test",
            "gpu_uuid": "GPU-test",
            "device_allowance": 0.5,
            "gpu_memory_utilization": 0.4,
        }
        env = {"NARWHAL_FABRIC_INTERFACE": "fabric0"}
        launches = {role: selected_launch(document, role, env) for role in document["engines"]}
        expose_colocated_gpus(launches, [["engine-1", "engine-2", "engine-3"]])
        visible = {role: r["environment"]["CUDA_VISIBLE_DEVICES"] for role, r in launches.items()}
        self.assertEqual(visible, {"engine-1": "0,2", "engine-2": "1", "engine-3": "2,0"})

    def test_engines_on_separate_hosts_keep_their_own_gpus(self):
        launches = self.cuda_engines(2)
        expose_colocated_gpus(launches, [["engine-1"], ["engine-2"]])
        self.assertEqual(launches["engine-1"]["environment"]["CUDA_VISIBLE_DEVICES"], "0")
        self.assertEqual(launches["engine-2"]["environment"]["CUDA_VISIBLE_DEVICES"], "1")

    def test_rocm_engines_keep_their_own_gpus(self):
        document = launch_document()
        document["engines"]["engine-2"] = copy.deepcopy(document["engines"]["engine-1"])
        document["engines"]["engine-2"]["gpu_ids"] = ["2", "3"]
        env = {"NARWHAL_FABRIC_INTERFACE": "fabric0"}
        launches = {role: selected_launch(document, role, env) for role in ("engine-1", "engine-2")}
        expose_colocated_gpus(launches, [["engine-1", "engine-2"]])
        self.assertEqual(launches["engine-1"]["environment"]["ROCR_VISIBLE_DEVICES"], "0,1")
        self.assertNotIn("CUDA_VISIBLE_DEVICES", launches["engine-1"]["environment"])
