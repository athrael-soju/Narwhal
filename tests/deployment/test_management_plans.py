"""Check immutable plan evidence and coordinator admission against a CPU adapter."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment.management_access import InspectionAccess
from narwhal.deployment.management_adapters import AdapterManifest, PreparedPlan
from narwhal.deployment.management_coordinator import OperationCoordinator, action_parameters
from narwhal.deployment.management_executor import ReconcileOutcome, StageOutcome
from narwhal.deployment.management_exports import public_value
from narwhal.deployment.management_plans import PlanStore, canonical, digest, validate_plan
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.diagnostics.management_artifacts import ArtifactStore


class FixtureAdapter:
    """Use in-memory generations to test binding decisions without a GPU claim."""

    manifest = AdapterManifest(
        id="local-dev-v1",
        version="fixture-1",
        assets_sha256="a" * 64,
        source={
            "commit": "b" * 40,
            "distribution_version": "0.0.0",
            "wheel_sha256": "c" * 64,
            "bundle_sha256": None,
        },
        actions={"dev_verify": frozenset({"verify"})},
    )

    def __init__(self):
        self.generation = 1
        self.fail = False
        self.executions = 0

    def prepare(self, context, target, action, parameters):
        return PreparedPlan(
            identity={"generation": self.generation, "resources": ["gpu:fixture-physical-id"]},
            observations={"ready": True, "load": 0.5},
            inputs={"host_inventory": json.dumps({"generation": self.generation}).encode()},
            parameters=parameters,
            stages=[
                {
                    "stage_id": "verify",
                    "gate": None,
                    "operation": "verify",
                    "depends_on": [],
                    "subjects": [target.id],
                    "input_names": ["host_inventory"],
                    "timeout_ms": 5000,
                    "cleanup": {
                        "policy": "temporary_only",
                        "term_grace_ms": 100,
                        "kill_grace_ms": 100,
                        "reconcile_ms": 100,
                    },
                    "retain_on_success": [],
                }
            ],
        )

    def check(self, context, target, plan):
        expected = digest(
            canonical({"generation": self.generation, "resources": ["gpu:fixture-physical-id"]})
        )
        if plan["payload"]["binding"]["identity_sha256"] != expected:
            raise OperationError("stale_plan", "Fixture generation changed")

    def execute_stage(self, context, stage, plan):
        context.assert_current()
        self.executions += 1
        return StageOutcome(status="failed_gate" if self.fail else "success")

    def reconcile(self, context, operation):
        return ReconcileOutcome(helpers_stopped=True)


class PlanTests(unittest.TestCase):
    """Exercise core APIs used by every future execution entry point."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": str(uuid4()),
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "dev",
                    "kind": "dev",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "artifacts"),
                    "fleet_file": None,
                    "instance_dir": str(self.root / "instance"),
                    "adapter": {"id": "local-dev-v1", "settings_path": None},
                    "actions": ["dev_verify"],
                    "capabilities": ["inspect", "measure"],
                }
            ],
        }
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        self.adapter = FixtureAdapter()
        self.launches = []
        self.coordinator = OperationCoordinator(
            self.registry,
            adapters={"local-dev-v1": self.adapter},
            launcher=lambda *args: self.launches.append(args),
        )
        self.store = PlanStore(self.registry, "dev")

    def plan(self):
        operation = self.coordinator.submit_prepare("dev", "dev_verify", {}, str(uuid4()))
        result = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(result["state"], "succeeded", result["result"])
        return self.store.read(result["result"]["data"]["plan_id"])

    def test_prepare_execute_and_frozen_plan_exports(self):
        plan = self.plan()
        view = self.coordinator.inspect_plan("dev", plan["plan_id"])
        self.assertEqual(view.data["plan"]["plan_digest"], plan["plan_digest"])
        artifact = ArtifactStore(str(self.registry.registry_id), self.registry.targets[0])
        snapshot = artifact.read(view.data["snapshot_artifact_id"])
        self.assertEqual(json.loads(snapshot["text"])["identity"]["generation"], 1)
        request = str(uuid4())
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], request)
        repeated = self.coordinator.submit_execute("dev", plan["plan_id"], request)
        self.assertEqual(operation["operation_id"], repeated["operation_id"])
        result = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(result["state"], "succeeded", result["result"])
        self.assertEqual(self.adapter.executions, 1)

    def test_changed_identity_prevents_stage_and_fresh_resume_runs_new_evidence(self):
        plan = self.plan()
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        self.adapter.generation = 2
        result = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(self.adapter.executions, 0)
        self.assertEqual(result["result"]["errors"][0]["code"], "stale_plan")
        new_plan = self.plan()
        resumed = self.coordinator.resume(
            "dev", operation["operation_id"], new_plan["plan_id"], str(uuid4())
        )
        self.assertEqual(resumed["parent_operation_id"], operation["operation_id"])
        completed = self.coordinator.run_worker("dev", resumed["operation_id"])
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertTrue(all(stage["reused_from"] is None for stage in completed["stages"]))
        self.assertEqual(
            self.coordinator.store.read("dev", operation["operation_id"])["state"], "failed"
        )

    def test_replay_survives_missing_plan_and_disabled_adapter(self):
        plan = self.plan()
        request_id = str(uuid4())
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], request_id)
        self.coordinator.run_worker("dev", operation["operation_id"])
        path = self.registry.state_dir.joinpath(*self.store.parts, plan["plan_id"] + ".json")
        path.unlink()
        coordinator = OperationCoordinator(self.registry)
        repeated = coordinator.submit_execute("dev", plan["plan_id"], request_id)
        self.assertEqual(repeated["operation_id"], operation["operation_id"])
        alias_request = str(uuid4())
        aliased = coordinator.submit_execute("dev", plan["plan_id"], alias_request)
        self.assertEqual(aliased["operation_id"], operation["operation_id"])
        with self.assertRaises(OperationError) as error:
            coordinator.submit_execute("dev", str(uuid4()), request_id)
        self.assertEqual(error.exception.code, "request_id_conflict")
        coordinator.store.tombstone("dev", operation["operation_id"])
        with self.assertRaises(OperationError) as error:
            coordinator.submit_execute("dev", plan["plan_id"], alias_request)
        self.assertEqual(error.exception.code, "operation_record_removed")
        self.assertEqual(self.adapter.executions, 1)

    def test_revocation_before_worker_start_records_failure_without_stage(self):
        plan = self.plan()
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        self.document["targets"][0]["capabilities"] = ["inspect"]
        denied = ManagementRegistry.model_validate_json(json.dumps(self.document))
        coordinator = OperationCoordinator(denied, adapters={"local-dev-v1": self.adapter})
        result = coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(result["state"], "failed", result["result"])
        self.assertEqual(result["result"]["errors"][0]["code"], "permission_denied")
        self.assertEqual(self.adapter.executions, 0)

    def test_endpoint_rotation_invalidates_binding_but_credential_rotation_does_not(self):
        self.document["targets"][0]["endpoints"] = {"router_env": "CPU_ROUTER_URL"}
        self.document["targets"][0]["credential_env"] = ["CPU_API_TOKEN"]
        registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        coordinator = OperationCoordinator(registry, adapters={"local-dev-v1": self.adapter})
        target = registry.targets[0]
        with patch.dict(
            "os.environ",
            {"CPU_ROUTER_URL": "http://127.0.0.1:1234", "CPU_API_TOKEN": "first-secret"},
        ):
            before = coordinator._registration_digest(target)
            with patch.dict("os.environ", {"CPU_API_TOKEN": "second-secret"}):
                self.assertEqual(before, coordinator._registration_digest(target))
            with patch.dict("os.environ", {"CPU_ROUTER_URL": "http://127.0.0.1:2345"}):
                self.assertNotEqual(before, coordinator._registration_digest(target))

    def test_literal_credentials_are_rejected_before_any_plan_blob_is_written(self):
        original = self.adapter.prepare

        def contaminated(*arguments):
            prepared = original(*arguments)
            prepared.inputs["host_inventory"] = b'{"api_key":"not-in-environment"}'
            return prepared

        with patch.object(self.adapter, "prepare", side_effect=contaminated):
            operation = self.coordinator.submit_prepare("dev", "dev_verify", {}, str(uuid4()))
            result = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["errors"][0]["code"], "permission_denied")
        self.assertFalse(list(self.registry.state_dir.rglob("*.blob")))

    def test_exports_redact_destinations_and_preserve_opaque_ownership(self):
        token = str(uuid4())
        value = {
            "ssh_destination": "user@10.1.2.3",
            "address": "10.2.3.4",
            "path": str(self.root / "secret"),
            "owner": {"launch_token": token},
            "resource_id": "helper:" + token,
        }
        public = public_value(InspectionAccess(self.registry), self.registry.targets[0], value)
        self.assertEqual(public["owner"]["launch_token"], token)
        self.assertEqual(public["resource_id"], value["resource_id"])
        self.assertNotIn("10.1.2.3", json.dumps(public))
        self.assertNotIn("10.2.3.4", json.dumps(public))
        self.assertNotIn(str(self.root), json.dumps(public))

    def test_input_tampering_and_missing_evidence_prevent_execution(self):
        plan = self.plan()
        identity = plan["payload"]["binding"]["inputs"][0]["sha256"]
        path = self.registry.state_dir.joinpath(*self.store.parts, identity + ".blob")
        path.write_text("changed")
        with self.assertRaises(OperationError) as error:
            self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        self.assertEqual(error.exception.code, "plan_evidence_changed")
        path.unlink()
        with self.assertRaises(OperationError) as error:
            self.coordinator.inspect_plan("dev", plan["plan_id"])
        self.assertEqual(error.exception.code, "plan_evidence_missing")
        self.assertEqual(self.adapter.executions, 0)

    def test_validation_rejects_float_digest_cycles_missing_gates_and_source(self):
        plan = self.plan()
        variations = []
        changed = copy.deepcopy(plan)
        changed["payload"]["stages"][0]["depends_on"] = ["verify"]
        variations.append(changed)
        changed = copy.deepcopy(plan)
        changed["payload"]["stages"][0]["timeout_ms"] = 2.5
        variations.append(changed)
        changed = copy.deepcopy(plan)
        changed["payload"]["action"] = "fleet_deploy"
        variations.append(changed)
        changed = copy.deepcopy(plan)
        changed["payload"]["binding"]["source"]["wheel_sha256"] = None
        variations.append(changed)
        for changed in variations:
            with self.subTest(changed=changed), self.assertRaises(OperationError):
                validate_plan(changed)

    def test_plan_storage_rejects_symlinks_and_cross_target_copy(self):
        plan = self.plan()
        path = self.registry.state_dir.joinpath(*self.store.parts, plan["plan_id"] + ".json")
        outside = self.root / "outside.json"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.store.read(plan["plan_id"])
        with self.assertRaises(OperationError):
            self.store.read("../../outside")

    def test_unavailable_adapter_and_revoked_grants_cannot_start_work(self):
        with self.assertRaises(OperationError) as error:
            OperationCoordinator(self.registry).submit_prepare(
                "dev", "dev_verify", {}, str(uuid4())
            )
        self.assertEqual(error.exception.code, "adapter_unavailable")
        self.document["targets"][0]["capabilities"] = ["inspect"]
        denied = ManagementRegistry.model_validate_json(json.dumps(self.document))
        with self.assertRaises(OperationError) as error:
            OperationCoordinator(denied, adapters={"local-dev-v1": self.adapter}).submit_prepare(
                "dev", "dev_verify", {}, str(uuid4())
            )
        self.assertEqual(error.exception.code, "permission_denied")
        self.assertEqual(self.coordinator.list_operations("dev").data["operations"], [])

    def test_canonical_action_parameters_have_no_command_escape(self):
        self.assertEqual(
            action_parameters("fleet_profile", {"engine_ids": ["b", "a"]}),
            {"engine_ids": ["a", "b"]},
        )
        for action, parameters in (
            ("dev_up", {"command": "touch x"}),
            ("engine_replace", {"engine_id": "../../x"}),
            ("fleet_profile", {"engine_ids": ["a", "a"]}),
        ):
            with self.subTest(action=action), self.assertRaises(OperationError):
                action_parameters(action, parameters)
