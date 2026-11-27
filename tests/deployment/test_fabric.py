"""Qualify directed host edges against fabric budgets on synthetic engine hosts."""

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import fabric
from narwhal.deployment.hosts import RemoteError
from narwhal.dev.cli import main
from tests.deployment.fabric_hosts import REMOTE_DIR, Fleet, address


class FabricTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        infiniband = patch.object(fabric, "INFINIBAND", str(self.root / "sys"))
        infiniband.start()
        self.addCleanup(infiniband.stop)
        self.addCleanup(self.folder.cleanup)
        self.runs = 0

    def fleet(self, assignment, **options):
        return Fleet(self.root, assignment, **options)

    def load(self, fleet, groups, transfers=None, **changes):
        env = {**fleet.env, **changes}
        return fabric.load(
            fleet.hosts, fleet.manifest, fleet.run, env, fleet.groups_file(groups, transfers)
        )

    def measure(self, fleet, **changes):
        self.runs += 1
        out = self.root / f"measured-{self.runs}"
        out.mkdir()
        env = {**fleet.env, **changes}
        engines, peers, tokens = fabric.measure_handoffs(
            fleet.hosts, fleet.manifest, fleet.run, env
        )
        with contextlib.redirect_stdout(io.StringIO()):
            summary = fabric.measure(
                engines, peers, tokens, fleet.manifest["remote_dir"], fleet.ssh, out
            )
        return out, summary

    def qualify(self, fleet, groups, retained=None, **changes):
        self.runs += 1
        out = self.root / f"out-{self.runs}"
        out.mkdir()
        plan = self.load(fleet, groups, **changes)
        with contextlib.redirect_stdout(io.StringIO()):
            summary = fabric.qualify(plan, fleet.ssh, out, retained)
        return out, summary

    def clients(self, fleet):
        commands = self.root / "commands"
        lines = commands.read_text().splitlines() if commands.exists() else []
        return [line for line in lines if "--client" in line or line.endswith(address(1))]


