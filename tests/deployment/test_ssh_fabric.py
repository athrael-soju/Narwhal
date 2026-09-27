"""Check directed scheduling and evidence boundaries without fleet measurements."""

import hashlib
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from narwhal.deployment import ssh_fabric as fabric
from narwhal.deployment.management_records import OperationError, encode_record
from narwhal.deployment.ssh_settings import SSHRecipe
from tools.deployment import fabric_budget


class FabricTests(unittest.TestCase):
    def session(self):
        recipe = SSHRecipe.model_validate({"schema": "narwhal.ssh-recipe", "schema_version": 1})
        roles = ["engine-1", "engine-2", "engine-3"]
        session = SimpleNamespace(
            plan={},
            settings=SimpleNamespace(remote_root="/private"),
            recipe=recipe,
            state={
                "engines": {role: {} for role in roles},
                "router": {"fleet_path": "/private/fleet.json"},
            },
            execution={
                "launches": {
                    role: {"tensor_parallel_size": 1, "transfer": {"transport": "ucx_tcp"}}
                    for role in roles
                }
            },
            router_host="one",
            context=SimpleNamespace(deadline=time.monotonic() + 5),
            host_for=lambda role: {"engine-1": "one", "engine-2": "two", "engine-3": "alias"}[role],
            checkout=lambda _host: Path("/checkout"),
            role_environment=lambda role: {
                "NARWHAL_FABRIC_INTERFACE": "eth0",
                f"NARWHAL_NODE_{role.removeprefix('engine-')}_IP": f"192.0.2.{role[-1]}",
            },
            check_generations=Mock(),
            gate=Mock(return_value=({}, {"listening": True})),
            put=Mock(side_effect=lambda _host, name, _body: Path("/private") / name),
        )
        patcher = patch.object(
            fabric,
            "input_document",
            return_value={
                "one": {"host_id": "physical-one"},
                "two": {"host_id": "physical-two"},
                "alias": {"host_id": "physical-one"},
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_all_directed_hosts_use_largest_source_budget_and_cleanup_serially(self):
        session = self.session()
        live = []
        clients = []

        def command(host, name, _argv, **kwargs):
            if kwargs.get("background"):
                self.assertEqual(live, [])
                live.append(name)
            else:
                self.assertEqual(len(live), 1)
                clients.append(name)
            return {"host_id": host, "owner": {"launch_token": name}}

        def stop(_session, _effect):
            self.assertEqual(len(live), 1)
            live.clear()

        sample = {
            "start": {"test_start": {"protocol": "TCP"}},
            "end": {"sum_received": {"seconds": 10, "bits_per_second": 10_000_000_000}},
        }
        session.command = command
        info = {
            "rails": [],
            "route": [{"dev": "eth0"}],
            "version": "fixture",
            "executable": "/usr/bin/iperf3",
        }
        with (
            patch.object(fabric, "asset", return_value=fabric_budget),
            patch.object(
                fabric, "_budget", side_effect=lambda _s, role, _c: {"required_gbps": int(role[-1])}
            ),
            patch.object(fabric, "_observe", return_value=info),
            patch.object(fabric, "_output", return_value=json.dumps(sample).encode()),
            patch.object(fabric, "_stop", side_effect=stop),
        ):
            result = fabric.qualify(session)
        self.assertEqual(result["directed_host_pairs"], 2)
        self.assertEqual(
            [(row["source"], row["destination"]) for row in result["edges"]],
            [("engine-3", "engine-2"), ("engine-2", "engine-3")],
        )
        self.assertEqual(len(clients), 2)
        self.assertEqual(live, [])

    def test_failed_client_always_removes_owned_server(self):
        session = self.session()
        effect = {"host_id": "two", "owner": {"launch_token": "job"}}
        session.command = Mock(
            side_effect=[effect, OperationError("stage_timeout", "fixture timeout")]
        )
        info = {"rails": [], "route": [], "version": "fixture", "executable": "/usr/bin/iperf3"}
        with (
            patch.object(fabric, "asset", return_value=fabric_budget),
            patch.object(fabric, "_budget", return_value={"required_gbps": 1}),
            patch.object(fabric, "_observe", return_value=info),
            patch.object(fabric, "_stop") as stop,
            self.assertRaises(OperationError),
        ):
            fabric.qualify(session)
        stop.assert_called_once_with(session, effect)

    def test_sample_below_budget_stops_before_the_next_pair(self):
        session = self.session()
        session.command = Mock(return_value={"host_id": "two", "owner": {"launch_token": "job"}})
        sample = {
            "start": {"test_start": {"protocol": "TCP"}},
            "end": {"sum_received": {"seconds": 10, "bits_per_second": 1000}},
        }
        info = {"rails": [], "route": [], "version": "fixture", "executable": "/usr/bin/iperf3"}
        with (
            patch.object(fabric, "asset", return_value=fabric_budget),
            patch.object(fabric, "_budget", return_value={"required_gbps": 1}),
            patch.object(fabric, "_observe", return_value=info),
            patch.object(fabric, "_output", return_value=json.dumps(sample).encode()),
            patch.object(fabric, "_stop") as stop,
            self.assertRaises(OperationError) as raised,
        ):
            fabric.qualify(session)
        self.assertEqual(raised.exception.code, "fabric_budget_unmet")
        self.assertEqual(session.command.call_count, 2)
        stop.assert_called_once()
        evidence = [
            json.loads(call.args[2])
            for call in session.put.call_args_list
            if call.args[1].endswith(".evidence.json")
        ]
        self.assertEqual(len(evidence), 1)
        self.assertFalse(evidence[0]["passed"])

    def test_budget_is_bound_to_serving_cache_and_full_load_prompt(self):
        session = self.session()
        launch = session.execution["launches"]["engine-1"]
        layout = {
            "schema_version": 1,
            "sizing": "runtime_padded_page_upper_bound",
            "image": "sha256:fixture",
            "launch_config_sha256": hashlib.sha256(encode_record(launch)).hexdigest(),
            "model_config_sha256": "model",
            "plan_sha256": "plan",
            "ranks": [
                {
                    "rank": 0,
                    "layers": [
                        {"layer": "one", "page_bytes": 4096, "block_tokens": 16, "extra_blocks": 1}
                    ],
                }
            ],
        }
        raw = encode_record(layout)
        session.state["engines"]["engine-1"] = {
            "run": "/private/operations/run/engine-1",
            "host_id": "one",
            "cache_sha256": hashlib.sha256(raw).hexdigest(),
            "plan_sha256": "plan",
        }
        session.files = SimpleNamespace(read=Mock(return_value=raw))
        session.role_environment = lambda _role: {"NARWHAL_MODEL_CONFIG_SHA256": "model"}
        result = fabric._budget(session, "engine-1", fabric_budget)
        self.assertEqual(result["payload_bytes_per_handoff"], 513 * 4096)
        session.files.read.return_value = encode_record({**layout, "plan_sha256": "changed"})
        with self.assertRaises(OperationError) as raised:
            fabric._budget(session, "engine-1", fabric_budget)
        self.assertEqual(raised.exception.code, "stale_plan")

    def test_route_must_use_declared_address_and_interface(self):
        request = {
            "address": "192.0.2.1",
            "peer": "192.0.2.2",
            "interface": "eth0",
            "transport": "ucx_tcp",
            "timeout_s": 1,
        }
        with (
            patch.object(fabric.ssh_worker.shutil, "which", return_value="/usr/bin/iperf3"),
            patch.object(
                fabric.ssh_worker,
                "_command",
                side_effect=[b'[{"dev":"eth0","from":"192.0.2.1"}]', b"iperf fixture"],
            ),
        ):
            self.assertEqual(fabric.observe(request)["version"], "iperf fixture")
        with (
            patch.object(
                fabric.ssh_worker, "_command", return_value=b'[{"dev":"other","from":"192.0.2.1"}]'
            ),
            self.assertRaises(ValueError),
        ):
            fabric.observe(request)

    def test_rdma_parser_requires_one_average_with_declared_units(self):
        raw = (
            b"#bytes #iterations BW peak[Gb/sec] BW average[Gb/sec] MsgRate[Mpps]\n"
            b"1048576 100 40.0 39.0 0.1\n"
        )
        self.assertEqual(fabric._rdma_gbps(raw), 39.0)
        for invalid in (raw.replace(b"Gb/sec", b"MB/sec"), raw.replace(b"39.0", b"NaN")):
            with self.assertRaises(OperationError):
                fabric._rdma_gbps(invalid)

    def test_cleanup_error_does_not_mark_empty_observation_absent(self):
        session = self.session()
        session.context.hard_deadline = time.monotonic() + 2
        session.context.record_effect = Mock()
        session.transport = SimpleNamespace(
            cancel_owned=Mock(
                return_value={
                    "state": "cancelled",
                    "observed_processes": {},
                    "containers": [],
                    "cleanup": {"error": "ownership_conflict"},
                }
            )
        )
        with self.assertRaises(OperationError):
            fabric._stop(
                session, {"host_id": "one", "owner": {"launch_token": "job"}, "effect": "unknown"}
            )
        session.context.record_effect.assert_not_called()
