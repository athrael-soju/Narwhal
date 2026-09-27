"""Keep unknown remote ownership reserved and preserve exported stage evidence."""

from __future__ import annotations

import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from narwhal.deployment import ssh_adapter, ssh_workflow
from narwhal.deployment.management_executor import StageOutcome, run_operation
from narwhal.deployment.management_records import OperationError
from narwhal.diagnostics.management_artifacts import ArtifactStore
from tests.deployment import test_management_executor as executor_fixtures


class SSHOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.effect = {
            "resource_id": "ssh:host:" + str(uuid4()),
            "host_id": "host",
            "kind": "measurement_helper",
            "owner": {
                "operation_id": str(uuid4()),
                "stage_id": "gate",
                "launch_token": str(uuid4()),
            },
            "effect": "confirmed",
        }
        self.receipt = {
            "owner": self.effect["owner"],
            "job_id": self.effect["owner"]["launch_token"],
            "state": "cancelled",
            "observed_processes": {},
            "containers": [],
            "cleanup": {"error": None},
        }
        self.session = ssh_adapter.Session.__new__(ssh_adapter.Session)
        self.session.context = SimpleNamespace(
            assert_current=Mock(),
            hard_deadline=time.monotonic() + 10,
            stage_id="gate",
            stage={"cleanup": {"term_grace_ms": 100, "kill_grace_ms": 100, "reconcile_ms": 100}},
            read=lambda: {"stages": [{"stage_id": "gate", "effects": [self.effect]}]},
            record_effect=Mock(),
            record_intent=Mock(),
        )
        self.session.export = Mock()
        self.session.save = Mock()
        self.session.state = {"services": []}
        self.session.transport = SimpleNamespace(
            status=Mock(return_value=self.receipt),
            cancel=Mock(),
            cancel_owned=Mock(),
            status_owned=Mock(return_value=self.receipt),
        )

    def test_cleanup_cannot_treat_failed_container_inspection_as_absence(self):
        for changes in (
            {"state": "recovery_required"},
            {"cleanup": {"error": "source_unavailable"}},
            {"container_error": "source_unavailable"},
        ):
            with self.subTest(changes=changes):
                self.session.transport.status.return_value = {**self.receipt, **changes}
                with self.assertRaises(OperationError) as error:
                    ssh_workflow._stop_effect(self.session, self.effect)
                self.assertEqual(error.exception.code, "recovery_required")

    def test_cleanup_rechecks_owner_after_cancellation(self):
        changed = copy.deepcopy(self.receipt)
        changed["owner"]["launch_token"] = str(uuid4())
        self.session.transport.status.side_effect = [self.receipt, changed]
        with self.assertRaises(OperationError) as error:
            ssh_workflow._stop_effect(self.session, self.effect)
        self.assertEqual(error.exception.code, "ownership_conflict")

    def test_stage_cleanup_waits_for_terminal_observation_before_absence(self):
        pending = {**self.receipt, "state": "running"}
        self.session.transport.status_owned.side_effect = [pending, self.receipt]
        with patch.object(ssh_adapter.time, "sleep"):
            self.session.cleanup_current()
        self.assertEqual(self.session.transport.status_owned.call_count, 2)
        self.session.context.record_effect.assert_called_once()
        self.assertEqual(self.effect["effect"], "absent")

    def test_stage_cleanup_preserves_unknown_container_ownership(self):
        self.session.transport.status_owned.return_value = {
            **self.receipt,
            "state": "recovery_required",
            "cleanup": {"error": "source_unavailable"},
        }
        self.session.cleanup_current()
        self.session.context.record_effect.assert_not_called()
        self.assertEqual(self.effect["effect"], "confirmed")

    def test_generation_check_uses_captured_engine_identity_and_owned_gpu_clients(self):
        self.session.plan = {"payload": {"action": "engine_replace"}}
        self.session.state["engines"] = {
            "engine-1": {
                "effect": self.effect,
                "container_id": "container",
                "generation": {"vllm_version": "0.22.0", "process_start_time_seconds": 100.25},
            }
        }
        self.session.transport.status.return_value = {**self.receipt, "state": "retained"}
        self.session.transport.inspect_containers = Mock(
            return_value={
                "containers": [
                    {
                        "container_id": "container",
                        "running": True,
                        "process": {"pid": 101, "start_ticks": 1001},
                        "descendants": {"102": 1002},
                    }
                ]
            }
        )
        self.session.role_environment = Mock(
            return_value={"NARWHAL_NODE_1_URL": "http://engine:8001"}
        )
        generation = {"version": "0.22.0", "process_start_time_seconds": "100.250"}
        inventory = {
            "gpu_clients": {"complete": True, "processes": [{"pid": 102, "start_ticks": 1002}]}
        }
        self.session.transport.probe = Mock(side_effect=[generation, inventory])
        self.session.check_generations()
        self.session.transport.probe.side_effect = [
            {**generation, "process_start_time_seconds": "200.25"}
        ]
        with self.assertRaises(OperationError) as error:
            self.session.check_generations()
        self.assertEqual(error.exception.code, "stale_plan")
        inventory["gpu_clients"]["processes"].append({"pid": 103, "start_ticks": 1003})
        self.session.transport.probe.side_effect = [generation, inventory]
        with self.assertRaises(OperationError) as error:
            self.session.check_generations()
        self.assertEqual(error.exception.code, "resource_conflict")

    def test_reconciliation_keeps_unknown_remote_helpers_reserved(self):
        self.session.transport.status.return_value = {
            **self.receipt,
            "state": "recovery_required",
            "cleanup": {"error": "source_unavailable"},
        }
        context = SimpleNamespace(
            store=SimpleNamespace(plan=Mock(return_value={"payload": {"action": "fleet_deploy"}})),
            target_id="fleet",
            operation_id="op",
        )
        with patch.object(ssh_adapter, "Session", return_value=self.session):
            result = ssh_adapter.SSHAdapter().reconcile(
                context, {"stages": [{"effects": [self.effect]}]}
            )
        self.assertFalse(result.helpers_stopped)
        self.assertFalse(result.complete)
        self.assertEqual(result.effects[0]["effect"], "unknown")

    def test_cleanup_retries_stop_newer_effects_before_predecessors(self):
        older = {**self.effect, "resource_id": "older"}
        newer = {**self.effect, "resource_id": "newer"}
        selection = {
            "stages": [{"effects": [newer]}],
            "predecessors": [{"stages": [{"effects": [older]}]}],
        }
        self.session.plan = {}
        self.session.state = {"services": [older, newer]}
        stopped = []

        def stop(session, effect):
            stopped.append(effect["resource_id"])
            return {
                "resource_id": effect["resource_id"],
                "effect": "absent",
                "receipt": self.receipt,
            }

        with (
            patch.object(ssh_workflow, "input_document", return_value=selection),
            patch.object(ssh_workflow, "_stop_effect", side_effect=stop),
        ):
            ssh_workflow._cleanup(self.session)
        self.assertEqual(stopped, ["newer", "older"])
        self.assertEqual(self.session.state["status"], "removed")

    def test_cleanup_records_all_predecessor_effects_before_stop_and_retains_failures(self):
        peer = {**self.effect, "resource_id": "second"}
        selection = {"stages": [{"effects": [self.effect, peer]}]}
        self.session.plan = {}
        self.session.state = {"services": [self.effect, peer]}

        def stop(session, effect):
            self.assertEqual(self.session.context.record_intent.call_count, 2)
            raise OperationError("source_unavailable", "Host is unavailable")

        with (
            patch.object(ssh_workflow, "input_document", return_value=selection),
            patch.object(ssh_workflow, "_stop_effect", side_effect=stop),
            self.assertRaises(OperationError),
        ):
            ssh_workflow._cleanup(self.session)
        self.session.context.record_effect.assert_not_called()
        for call in self.session.context.record_intent.call_args_list:
            self.assertEqual(call.args[0]["effect"], "unknown")
            self.assertEqual(call.args[0]["owner"], self.effect["owner"])
        report = self.session.export.call_args.args[1]
        self.assertEqual(len(report["residual"]), 2)
        self.assertEqual(report["removed"], [])

    def test_cleanup_reconciliation_requires_owned_services_to_be_absent(self):
        self.session.transport.status.return_value = {**self.receipt, "state": "retained"}
        engine = {**self.effect, "kind": "engine"}
        context = SimpleNamespace(
            store=SimpleNamespace(
                plan=Mock(return_value={"payload": {"action": "deployment_cleanup"}})
            ),
            target_id="fleet",
            operation_id="op",
        )
        with (
            patch.object(ssh_adapter, "Session", return_value=self.session),
            patch.object(
                ssh_adapter.ssh_prepare,
                "input_document",
                return_value={"stages": [{"effects": [engine]}]},
            ),
        ):
            result = ssh_adapter.SSHAdapter().reconcile(
                context, {"stages": [{"effects": [engine]}]}
            )
        self.assertFalse(result.complete)
        self.assertEqual(result.effects[0]["effect"], "unknown")

    def test_cleanup_crash_before_first_intent_cannot_release_predecessor_reservations(self):
        context = SimpleNamespace(
            store=SimpleNamespace(
                plan=Mock(return_value={"payload": {"action": "deployment_cleanup"}})
            ),
            target_id="fleet",
            operation_id="op",
        )
        selection = {"stages": [{"effects": [self.effect]}]}
        with (
            patch.object(ssh_adapter, "Session", return_value=self.session),
            patch.object(ssh_adapter.ssh_prepare, "input_document", return_value=selection),
        ):
            result = ssh_adapter.SSHAdapter().reconcile(context, {"stages": [{"effects": []}]})
        self.assertFalse(result.helpers_stopped)
        self.assertFalse(result.complete)
        self.assertEqual(result.errors[0]["code"], "recovery_required")


class ArtifactOutcomeTests(unittest.TestCase):
    def test_previously_persisted_artifact_appears_once_in_completed_operation(self):
        fixture = executor_fixtures.ManagementExecutorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        operation_id = fixture.admit()
        references = []

        class Adapter(executor_fixtures.CPUAdapter):
            def execute_stage(adapter, context, stage, plan):
                reference = ArtifactStore(str(context.registry.registry_id), context.target).export(
                    b'{"measurement":"synthetic"}', "measurement"
                )
                context.update(lambda record: record["artifacts"].append(reference))
                references.append(reference)
                return StageOutcome(artifacts=[reference])

        result = run_operation(
            fixture.registry,
            "cpu",
            operation_id,
            adapter=Adapter(fixture.root),
            plan=fixture.plan,
        )
        self.assertEqual(result["state"], "succeeded")
        selected = references[0]["artifact_id"]
        self.assertEqual(sum(row["artifact_id"] == selected for row in result["artifacts"]), 1)
        self.assertEqual(result["stages"][0]["artifacts"], references)