class EdgeMatrixTests(FabricTestCase):
    def test_one_host_sizes_budgets_and_has_no_edges(self):
        fleet = self.fleet([[1, 2]])
        out, summary = self.qualify(fleet, [["engine-1", "engine-2"]])
        self.assertEqual(summary["edges"], [])
        self.assertEqual(list(summary["budgets"]), ["engine-1"])
        budget = json.loads((out / "fabric-budget/engine-1/budget.json").read_text())
        self.assertEqual(budget["cache_group"], ["engine-1", "engine-2"])
        self.assertEqual(budget["kv_cache_layout"], "NHD")
        self.assertFalse((out / "fabric-edge").exists())
        self.assertEqual(json.loads((out / "fabric.json").read_text()), summary)

    def test_two_hosts_measure_both_directed_edges(self):
        fleet = self.fleet([[1], [2]])
        out, summary = self.qualify(fleet, [["engine-1", "engine-2"]])
        self.assertEqual(
            [(edge["edge"], edge["sample"]) for edge in summary["edges"]],
            [
                ("node-1-to-node-2", "engine-1-to-engine-2"),
                ("node-2-to-node-1", "engine-2-to-engine-1"),
            ],
        )
        folder = out / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2"
        evidence = json.loads((folder / "engine-1.evidence.json").read_text())
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["measured_gbps"], 100)
        self.assertEqual(evidence["source_role"], "engine-1")
        link = json.loads((folder / "link.json").read_text())
        self.assertEqual(link["transport"], "ucx_tcp")
        self.assertEqual(link["test_parameters"]["parallel"], 2)
        self.assertEqual(
            evidence["link_sha256"],
            hashlib.sha256(
                json.dumps(link, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        self.assertEqual(len(self.clients(fleet)), 2)
        for host in ("node-1", "node-2"):
            deployment = fleet.ssh.home(host) / fleet.manifest["remote_dir"]
            self.assertEqual(list((deployment / "runs/fabric").iterdir()), [])
            self.assertEqual(list(fleet.ssh.home(host).glob("listening-*")), [])

    def test_three_hosts_sample_each_directed_pair_once_against_every_source_role(self):
        fleet = self.fleet([[1, 2], [3], [4]])
        groups = [["engine-1", "engine-3"], ["engine-2", "engine-4"]]
        out, summary = self.qualify(fleet, groups)
        edges = [edge["edge"] for edge in summary["edges"]]
        self.assertEqual(len(edges), 6)
        self.assertEqual(len(set(edges)), 6)
        self.assertEqual(len(self.clients(fleet)), 6)
        first = summary["edges"][0]
        self.assertEqual(first["source_roles"], ["engine-1", "engine-2"])
        self.assertEqual(sorted(first["roles"]), ["engine-1", "engine-2"])
        folder = out / "fabric-edge/node-1-to-node-2/engine-1-to-engine-3"
        for role in ("engine-1", "engine-2"):
            evidence = json.loads((folder / f"{role}.evidence.json").read_text())
            budget = out / "fabric-budget" / role / "budget.json"
            self.assertEqual(
                evidence["budget_sha256"], hashlib.sha256(budget.read_bytes()).hexdigest()
            )
        inbound = [edge for edge in summary["edges"] if edge["edge"] == "node-2-to-node-1"]
        self.assertEqual(inbound[0]["destination_roles"], ["engine-1", "engine-2"])
        self.assertEqual(list(inbound[0]["roles"]), ["engine-3"])

    def test_engines_with_their_own_hca_get_one_sample_per_hca_pair(self):
        fleet = self.fleet([[1, 2], [3]], rdma=True)
        _, summary = self.qualify(fleet, [["engine-1", "engine-2", "engine-3"]])
        samples = [(edge["edge"], edge["sample"]) for edge in summary["edges"]]
        self.assertEqual(
            samples,
            [
                ("node-1-to-node-2", "engine-1-to-engine-3"),
                ("node-1-to-node-2", "engine-2-to-engine-3"),
                ("node-2-to-node-1", "engine-3-to-engine-1"),
                ("node-2-to-node-1", "engine-3-to-engine-2"),
            ],
        )

    def test_rdma_samples_name_the_configured_hca_port_and_gid(self):
        fleet = self.fleet([[1], [2]], rdma=True)
        out, summary = self.qualify(fleet, [["engine-1", "engine-2"]])
        folder = out / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2"
        link = json.loads((folder / "link.json").read_text())
        parameters = link["test_parameters"]
        self.assertEqual(parameters["source_device"], "mlx5_1")
        self.assertEqual(parameters["destination_gid_index"], 3)
        self.assertEqual(parameters["roce_mode"], "v2")
        self.assertIn("ffff", parameters["source_gid"])
        source = json.loads((folder / "source.json").read_text())
        self.assertEqual(source["gid"]["ndev"], "fabric0")
        self.assertTrue((folder / "sample.txt").is_file())
        self.assertEqual(summary["edges"][0]["measured_gbps"], 100)
        commands = (self.root / "commands").read_text()
        self.assertIn("-d mlx5_1 -i 1 -x 3", commands)
        self.assertIn("--bind_source_ip 192.0.2.11 192.0.2.12", commands)

    def test_inputs_and_invalid_topologies_fail_before_any_host_is_reached(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        for changes, message in (
            ({"NARWHAL_FABRIC_HEADROOM": ""}, "Set NARWHAL_FABRIC_HEADROOM"),
            ({"NARWHAL_FABRIC_HEADROOM": "0.5"}, "headroom must be at least 1"),
            ({"NARWHAL_FABRIC_BURST": "1.5"}, "NARWHAL_FABRIC_BURST must be an integer"),
            ({"NARWHAL_FABRIC_TEST_PORT": "70000"}, "TCP port between 1 and 65535"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.load(fleet, groups, **changes)
        with self.assertRaisesRegex(ValueError, "groups engine-1, but the package holds"):
            self.load(fleet, [["engine-1"]])
        path = fleet.groups_file(groups, name="sourceless")
        document = json.loads(path.read_text())
        del document["engines"]["engine-1"]["geometry_source"]
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "engine-1: --groups .* names no geometry source"):
            fabric.load(fleet.hosts, fleet.manifest, fleet.run, fleet.env, path)
        self.assertEqual(fleet.ssh.calls, [])

    def test_groups_from_another_package_or_a_changed_layout_are_refused(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        path = fleet.groups_file(groups)
        layout = path.parent / "engine-1-layout.json"
        layout.write_text(layout.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "engine-1: .* changed after the groups were"):
            fabric.load(fleet.hosts, fleet.manifest, fleet.run, fleet.env, path)
        path = fleet.groups_file(groups, name="stale")
        document = json.loads(path.read_text())
        document["engines"]["engine-2"]["launch_sha256"] = "0" * 64
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "engine-2: --groups .* another launch record"):
            fabric.load(fleet.hosts, fleet.manifest, fleet.run, fleet.env, path)
        self.assertEqual(fleet.ssh.calls, [])

    def test_rdma_inputs_select_one_hca_port_with_gid_index_and_roce_mode(self):
        fleet = self.fleet([[1], [2]], rdma=True)
        groups = [["engine-1", "engine-2"]]
        with self.assertRaisesRegex(ValueError, "NODE_1_RDMA_GID_INDEX"):
            self.load(fleet, groups, NARWHAL_RDMA_GID_INDEX="")
        with self.assertRaisesRegex(ValueError, "v2, v1, infiniband"):
            self.load(fleet, groups, NARWHAL_NODE_1_RDMA_ROCE_MODE="v3")
        plan = self.load(fleet, groups, NARWHAL_NODE_2_RDMA_GID_INDEX="5")
        self.assertEqual(plan.engines["engine-2"].endpoint.rdma.gid_index, 5)
        self.assertEqual(plan.engines["engine-1"].endpoint.rdma.gid_index, 3)
        with self.assertRaisesRegex(ValueError, "one HCA port per ucx_rdma engine"):
            fabric.rdma_input(fleet.env, "engine-1", "mlx5_0:1,mlx5_1:1")

    def test_tcp_engines_send_kv_through_the_fabric_interface(self):
        fleet = self.fleet([[1], [2]])
        path = fleet.run / "node-2/engine-launch.engine-2.json"
        launch = json.loads(path.read_text())
        launch["transfer"]["net_devices"] = "eth7"
        path.write_text(json.dumps(launch))
        with self.assertRaisesRegex(ValueError, "transfer.net_devices eth7"):
            self.load(fleet, [["engine-1", "engine-2"]])

    def test_roles_on_different_transports_stop_naming_the_edge(self):
        fleet = self.fleet([[1], [2]], rdma=True)
        path = fleet.run / "node-2/engine-launch.engine-2.json"
        launch = json.loads(path.read_text())
        launch["transfer"].update(transport="ucx_tcp", net_devices="fabric0")
        path.write_text(json.dumps(launch))
        with self.assertRaisesRegex(
            ValueError, "node-1-to-node-2: engine-1 sends KV over ucx_rdma and engine-2 over"
        ):
            self.load(fleet, [["engine-1", "engine-2"]])

    def test_tcp_roles_of_different_tp_sizes_on_one_host_stop_before_any_host(self):
        fleet = self.fleet([[1, 2], [3]])
        for folder in (fleet.run / "node-1", fleet.ssh.home("node-1") / REMOTE_DIR / "config"):
            path = folder / "engine-launch.engine-2.json"
            launch = json.loads(path.read_text())
            launch["tensor_parallel_size"] = 1
            path.write_text(json.dumps(launch))
        with self.assertRaisesRegex(
            ValueError,
            r"^node-1-to-node-2: engine-1, engine-2 share one TCP fabric endpoint on node-1 but "
            r"differ in tensor parallel size \(1, 2\)",
        ):
            self.load(fleet, [["engine-1", "engine-3"], ["engine-2"]])
        self.assertEqual(fleet.ssh.calls, [])


class ReuseTests(FabricTestCase):
    def test_an_unchanged_link_reuses_the_retained_sample_against_the_current_budget(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        first, _ = self.qualify(fleet, groups)
        clients = len(self.clients(fleet))
        second, summary = self.qualify(
            fleet, groups, retained=first, NARWHAL_FABRIC_HANDOFFS_PER_S="4"
        )
        self.assertEqual(len(self.clients(fleet)), clients)
        self.assertTrue(all(edge["reused"] for edge in summary["edges"]))
        folder = second / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2"
        comparison = json.loads((folder / "engine-1.comparison.json").read_text())
        budget = json.loads((second / "fabric-budget/engine-1/budget.json").read_text())
        self.assertEqual(comparison["required_gbps"], budget["required_gbps"])
        original = first / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2/sample.json"
        self.assertEqual((folder / "retained/sample.json").read_bytes(), original.read_bytes())
        _, summary = self.qualify(fleet, groups, retained=second)
        self.assertTrue(all(edge["reused"] for edge in summary["edges"]))
        self.assertEqual(len(self.clients(fleet)), clients)

    def test_a_sample_below_its_budget_is_measured_again_once_the_fabric_is_fixed(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        (fleet.ssh.home("node-2") / "rate").write_text("0.000001")
        failed = self.root / "out-failed"
        failed.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(fabric.BelowBudget):
            fabric.qualify(self.load(fleet, groups), fleet.ssh, failed)
        # The operator fixes the fabric; nothing in the link record changes.
        (fleet.ssh.home("node-2") / "rate").unlink()
        clients = len(self.clients(fleet))
        _, summary = self.qualify(fleet, groups, retained=failed)
        by_edge = {edge["edge"]: edge for edge in summary["edges"]}
        self.assertFalse(by_edge["node-2-to-node-1"]["reused"])
        self.assertTrue(
            all(verdict["passed"] for verdict in by_edge["node-2-to-node-1"]["roles"].values())
        )
        self.assertGreater(len(self.clients(fleet)), clients)

    def test_a_retained_sample_below_the_current_budget_is_measured_again(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        for host in ("node-1", "node-2"):
            (fleet.ssh.home(host) / "rate").write_text("0.01")
        first, _ = self.qualify(fleet, groups)
        for host in ("node-1", "node-2"):
            (fleet.ssh.home(host) / "rate").unlink()
        clients = len(self.clients(fleet))
        second, summary = self.qualify(
            fleet, groups, retained=first, NARWHAL_FABRIC_HANDOFFS_PER_S="10"
        )
        self.assertEqual([edge["reused"] for edge in summary["edges"]], [False, False])
        self.assertEqual(len(self.clients(fleet)), clients + 2)
        folder = second / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2"
        self.assertFalse((folder / "engine-1.comparison.json").exists())
        self.assertFalse((folder / "retained").exists())
        self.assertTrue(json.loads((folder / "engine-1.evidence.json").read_text())["passed"])

    def test_only_the_newest_sample_of_a_retained_attempt_counts(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]

        def rate(value):
            for host in ("node-1", "node-2"):
                (fleet.ssh.home(host) / "rate").write_text(value)

        rate("0.01")
        first, _ = self.qualify(fleet, groups)
        # A higher budget rejects the retained rate, and the new measurement falls short.
        rate("0.001")
        second = self.root / "second"
        second.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(fabric.BelowBudget):
            plan = self.load(fleet, groups, NARWHAL_FABRIC_HANDOFFS_PER_S="10")
            fabric.qualify(plan, fleet.ssh, second, first)
        # The budget is back where the old rate passed, but the newest sample fell short.
        clients = len(self.clients(fleet))
        rate("0.01")
        _, summary = self.qualify(fleet, groups, retained=second)
        self.assertEqual([edge["reused"] for edge in summary["edges"]], [False, False])
        self.assertEqual(len(self.clients(fleet)), clients + 2)
        # The rejected copy is gone, so the attempt holds only the sample it measured.
        folder = second / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2"
        self.assertEqual(
            sorted(path.name for path in folder.iterdir()),
            [
                "destination.json",
                "engine-1.evidence.json",
                "link.json",
                "sample.json",
                "source.json",
            ],
        )

    def test_a_changed_link_or_a_tampered_sample_is_measured_again(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        first, _ = self.qualify(fleet, groups)
        (fleet.ssh.home("node-2") / "iperf3-version").write_text("iperf 3.17\n")
        _, summary = self.qualify(fleet, groups, retained=first)
        self.assertEqual([edge["reused"] for edge in summary["edges"]], [False, False])
        second, _ = self.qualify(fleet, groups)
        sample = second / "fabric-edge/node-1-to-node-2/engine-1-to-engine-2/sample.json"
        sample.write_text(sample.read_text().replace("100000000000.0", "900000000000.0"))
        _, summary = self.qualify(fleet, groups, retained=second)
        self.assertEqual([edge["reused"] for edge in summary["edges"]], [False, True])

    def test_a_changed_route_field_but_not_its_cache_timer_changes_the_link(self):
        fleet = self.fleet([[1], [2]])
        groups = [["engine-1", "engine-2"]]
        first, _ = self.qualify(fleet, groups)
        _, summary = self.qualify(fleet, groups, retained=first)
        self.assertTrue(summary["edges"][0]["reused"])
        route = [{"dst": address(2), "from": address(1), "gateway": "192.0.2.1", "dev": "fabric0"}]
        (fleet.ssh.home("node-1") / "routes" / f"{address(2)}.json").write_text(json.dumps(route))
        _, summary = self.qualify(fleet, groups, retained=first)
        self.assertFalse(summary["edges"][0]["reused"])


class FailureTests(FabricTestCase):
    groups = (("engine-1", "engine-2"),)

    def fails(self, fleet, error, message, **changes):
        with self.assertRaisesRegex(error, message) as caught:
            self.qualify(fleet, self.groups, **changes)
        return caught.exception

    def test_a_missing_tool_names_the_edge_and_its_package(self):
        for tool, package, rdma in (
            ("iperf3", "iperf3", False),
            ("ib_write_bw", "perftest", True),
            ("ip", "iproute2", False),
        ):
            with self.subTest(tool=tool):
                self.setUp()
                fleet = self.fleet([[1], [2]], rdma=rdma)
                fleet.ssh.remove_tool("node-2", tool)
                self.fails(
                    fleet,
                    RemoteError,
                    rf"node-2: blocked at node-1-to-node-2 engine-1-to-engine-2 tools and "
                    rf"route on node-2: the host lacks {tool}; install the {package} package",
                )

    def test_an_occupied_test_port_stops_before_any_server_starts(self):
        fleet = self.fleet([[1], [2]])
        (fleet.ssh.home("node-2") / "busy-5201").touch()
        self.fails(
            fleet,
            RemoteError,
            "node-2: blocked at node-1-to-node-2 engine-1-to-engine-2 test port on node-2: "
            "test port 5201 is in use",
        )
        self.assertEqual(self.clients(fleet), [])

    def test_a_route_on_the_wrong_interface_names_the_host_and_both_interfaces(self):
        fleet = self.fleet([[1], [2]])
        (fleet.ssh.home("node-2") / "dev").write_text("eth9")
        error = self.fails(
            fleet,
            fabric.FabricFailure,
            r"^node-1-to-node-2 engine-1-to-engine-2: the route from 192.0.2.12 to 192.0.2.11 "
            "on node-2 leaves through eth9, not NARWHAL_FABRIC_INTERFACE fabric0$",
        )
        self.assertEqual(error.stage, "fabric-edge")
        self.assertEqual(error.context["host"], "node-2")

    def test_a_missing_interface_or_foreign_source_address_stops(self):
        fleet = self.fleet([[1], [2]])
        (fleet.ssh.home("node-1") / "interfaces/fabric0.json").unlink()
        self.fails(fleet, RemoteError, "the host has no interface fabric0")
        self.setUp()
        fleet = self.fleet([[1], [2]])
        (fleet.ssh.home("node-1") / "interfaces/fabric0.json").write_text(
            json.dumps([{"ifname": "fabric0", "addr_info": [{"local": "192.0.2.99"}]}])
        )
        self.fails(fleet, fabric.FabricFailure, "192.0.2.11 is not an address of fabric0")

    def test_a_sample_over_another_transport_or_between_other_addresses_stops(self):
        cases = (
            (False, "protocol", "UDP", "sample ran over UDP, not TCP"),
            (False, "client-address", "192.0.2.77", "streams did not run from 192.0.2.11"),
            (True, "link-type", "IB", "names Link type IB, not Ethernet"),
            (True, "report-device", "mlx5_9", "names Device mlx5_9, not mlx5_1"),
        )
        for rdma, name, value, message in cases:
            with self.subTest(name=name):
                self.setUp()
                fleet = self.fleet([[1], [2]], rdma=rdma)
                (fleet.ssh.home("node-1") / name).write_text(value)
                self.fails(fleet, fabric.FabricFailure, message)

    def test_gid_tables_that_contradict_the_rdma_inputs_stop_naming_the_cause(self):
        cases = (
            ({"ndev": "eth3"}, "belongs to eth3, not NARWHAL_FABRIC_INTERFACE fabric0"),
            ({"kind": "IB/RoCE v1"}, "has type IB/RoCE v1, and RoCE mode v2 needs RoCE v2"),
            ({"gid": "::ffff:192.0.2.99"}, "not the fabric address 192.0.2.12"),
            ({"state": "1: DOWN"}, "mlx5_2:1 is 1: DOWN, not ACTIVE"),
            ({"link": "InfiniBand"}, "link layer InfiniBand, and RoCE mode v2 needs Ethernet"),
        )
        for change, message in cases:
            with self.subTest(change=change):
                self.setUp()
                fleet = self.fleet([[1], [2]], rdma=True)
                values = {"ndev": "fabric0", "gid": f"::ffff:{address(2)}", **change}
                fleet.gid(2, **values)
                self.fails(fleet, fabric.FabricFailure, f"node-2: .*{message}")
        self.setUp()
        fleet = self.fleet([[1], [2]], rdma=True)
        self.fails(
            fleet,
            RemoteError,
            "the GID table of mlx5_1:1 has no index 4",
            NARWHAL_NODE_1_RDMA_GID_INDEX="4",
        )

    def test_native_infiniband_needs_its_link_layer_and_a_set_gid(self):
        fleet = self.fleet([[1], [2]], rdma=True)
        for node in (1, 2):
            fleet.gid(node, ndev="", gid="fe80::1", kind="IB/RoCE v1", link="InfiniBand")
            (fleet.ssh.home(f"node-{node}") / "link-type").write_text("IB")
        _, summary = self.qualify(fleet, self.groups, NARWHAL_RDMA_ROCE_MODE="infiniband")
        self.assertEqual(len(summary["edges"]), 2)
        fleet.gid(2, ndev="", gid="::", kind="IB/RoCE v1", link="InfiniBand")
        self.fails(
            fleet,
            fabric.FabricFailure,
            "GID 3 of mlx5_2:1 is unset",
            NARWHAL_RDMA_ROCE_MODE="infiniband",
        )

    def test_an_edge_below_its_budget_keeps_its_evidence_and_names_the_role(self):
        fleet = self.fleet([[1], [2]])
        (fleet.ssh.home("node-2") / "rate").write_text("0.000001")
        out = self.root / "out-below"
        out.mkdir()
        plan = self.load(fleet, self.groups)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                fabric.BelowBudget,
                r"^node-2-to-node-1 engine-2-to-engine-1: measured 0.000 Gbit/s, below the "
                r"\S+ Gbit/s budget of engine-2; see deploy/04-Qualify-Fabric.md#troubleshooting",
            ),
        ):
            fabric.qualify(plan, fleet.ssh, out)
        folder = out / "fabric-edge/node-2-to-node-1/engine-2-to-engine-1"
        self.assertFalse(json.loads((folder / "engine-2.evidence.json").read_text())["passed"])
        summary = json.loads((out / "fabric.json").read_text())
        self.assertEqual(
            [edge["edge"] for edge in summary["edges"]], ["node-1-to-node-2", "node-2-to-node-1"]
        )
        self.assertEqual(list(fleet.ssh.home("node-1").glob("listening-*")), [])

    def test_a_server_that_never_listens_is_stopped_and_its_files_removed(self):
        fleet = self.fleet([[1], [2]])
        home = fleet.ssh.home("node-2")
        (home / "silent-server").touch()
        with patch("narwhal.deployment.fabric.LISTEN_TENTHS", 5):
            self.fails(fleet, RemoteError, "the iperf3 server did not listen on port 5201")
        pid = int((home / "server-pid").read_text())
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(pid), "the test server outlived its failed start")
        operations = [call[2] for call in fleet.ssh.calls]
        self.assertEqual(operations[-1], "node-1-to-node-2 engine-1-to-engine-2 iperf3 server stop")
        deployment = home / fleet.manifest["remote_dir"]
        self.assertEqual(list((deployment / "runs/fabric").iterdir()), [])

    def test_a_failed_client_stops_the_server_it_started(self):
        fleet = self.fleet([[1], [2]])
        fleet.ssh.remove_tool("node-1", "iperf3")
        client = fleet.ssh.home("node-1") / "bin" / "iperf3"
        client.write_text(
            "#!/bin/sh\ncase \"$*\" in --version) echo 'iperf 3.16';; *) exit 1;; esac\n"
        )
        client.chmod(0o755)
        self.fails(fleet, RemoteError, "the iperf3 client exited with status 1")
        operations = [call[2] for call in fleet.ssh.calls]
        self.assertEqual(operations[-1], "node-1-to-node-2 engine-1-to-engine-2 iperf3 server stop")
        self.assertEqual(list(fleet.ssh.home("node-2").glob("listening-*")), [])
        deployment = fleet.ssh.home("node-2") / fleet.manifest["remote_dir"]
        self.assertEqual(list((deployment / "runs/fabric").iterdir()), [])


class CommandTests(FabricTestCase):
    def invoke(self, fleet, *arguments, action="fabric", **changes):
        with (
            patch.dict(os.environ, {**fleet.env, **changes}),
            patch("narwhal.deployment.cli.load_hosts", return_value=fleet.hosts),
            patch("narwhal.deployment.install.load_run", return_value=fleet.manifest),
            patch("narwhal.deployment.cli.SSH", return_value=fleet.ssh),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(["deploy", action, "--run", str(fleet.run), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_the_command_writes_its_evidence_and_reports_each_outcome(self):
        fleet = self.fleet([[1], [2]])
        group = ["--groups", str(fleet.groups_file([["engine-1", "engine-2"]]))]
        first = self.root / "first"
        code, stdout, _ = self.invoke(fleet, *group, "--out", str(first))
        self.assertEqual(code, 0, stdout)
        self.assertIn("Qualified 2 directed samples", stdout)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
        code, stdout, _ = self.invoke(
            fleet, *group, "--out", str(self.root / "second"), "--retained", str(first),
            "--format", "json",
        )  # fmt: skip
        result = json.loads(stdout)
        self.assertEqual((code, result["status"]), (0, "success"))
        self.assertTrue(all(edge["reused"] for edge in result["data"]["edges"]))
        (fleet.ssh.home("node-2") / "rate").write_text("0.000001")
        code, stdout, stderr = self.invoke(fleet, *group, "--out", str(self.root / "third"))
        self.assertEqual(code, 1)
        self.assertIn("narwhal: deploy fabric: node-2-to-node-1 engine-2-to-engine-1", stderr)
        (fleet.ssh.home("node-2") / "dev").write_text("eth9")
        code, stdout, _ = self.invoke(
            fleet, *group, "--out", str(self.root / "fourth"), "--format", "json"
        )
        result = json.loads(stdout)
        self.assertEqual((code, result["errors"][0]["stage"]), (4, "fabric-edge"))
        self.assertEqual(result["errors"][0]["context"]["host"], "node-2")
        code, _, stderr = self.invoke(fleet, *group, "--out", str(first))
        self.assertEqual(code, 2)
        code, _, stderr = self.invoke(
            fleet, "--groups", str(self.root / "missing.json"), "--out", str(self.root / "fifth")
        )
        self.assertEqual(code, 2)
        self.assertIn("--groups", stderr)
        self.assertFalse((self.root / "fifth").exists())

    def test_transfer_measure_writes_each_engines_record_for_the_fabric_budget(self):
        fleet = self.fleet([[1], [2]], backend="sglang", tp=1)
        measured = self.root / "measured"
        code, stdout, _ = self.invoke(
            fleet, "--out", str(measured), "--format", "json", action="transfer-measure"
        )
        result = json.loads(stdout)
        self.assertEqual((code, result["status"]), (0, "success"))
        self.assertEqual(result["data"]["engines"]["engine-2"]["peer"], "engine-1")
        # Each record names the packaged launch record that cache-groups checks it against.
        for role in ("engine-1", "engine-2"):
            launch = next(fleet.run.glob(f"*/engine-launch.{role}.json"))
            record = json.loads((measured / role / "transfer.json").read_text())
            self.assertEqual(
                record["launch_config_sha256"], hashlib.sha256(launch.read_bytes()).hexdigest()
            )
        groups = fleet.groups_file(
            [["engine-1", "engine-2"]], {"engine-1": measured / "engine-1/transfer.json"}
        )
        code, stdout, _ = self.invoke(
            fleet, "--groups", str(groups), "--out", str(self.root / "fabric")
        )
        self.assertEqual(code, 0, stdout)
        code, _, _ = self.invoke(fleet, "--out", str(measured), action="transfer-measure")
        self.assertEqual(code, 2)


class TransferMeasurementTests(FabricTestCase):
    def relaunch(self, fleet, role, **runtime):
        host = fleet.host_of(role).name
        path = fleet.run / host / f"engine-launch.{role}.json"
        launch = json.loads(path.read_text())
        launch["runtime"].update(runtime)
        path.write_text(json.dumps(launch))

    def test_each_engine_measures_handoffs_to_the_next_engine_with_its_launch_inputs(self):
        fleet = self.fleet([[1, 2], [3], [4]], backend="sglang", tp=1)
        self.relaunch(fleet, "engine-4", mem_fraction_static=0.5)
        self.relaunch(fleet, "engine-2", mem_fraction_static=0.5)
        out, summary = self.measure(fleet)
        peers = {role: record["peer"] for role, record in summary["engines"].items()}
        self.assertEqual(
            peers,
            {"engine-1": "engine-3", "engine-2": "engine-4", "engine-3": "engine-1",
             "engine-4": "engine-2"},
        )  # fmt: skip
        calls = (fleet.ssh.home("node-1") / "transfer-calls").read_text().splitlines()
        first = json.loads(calls[0])
        self.assertEqual(first["decode"], f"http://{address(2)}:8000")
        self.assertEqual(first["bootstrap_host"], address(1))
        self.assertEqual(first["bootstrap_port"], "5600")
        self.assertEqual(first["prompt_tokens"], "1024")
        record = json.loads((out / "transfer-measure.json").read_text())
        self.assertEqual(record["engines"]["engine-3"]["payload_bytes_per_handoff"], 4194304)
        for role in peers:
            folder = out / role
            self.assertEqual(sorted(path.name for path in folder.iterdir()),
                             ["after.prom", "before.prom", "transfer.json"])  # fmt: skip
            self.assertEqual(stat.S_IMODE((folder / "transfer.json").stat().st_mode), 0o600)

    def test_an_engine_without_a_peer_of_its_launch_inputs_stops_with_that_cause(self):
        fleet = self.fleet([[1], [2]], backend="sglang", tp=1)
        self.relaunch(fleet, "engine-2", mem_fraction_static=0.5)
        with self.assertRaisesRegex(
            ValueError,
            "engine-1: its backend sizes the fabric budget from handoffs to another idle "
            "engine with the same image, model configuration, accelerator, TP size, GPU "
            "visibility, runtime and transport, and no other engine matches it",
        ):
            self.measure(fleet)
        with self.assertRaisesRegex(ValueError, "Set NARWHAL_FABRIC_PROMPT_TOKENS"):
            self.measure(fleet, NARWHAL_FABRIC_PROMPT_TOKENS="")
        self.assertEqual(fleet.ssh.calls, [])
        self.setUp()
        with self.assertRaisesRegex(ValueError, "measures its handoffs; its backend captures"):
            self.measure(self.fleet([[1], [2]]))

    def test_the_budget_is_sized_from_the_representatives_transfer_record(self):
        fleet = self.fleet([[1], [2]], backend="sglang", tp=1)
        measured, _ = self.measure(fleet)
        transfers = {"engine-1": measured / "engine-1/transfer.json"}
        groups = [["engine-1", "engine-2"]]
        self.runs += 1
        out = self.root / "qualified"
        out.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            summary = fabric.qualify(self.load(fleet, groups, transfers), fleet.ssh, out)
        budget = json.loads((out / "fabric-budget/engine-1/budget.json").read_text())
        self.assertEqual(budget["layout"], "measured_transfer")
        transfer = (measured / "engine-1/transfer.json").read_bytes()
        self.assertEqual(budget["transfer_record_sha256"], hashlib.sha256(transfer).hexdigest())
        for name in ("transfer.json", "before.prom", "after.prom"):
            self.assertEqual(
                (out / "fabric-budget/engine-1" / name).read_bytes(),
                (measured / "engine-1" / name).read_bytes(),
            )
        self.assertFalse((out / "fabric-budget/engine-1/cache-layout.json").exists())
        self.assertEqual(len(summary["edges"]), 2)
        # A group of one is sized from the measurement its engine made to a peer of its inputs.
        plan = self.load(fleet, [["engine-1"], ["engine-2"]], {
            "engine-1": transfers["engine-1"], "engine-2": measured / "engine-2/transfer.json",
        })  # fmt: skip
        self.assertEqual([group.roles for group in plan.groups], [("engine-1",), ("engine-2",)])

    def test_a_missing_foreign_or_altered_transfer_record_stops_before_any_host(self):
        fleet = self.fleet([[1], [2]], backend="sglang", tp=1)
        measured, _ = self.measure(fleet)
        calls = len(fleet.ssh.calls)
        groups = [["engine-1", "engine-2"]]
        record = measured / "engine-1/transfer.json"
        with self.assertRaisesRegex(ValueError, "engine-1: --groups .* names no geometry source"):
            self.load(fleet, groups)
        with self.assertRaisesRegex(ValueError, "measured another prompt length"):
            self.load(fleet, groups, {"engine-1": record}, NARWHAL_FABRIC_PROMPT_TOKENS="2048")
        (measured / "engine-1/before.prom").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "before.prom beside .* is not the one it records"):
            self.load(fleet, groups, {"engine-1": record})
        self.assertEqual(len(fleet.ssh.calls), calls)

    def test_a_changed_tool_or_launch_record_on_the_host_stops(self):
        fleet = self.fleet([[1], [2]], backend="sglang", tp=1)
        deployment = fleet.host_of("engine-1") / fleet.manifest["remote_dir"]
        (deployment / "runs/deployment-tools/transfer_measure.py").write_text("changed\n")
        with self.assertRaisesRegex(RemoteError, "differs from its packaged SHA-256"):
            self.measure(fleet)
        self.setUp()
        fleet = self.fleet([[1], [2]])
        deployment = fleet.host_of("engine-1") / fleet.manifest["remote_dir"]
        (deployment / "config/engine-launch.engine-1.json").write_text("{}")
        with self.assertRaisesRegex(fabric.FabricFailure, "differs from the package's"):
            self.qualify(fleet, [["engine-1", "engine-2"]])


if __name__ == "__main__":
    unittest.main()


def _alive(pid):
    """Whether `pid` runs and is not a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"
