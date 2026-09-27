"""Exercise SSH plan binding with private inputs and synthetic host observations."""

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from narwhal.deployment import ssh_prepare
from narwhal.deployment.management_access import InspectionAccess
from narwhal.deployment.management_adapters import installed_adapters
from narwhal.deployment.management_plans import PlanStore, canonical, digest
from narwhal.deployment.management_records import OperationError, utc_now
from narwhal.deployment.management_registry import ManagementRegistry
from tests.deployment.fixtures import launch_document, runtime
from tools.deployment import prepare_host_env


class SSHPrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = {
            "commit": "a" * 40,
            "distribution_version": "0.1.0",
            "wheel_sha256": None,
            "bundle_sha256": "b" * 64,
        }
        self.verified = {"source": self.source, "assets": {}, "assets_sha256": "c" * 64}
        self.fleet = {
            "schema": "narwhal.fleet",
            "schema_version": 1,
            "model": "synthetic-model",
            "hardware": {
                "accelerator": "synthetic-gpu",
                "accelerators_per_engine": 2,
                "tensor_parallel": 2,
            },
            "engine": {"engine_api_key_env": "FLEET_API_KEY"},
            "engines": [
                {
                    "iid": f"n{number}",
                    "url": "${NARWHAL_NODE_" + str(number) + "_URL}",
                    "attestation_url": "${NARWHAL_NODE_" + str(number) + "_ATTESTATION_URL}",
                    "role": "prefill" if number == 1 else "decode",
                }
                for number in (1, 2)
            ],
            "slo": {"ttft_s": 10.0, "tpot_s": 0.125},
            "controller": {"min_prefill": 1, "min_decode": 1},
            "profiles": {"path": "runs/profiles.json"},
        }
        self.hosts = {
            "hosts": [
                {"id": "one", "ssh_env": "HOST_ONE", "roles": ["engine-1"]},
                {"id": "two", "ssh_env": "HOST_TWO", "roles": ["engine-2"]},
                {"id": "router", "ssh_env": "HOST_ROUTER", "roles": ["router"]},
            ]
        }
        self.launch = launch_document()
        self.launch["engines"]["engine-1"]["runtime"] = runtime()
        self.launch["engines"]["engine-2"] = copy.deepcopy(self.launch["engines"]["engine-1"])
        self.recipe = {
            "schema": "narwhal.ssh-recipe",
            "schema_version": 1,
            "environment": {
                "NARWHAL_ENGINE_IMAGE": "sha256:" + "d" * 64,
                "NARWHAL_ENGINE_MODEL_NAME": "synthetic-model",
                "NARWHAL_MODEL_DIR": "/srv/model",
                "NARWHAL_RUN_DIR": "/srv/narwhal/runs",
                "NARWHAL_MODEL_CONFIG_SHA256": "e" * 64,
                "NARWHAL_FABRIC_INTERFACE": "eth0",
                "NARWHAL_ENGINE_PORT": "8000",
                "NARWHAL_ATTEST_PORT": "8010",
                "NARWHAL_NIXL_SIDE_CHANNEL_PORT": "5600",
                "NARWHAL_UCX_TCP_PORT_RANGE": "39000-39003",
                "NARWHAL_NODE_1_IP": "192.0.2.11",
                "NARWHAL_NODE_2_IP": "192.0.2.12",
                "NARWHAL_ROUTER_URL": "http://127.0.0.1:8000",
                "NARWHAL_GRAFANA_BIND_ADDRESS": "127.0.0.1",
                "NARWHAL_PROMETHEUS_LISTEN_ADDRESS": "127.0.0.1:9090",
            },
        }
        self.settings = {
            "schema": "narwhal.ssh-settings",
            "schema_version": 1,
            "source_root": str(self.root / "source"),
            "source_commit": self.source["commit"],
            "hosts_path": str(self.root / "hosts.json"),
            "launch_path": str(self.root / "launch.json"),
            "known_hosts_path": str(self.root / "known_hosts"),
            "remote_root": "/srv/narwhal",
        }
        for name, document in (
            ("fleet.json", self.fleet),
            ("hosts.json", self.hosts),
            ("launch.json", self.launch),
            ("recipe.json", self.recipe),
            ("settings.json", self.settings),
        ):
            self.write(name, document)
        self.write("known_hosts", "synthetic-host-key\n")
        (self.root / "artifacts").mkdir(mode=0o700)
        self.registry = ManagementRegistry.model_validate_json(
            json.dumps(
                {
                    "schema": "narwhal.management-registry",
                    "schema_version": 1,
                    "registry_id": str(uuid4()),
                    "state_dir": str(self.root / "state"),
                    "targets": [
                        {
                            "id": "fleet",
                            "kind": "fleet",
                            "working_directory": str(self.root),
                            "artifact_root": str(self.root / "artifacts"),
                            "fleet_file": str(self.root / "fleet.json"),
                            "instance_dir": None,
                            "adapter": {
                                "id": "ssh-v1",
                                "settings_path": str(self.root / "settings.json"),
                            },
                            "credential_env": ["FLEET_API_KEY"],
                            "actions": ["fleet_deploy", "deployment_cleanup"],
                            "capabilities": ["inspect", "measure", "mutate"],
                            "recipes": [
                                {
                                    "id": "standard",
                                    "kind": "fleet",
                                    "path": str(self.root / "recipe.json"),
                                }
                            ],
                        }
                    ],
                }
            )
        )
        self.target = self.registry.targets[0]
        self.current = {
            "operation_id": str(uuid4()),
            "action": "fleet_deploy",
            "tool": "plan_execute",
            "plan_id": None,
            "state": "running",
            "stages": [],
        }
        self.context = SimpleNamespace(
            registry=self.registry,
            target=self.target,
            target_id=self.target.id,
            operation_id=self.current["operation_id"],
            stage=None,
            stage_id=None,
            read_only=False,
            deadline=time.monotonic() + 30,
            operation_deadline=time.monotonic() + 30,
            assert_current=Mock(),
            access=InspectionAccess(self.registry),
            store=SimpleNamespace(read=Mock(), plan=Mock()),
            read=lambda: copy.deepcopy(self.current),
        )
        self.plans = PlanStore(self.registry, self.target.id)
        self.inventories = {}
        for number, name in enumerate(("one", "two", "router"), 1):
            self.inventories[name] = {
                "machine_id": f"{number:032x}",
                "boot_id": str(uuid4()),
                "netns": f"net:[{4000 + number}]",
                "hostname": name,
                "python": [3, 12, 0],
                "python_executable": "/opt/python/bin/python3.12",
                "runtime": "rocm",
                "interfaces": {"eth0": [f"192.0.2.{10 + number}"]},
                "ports": [],
                "containers": [],
                "gpu_clients": {"complete": True, "processes": []},
                "tools": {
                    name: "/usr/bin/" + name
                    for name in (
                        "python3",
                        "git",
                        "make",
                        "curl",
                        "docker",
                        "ip",
                        "rocminfo",
                        "iperf3",
                    )
                },
                "gpus": [
                    {
                        "index": index,
                        "uuid": f"GPU-{number}-{index}",
                        "pci": f"0000:{65 + index:02x}:00.0",
                        "product": "synthetic-gpu",
                        "device": f"/dev/dri/renderD{128 + index}",
                    }
                    for index in (0, 1)
                ],
            }
        self.models = {
            name: {
                "model_tree_sha256": "f" * 64,
                "files": [
                    {"path": "config.json", "sha256": "e" * 64},
                    {"path": "model.safetensors", "sha256": "0" * 64},
                    {"path": "tokenizer.json", "sha256": "1" * 64},
                ],
            }
            for name in ("one", "two")
        }
        self.images = dict.fromkeys(("one", "two"), "sha256:" + "d" * 64)
        environment = {
            "FLEET_API_KEY": "private-fleet-key",
            "HOST_ONE": "user@192.0.2.11",
            "HOST_TWO": "user@192.0.2.12",
            "HOST_ROUTER": "user@192.0.2.13",
        }
        for number in (1, 2):
            environment[f"NARWHAL_NODE_{number}_URL"] = f"http://192.0.2.{10 + number}:8000"
            environment[f"NARWHAL_NODE_{number}_ATTESTATION_URL"] = (
                f"http://192.0.2.{10 + number}:8010/v1/attestation"
            )
        for patcher in (
            patch.dict(os.environ, environment),
            patch.object(ssh_prepare.ssh_settings, "verify_source", return_value=self.verified),
            patch.object(ssh_prepare.provenance, "verified_source", return_value=self.source),
            patch.object(ssh_prepare, "asset", return_value=prepare_host_env),
            patch.object(
                ssh_prepare,
                "_management_host",
                return_value={"host_id": "7" * 64, "netns": 9000, "ports": []},
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "narwhal.deployment.ssh_transport.SSHTransport.probe", side_effect=self.probe
        )
        self.probed = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, value):
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(0o600)

    def probe(self, host_id, kind, parameters):
        if kind == "inventory":
            return copy.deepcopy(self.inventories[host_id])
        if kind == "checkpoint":
            return copy.deepcopy(self.models[host_id])
        if kind == "image":
            return {"id": self.images[host_id], "repo_digests": []}
        if kind == "router_state":
            return copy.deepcopy(self.router_state)
        self.fail(f"Unexpected SSH probe kind: {kind}")

    def prepare(self, action="fleet_deploy", parameters=None):
        parameters = {"recipe_id": "standard"} if parameters is None else parameters
        return ssh_prepare.prepare(self.context, self.target, action, parameters)

    def retain(self, prepared, action="fleet_deploy"):
        snapshot_id = str(uuid4())
        snapshot = {
            "schema": "narwhal.management-snapshot",
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "observed_at": utc_now(),
            "identity": prepared.identity,
            "observations": prepared.observations,
        }
        plan = self.plans.save(
            {
                "action": action,
                "target_id": self.target.id,
                "binding": {
                    "registration_digest": "2" * 64,
                    "adapter": {"id": "ssh-v1", "version": "1", "assets_sha256": "3" * 64},
                    "source": self.source,
                    "recipe": {
                        "recipe_id": "standard",
                        "sha256": digest((self.root / "recipe.json").read_bytes()),
                    },
                    "snapshot_id": snapshot_id,
                    "snapshot_sha256": self.plans.put_blob(canonical(snapshot)),
                    "identity_sha256": digest(canonical(prepared.identity)),
                    "inputs": [
                        {"name": name, "sha256": self.plans.put_blob(data)}
                        for name, data in sorted(prepared.inputs.items())
                    ],
                },
                "parameters": prepared.parameters,
                "stages": prepared.stages,
            }
        )
        self.current["plan_id"] = plan["plan_id"]
        return plan

    def assert_error(self, code, callback):
        with self.assertRaises(OperationError) as error:
            callback()
        self.assertEqual(error.exception.code, code, error.exception)

    def stage(self, operation):
        self.context.stage = {"operation": operation, "stage_id": operation.replace(".", "-")}
        self.context.stage_id = self.context.stage["stage_id"]

    def deployment(self, **changes):
        document = {
            "schema": "narwhal.ssh-deployment",
            "schema_version": 1,
            "registry_id": str(self.registry.registry_id),
            "target_id": self.target.id,
            "operation_id": self.context.operation_id,
            "recipe_id": "standard",
            "status": "deploying",
            "checkouts": {},
            "engines": {},
            "services": [],
            "gates": {},
            **changes,
        }
        self.write("artifacts/deployment-state.json", document)
        return document

    def test_prepare_retains_private_inputs_without_credential_values(self):
        prepared = self.prepare()
        plan = self.retain(prepared)
        self.assertEqual(self.plans.read(plan["plan_id"]), plan)
        retained = b"\n".join(prepared.inputs.values())
        self.assertNotIn(b"private-fleet-key", retained)
        self.assertIn(b"FLEET_API_KEY", retained)
        self.assertEqual(len(prepared.identity["hosts"]), 3)

    def replacement_router(self):
        owner = {
            "operation_id": str(uuid4()),
            "stage_id": "fleet-serve",
            "launch_token": str(uuid4()),
        }
        self.router_state = {
            "schema": "narwhal.state",
            "schema_version": 1,
            "ha": {"standby": False, "epoch": 0},
        }
        self.router_status = {
            "owner": owner,
            "state": "retained",
            "supervisor_present": True,
            "observed_processes": {"1234": 42},
        }
        self.deployment(
            status="ready",
            router={
                "host_id": "router",
                "url": "http://127.0.0.1:8000",
                "effect": {
                    "kind": "router",
                    "host_id": "router",
                    "effect": "confirmed",
                    "owner": owner,
                    "identity": {"job_id": owner["launch_token"]},
                },
            },
        )
        patcher = patch(
            "narwhal.deployment.ssh_transport.SSHTransport.status",
            side_effect=lambda *args: copy.deepcopy(self.router_status),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remote_or_tls_router_origin_is_rejected_before_host_observation(self):
        for origin in ("http://192.0.2.13:8000", "https://127.0.0.1:8000", "http://127.0.0.1/x"):
            with self.subTest(origin=origin):
                self.recipe["environment"]["NARWHAL_ROUTER_URL"] = origin
                self.write("recipe.json", self.recipe)
                self.assert_error("prerequisite_failed", self.prepare)
        self.probed.assert_not_called()

    def test_interpreter_path_is_absolute_and_bound_to_the_host_snapshot(self):
        prepared = self.prepare()
        hosts = json.loads(prepared.inputs["host_inventory"])
        self.assertEqual(hosts["one"]["python_executable"], "/opt/python/bin/python3.12")
        self.inventories["one"]["python_executable"] = "python3"
        self.assert_error("prerequisite_failed", self.prepare)

    def test_installed_ssh_adapter_exposes_the_production_action_manifest(self):
        adapter = installed_adapters()["ssh-v1"]
        manifest = adapter.manifest
        self.assertEqual(manifest.source, self.source)
        self.assertEqual(len(manifest.assets_sha256), 64)
        self.assertEqual(
            set(manifest.actions),
            {
                "fleet_deploy",
                "fleet_profile",
                "fleet_preflight",
                "engine_replace",
                "monitoring_start",
                "deployment_cleanup",
            },
        )

    def test_replacement_activates_profiles_before_readmission(self):
        self.replacement_router()
        prepared = self.prepare("engine_replace", {"engine_id": "n1"})
        operations = [stage["operation"] for stage in prepared.stages]
        index = operations.index("fleet.activate_profiles")
        self.assertEqual(
            operations[index - 1 : index + 2],
            ["fleet.preflight", "fleet.activate_profiles", "fleet.readmit"],
        )
        self.assertEqual(prepared.stages[index]["retain_on_success"], ["router"])
        self.assertEqual(
            prepared.stages[index]["timeout_ms"], prepared.stages[index + 1]["timeout_ms"]
        )

    def test_profile_default_selection_is_frozen_as_explicit_engine_ids(self):
        self.replacement_router()
        for parameters in ({}, {"engine_ids": []}):
            with self.subTest(parameters=parameters):
                prepared = self.prepare("fleet_profile", parameters)
                self.assertEqual(prepared.parameters, {"engine_ids": ["n1", "n2"]})

    def test_replacement_requires_individual_policy_and_owned_standalone_router(self):
        self.replacement_router()
        self.fleet["recovery"] = {"engine_restart_policy": "gang"}
        self.write("fleet.json", self.fleet)
        self.assert_error(
            "prerequisite_failed", lambda: self.prepare("engine_replace", {"engine_id": "n1"})
        )
        self.fleet["recovery"]["engine_restart_policy"] = "individual"
        self.write("fleet.json", self.fleet)
        for ha in ({"standby": True, "epoch": 0}, {"standby": False, "epoch": 1}):
            with self.subTest(ha=ha):
                self.router_state["ha"] = ha
                self.assert_error(
                    "prerequisite_failed",
                    lambda: self.prepare("engine_replace", {"engine_id": "n1"}),
                )
        self.router_state["ha"] = {"standby": False, "epoch": 0}
        self.router_status["owner"]["launch_token"] = str(uuid4())
        self.assert_error(
            "ownership_conflict", lambda: self.prepare("engine_replace", {"engine_id": "n1"})
        )

    def test_replacement_rechecks_standalone_router_before_effects(self):
        self.replacement_router()
        prepared = self.prepare("engine_replace", {"engine_id": "n1"})
        plan = self.retain(prepared, action="engine_replace")
        self.stage("fleet.drain")
        self.router_state["ha"]["epoch"] = 1
        self.assert_error("stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan))

    def test_replacement_rejects_gpu_client_arriving_after_engine_stop(self):
        self.replacement_router()
        plan = self.retain(
            self.prepare("engine_replace", {"engine_id": "n1"}), action="engine_replace"
        )
        self.stage("fleet.launch")
        self.inventories["one"]["gpu_clients"]["processes"] = [
            {"pid": 333, "start_ticks": 42, "devices": ["all"]}
        ]
        self.assert_error("fleet_busy", lambda: ssh_prepare.check(self.context, self.target, plan))

    def test_excessive_fabric_jobs_are_rejected_before_host_observation(self):
        hosts = tuple(SimpleNamespace(roles=["engine-" + str(index)]) for index in range(100))
        recipe = ssh_prepare.ssh_settings.SSHRecipe.model_validate_json(json.dumps(self.recipe))
        self.assert_error(
            "invalid_input",
            lambda: ssh_prepare._effect_budget(
                "fleet_deploy", hosts, self.launch["engines"], recipe
            ),
        )
        self.probed.assert_not_called()

    def test_gpu_index_and_uuid_aliases_resolve_to_physical_resources(self):
        self.launch["engines"]["engine-1"]["gpu_ids"] = ["1", "GPU-1-0"]
        self.write("launch.json", self.launch)
        prepared = self.prepare()
        allocations = prepared.identity["allocations"]["engine-1"]["gpu_ids"]
        self.assertEqual(allocations, ["GPU-1-0", "GPU-1-1"])
        host = prepared.identity["hosts"]["one"]["host_id"]
        for gpu in allocations:
            self.assertIn(f"host:{host}:gpu:{gpu}", prepared.identity["resources"])

    def test_aliases_of_same_gpu_within_allocation_are_rejected(self):
        self.launch["engines"]["engine-1"]["gpu_ids"] = ["0", "GPU-1-0"]
        self.write("launch.json", self.launch)
        self.assert_error("resource_conflict", self.prepare)

    def test_two_ssh_aliases_cannot_allocate_the_same_physical_gpu(self):
        self.inventories["two"] = copy.deepcopy(self.inventories["one"])
        self.inventories["two"]["interfaces"]["eth0"].append("192.0.2.12")
        self.assert_error("resource_conflict", self.prepare)

    def test_conflicting_inventory_alias_is_rejected(self):
        self.inventories["one"]["gpus"][1]["index"] = 0
        self.assert_error("prerequisite_failed", self.prepare)

    def test_gpu_without_uuid_uses_pci_identity(self):
        selected = self.inventories["one"]["gpus"][0]
        selected["uuid"] = None
        prepared = self.prepare()
        self.assertIn(selected["pci"], prepared.identity["allocations"]["engine-1"]["gpu_ids"])

    def test_existing_listener_blocks_new_deployment(self):
        self.inventories["one"]["ports"] = [8000]
        self.assert_error("resource_busy", self.prepare)

    def test_router_alias_cannot_reserve_an_engine_port_on_same_physical_host(self):
        self.inventories["router"] = copy.deepcopy(self.inventories["one"])
        self.assert_error("resource_conflict", self.prepare)

    def test_selected_gpu_client_blocks_new_deployment(self):
        self.inventories["one"]["gpu_clients"]["processes"] = [
            {"pid": 1234, "devices": [self.inventories["one"]["gpus"][0]["pci"]]}
        ]
        self.assert_error("fleet_busy", self.prepare)

    def test_incomplete_gpu_client_inventory_blocks_new_deployment(self):
        self.inventories["one"]["gpu_clients"]["complete"] = False
        self.assert_error("prerequisite_failed", self.prepare)

    def test_full_checkpoint_manifest_must_match_between_engines(self):
        self.models["two"]["model_tree_sha256"] = "4" * 64
        self.assert_error("prerequisite_failed", self.prepare)

    def test_reservations_cover_engines_router_monitoring_fabric_and_local_tunnels(self):
        prepared = self.prepare()
        resources = prepared.identity["resources"]
        self.assertEqual(resources, sorted(set(resources)))
        self.assertLessEqual(len(resources), 4096)
        for name in ("one", "two"):
            host = prepared.identity["hosts"][name]["host_id"]
            netns = prepared.identity["hosts"][name]["netns"]
            for port in (8000, 8010, 5600, 39000, 39001, 39002, 39003, 5201):
                self.assertIn(f"host:{host}:netns:{netns}:tcp:{port}", resources)
        router = prepared.identity["hosts"]["router"]
        for port in (8000, 9090, 3000):
            self.assertIn(f"host:{router['host_id']}:netns:{router['netns']}:tcp:{port}", resources)
        for port in (18000, 19090, 13000):
            self.assertTrue(any(value.endswith(f":tcp:{port}") for value in resources))

    def test_resource_limit_is_enforced_before_admission(self):
        self.recipe["environment"]["NARWHAL_UCX_TCP_PORT_RANGE"] = "39000-41047"
        self.write("recipe.json", self.recipe)
        self.assert_error("invalid_input", self.prepare)

    def test_endpoint_rotation_invalidates_a_retained_plan(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.discover")
        with patch.dict(os.environ, {"NARWHAL_NODE_1_URL": "http://192.0.2.111:8000"}):
            self.assert_error(
                "stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan)
            )

    def test_interface_address_change_invalidates_a_retained_plan(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.discover")
        self.inventories["one"]["interfaces"]["eth0"] = ["192.0.2.111"]
        self.assert_error("stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan))

    def test_registered_input_edits_invalidate_the_plan(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.discover")
        for name in (
            "fleet.json",
            "hosts.json",
            "launch.json",
            "settings.json",
            "recipe.json",
            "known_hosts",
        ):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n")
                    self.assert_error(
                        "stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan)
                    )
                finally:
                    path.write_bytes(original)

    def test_credential_rotation_preserves_a_retained_plan(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.discover")
        with patch.dict(os.environ, {"FLEET_API_KEY": "replacement-private-key"}):
            ssh_prepare.check(self.context, self.target, plan)

    def test_own_deployment_state_progression_does_not_stale_the_plan(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.monitor")
        self.deployment(gates={"A": {"status": "success"}})
        ssh_prepare.check(self.context, self.target, plan)

    def test_another_operation_cannot_replace_the_recorded_deployment_generation(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.monitor")
        self.deployment(operation_id=str(uuid4()))
        self.assert_error("stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan))

    def test_whole_checkpoint_recheck_occurs_at_engine_launch(self):
        plan = self.retain(self.prepare())
        self.probed.reset_mock()
        self.stage("fleet.discover")
        ssh_prepare.check(self.context, self.target, plan)
        self.assertFalse(any(call.args[1] == "checkpoint" for call in self.probed.call_args_list))
        self.stage("fleet.launch")
        ssh_prepare.check(self.context, self.target, plan)
        self.assertEqual(
            sum(call.args[1] == "checkpoint" for call in self.probed.call_args_list), 2
        )

    def test_changed_model_weights_block_engine_launch(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.launch")
        self.models["one"]["files"][1]["sha256"] = "5" * 64
        self.models["one"]["model_tree_sha256"] = "6" * 64
        self.assert_error("stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan))

    def test_changed_gpu_identity_blocks_engine_launch(self):
        plan = self.retain(self.prepare())
        self.stage("fleet.launch")
        self.inventories["one"]["gpus"][0]["uuid"] = "GPU-replaced"
        self.assert_error("stale_plan", lambda: ssh_prepare.check(self.context, self.target, plan))

    def test_cleanup_uses_retained_execution_plan_without_state_or_model(self):
        original = self.retain(self.prepare())
        original_operation = {
            "operation_id": str(uuid4()),
            "target_id": self.target.id,
            "registry_id": str(self.registry.registry_id),
            "action": "fleet_deploy",
            "tool": "plan_execute",
            "plan_id": original["plan_id"],
            "plan_digest": original["plan_digest"],
            "state": "failed",
            "stages": [],
            "resources": [],
        }
        self.context.store.read.return_value = original_operation
        self.context.store.plan.return_value = original
        self.models.clear()
        self.images.clear()
        self.probed.reset_mock()
        prepared = self.prepare(
            "deployment_cleanup", {"operation_id": original_operation["operation_id"]}
        )
        self.assertEqual([stage["operation"] for stage in prepared.stages], ["fleet.cleanup"])
        self.assertEqual(json.loads(prepared.inputs["cleanup_selection"]), original_operation)
        self.assertFalse(
            any(call.args[1] in {"checkpoint", "image"} for call in self.probed.call_args_list)
        )
        self.assertFalse((self.root / "artifacts" / "deployment-state.json").exists())

    def test_failed_cleanup_retains_original_selection_for_bounded_retry(self):
        original = self.retain(self.prepare())
        original_operation = {
            "operation_id": str(uuid4()),
            "target_id": self.target.id,
            "registry_id": str(self.registry.registry_id),
            "action": "fleet_deploy",
            "tool": "plan_execute",
            "plan_id": original["plan_id"],
            "state": "recovery_required",
            "stages": [],
        }
        self.context.store.read.return_value = original_operation
        self.context.store.plan.return_value = original
        prepared = self.prepare(
            "deployment_cleanup", {"operation_id": original_operation["operation_id"]}
        )
        cleanup_plan = self.retain(prepared, action="deployment_cleanup")
        cleanup_operation = {
            **original_operation,
            "operation_id": str(uuid4()),
            "action": "deployment_cleanup",
            "plan_id": cleanup_plan["plan_id"],
        }
        self.context.store.read.return_value = cleanup_operation
        self.context.store.plan.return_value = cleanup_plan
        retry = self.prepare(
            "deployment_cleanup", {"operation_id": cleanup_operation["operation_id"]}
        )
        selection = json.loads(retry.inputs["cleanup_selection"])
        self.assertEqual(selection["operation_id"], cleanup_operation["operation_id"])
        self.assertEqual(selection["predecessors"], [original_operation])
        self.assertEqual(retry.identity["resources"], prepared.identity["resources"])
        self.assertEqual(len(retry.inputs), 12)
        cleanup_operation["operation_id"] = original_operation["operation_id"]
        self.assert_error(
            "invalid_input",
            lambda: self.prepare(
                "deployment_cleanup", {"operation_id": cleanup_operation["operation_id"]}
            ),
        )
