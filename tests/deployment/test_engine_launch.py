"""Engine launch records bind the allocation, TP size and exposed devices."""

import copy
import unittest

from tests.deployment.fixtures import launch_document
from tools.deployment.engine_launch import selected_launch


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
        self.assertEqual(selected_launch(document, "engine-1", {})["transfer"], transfer)

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
