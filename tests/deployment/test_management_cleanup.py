"""Transfer cleanup reservations only after the original execution has stopped."""

import copy
import hashlib
import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment.management_coordinator import OperationCoordinator
from narwhal.deployment.management_executor import worker_identity
from narwhal.deployment.management_records import OperationError, canonical, utc_now
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.deployment.management_store import MAX_CLEANUP_PREDECESSORS, OperationStore
from tests.deployment.test_management_registry import registry_document
from tests.deployment.test_management_store import execution_plan, identity, terminal

RESOURCES = ["host:physical:gpu:0", "host:physical:netns:123:tcp:8000"]


def cleanup_plan(operation_id, target="fleet"):
    plan = execution_plan(target, "deployment_cleanup")
    plan["payload"]["parameters"] = {"operation_id": operation_id}
    plan["plan_digest"] = hashlib.sha256(canonical(plan["payload"])).hexdigest()
    return plan


def admit_cleanup(document, plan, request_id):
    store = OperationStore(ManagementRegistry.model_validate_json(json.dumps(document)))
    record, created = store.admit(
        target_id="fleet",
        request_id=request_id,
        tool="plan_execute",
        action="deployment_cleanup",
        parameters=plan["payload"]["parameters"],
        plan=plan,
        resources=RESOURCES,
        cleanup_of=plan["payload"]["parameters"]["operation_id"],
    )
    return record["operation_id"], created


def claim_original(document, operation_id):
    store = OperationStore(ManagementRegistry.model_validate_json(json.dumps(document)))
    try:
        store.claim("fleet", operation_id, worker_identity())
    except OperationError as error:
        return error.code
    return "claimed"


class CleanupAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = registry_document()
        self.document["state_dir"] = str(self.root / "state")
        target = self.document["targets"][0]
        target.update(
            id="fleet",
            kind="fleet",
            working_directory=str(self.root),
            artifact_root=str(self.root / "artifacts"),
            fleet_file=str(self.root / "fleet.json"),
            instance_dir=None,
            adapter={"id": "ssh-v1", "settings_path": str(self.root / "ssh.json")},
            actions=["fleet_deploy", "deployment_cleanup"],
            recipes=[],
        )
        alias = copy.deepcopy(target)
        alias["id"] = "alias"
        self.document["targets"].append(alias)
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        self.store = OperationStore(self.registry)

    def original(self, state="recovery_required", *, live=False):
        record, _ = self.store.admit(
            target_id="fleet",
            request_id=str(uuid4()),
            tool="plan_execute",
            action="fleet_deploy",
            parameters={},
            plan=execution_plan("fleet", "fleet_deploy"),
            resources=RESOURCES,
        )
        return self.advance(record, state, live=live)

    def advance(self, record, state="recovery_required", *, live=False):
        if state == "queued":
            return record
        record = self.store.claim(
            "fleet", record["operation_id"], worker_identity() if live else identity()
        )
        if state == "running":
            return record
        if state == "cancelling":
            return self.store.request_cancel("fleet", record["operation_id"])
        if state == "cancelled":
            record = self.store.request_cancel("fleet", record["operation_id"])
        if state == "recovery_required":
            changed = copy.deepcopy(record)
            changed["state"] = state
            changed["recovery"].update(reason="worker_lost", observed_at=utc_now())
        else:
            changed = terminal(record, state)
        return self.store.commit(
            "fleet", record["operation_id"], changed, record["revision"], record["worker"]["fence"]
        )

    def rewrite_selection(self, operation, predecessor_id):
        """Model corrupted immutable admission evidence for the chain rejection tests."""
        request = self.store.request("fleet", operation["operation_id"])
        plan = self.store.plan("fleet", operation["operation_id"])
        request["parameters"] = {"operation_id": predecessor_id}
        plan["payload"]["parameters"] = request["parameters"]
        plan["plan_digest"] = hashlib.sha256(canonical(plan["payload"])).hexdigest()
        request["plan_digest"] = plan["plan_digest"]
        operation = {**operation, "plan_digest": plan["plan_digest"]}
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE operations SET document=?,request=?,plan=? WHERE operation_id=?",
                (
                    canonical(operation),
                    canonical(request),
                    canonical(plan),
                    operation["operation_id"],
                ),
            )
        return operation

    def cleanup(self, original, *, plan=None, resources=RESOURCES, **changes):
        plan = plan or cleanup_plan(original["operation_id"])
        arguments = {
            "target_id": "fleet",
            "request_id": str(uuid4()),
            "tool": "plan_execute",
            "action": "deployment_cleanup",
            "parameters": plan["payload"]["parameters"],
            "plan": plan,
            "resources": resources,
            "cleanup_of": original["operation_id"],
            **changes,
        }
        return self.store.admit(**arguments)

    def assert_code(self, code, callback):
        with self.assertRaises(OperationError) as error:
            callback()
        self.assertEqual(error.exception.code, code, error.exception)

    def reservations(self):
        with self.store.connection() as connection:
            return connection.execute(
                "SELECT resource_id,operation_id,fence FROM reservations ORDER BY resource_id"
            ).fetchall()

    def test_recovery_transfer_preserves_original_record_and_evidence(self):
        original = self.original()
        original_plan = self.store.plan("fleet", original["operation_id"])
        original_request = self.store.request("fleet", original["operation_id"])
        cleanup, created = self.cleanup(original)
        self.assertTrue(created)
        self.assertEqual(self.store.read("fleet", original["operation_id"]), original)
        self.assertEqual(self.store.plan("fleet", original["operation_id"]), original_plan)
        self.assertEqual(self.store.request("fleet", original["operation_id"]), original_request)
        self.assertEqual(
            [(resource, owner) for resource, owner, _ in self.reservations()],
            [(resource, cleanup["operation_id"]) for resource in RESOURCES],
        )
        self.assertTrue(all(row[2] > original["worker"]["fence"] for row in self.reservations()))
        # Late reconciliation of the original cannot release the cleanup's reservations.
        self.store.commit(
            "fleet", original["operation_id"], terminal(original), original["revision"], None
        )
        self.assertEqual({row[1] for row in self.reservations()}, {cleanup["operation_id"]})

    def test_terminal_original_can_be_cleaned_with_its_exact_resources(self):
        for state in ("succeeded", "failed", "cancelled"):
            with self.subTest(state=state):
                original = self.original(state)
                cleanup, created = self.cleanup(original)
                self.assertTrue(created)
                self.assertEqual(self.store.read("fleet", original["operation_id"]), original)
                self.store.commit(
                    "fleet", cleanup["operation_id"], terminal(cleanup), cleanup["revision"], None
                )

    def test_live_worker_blocks_transfer_even_with_expired_heartbeat(self):
        original = self.original(live=True)
        original["worker"]["heartbeat_at"] = "2000-01-01T00:00:00Z"
        original = self.store.commit(
            "fleet", original["operation_id"], original, original["revision"], None
        )
        before = self.reservations()
        self.assert_code("resource_busy", lambda: self.cleanup(original))
        self.assertEqual(self.reservations(), before)

    def test_worker_inspection_failure_does_not_authorize_transfer(self):
        original = self.original()
        before = self.reservations()
        with patch(
            "narwhal.deployment.management_executor.worker_alive",
            side_effect=OperationError("recovery_required", "Cannot inspect worker"),
        ):
            self.assert_code("recovery_required", lambda: self.cleanup(original))
        self.assertEqual(self.reservations(), before)

    def test_queued_running_and_cancelling_originals_block_cleanup(self):
        for state in ("queued", "running", "cancelling"):
            with self.subTest(state=state):
                original = self.original(state)
                self.assert_code("resource_busy", lambda original=original: self.cleanup(original))
                self.store.commit(
                    "fleet",
                    original["operation_id"],
                    terminal(original, "cancelled" if state == "cancelling" else "failed"),
                    original["revision"],
                    original["worker"]["fence"] if original["worker"] else None,
                )

    def test_store_rejects_missing_or_different_cleanup_binding(self):
        original = self.original()
        for cleanup_of in (None, str(uuid4())):
            with self.subTest(cleanup_of=cleanup_of):
                self.assert_code(
                    "invalid_input",
                    lambda cleanup_of=cleanup_of: self.cleanup(original, cleanup_of=cleanup_of),
                )
        plan = cleanup_plan(original["operation_id"])
        plan["payload"]["parameters"]["extra"] = "unrecognised"
        plan["plan_digest"] = hashlib.sha256(canonical(plan["payload"])).hexdigest()
        self.assert_code("invalid_input", lambda: self.cleanup(original, plan=plan))

    def test_cleanup_binding_cannot_bypass_action_or_plan_scope(self):
        original = self.original()
        self.assert_code("invalid_input", lambda: self.cleanup(original, action="fleet_deploy"))
        plan = cleanup_plan(original["operation_id"], "alias")
        self.assert_code("invalid_input", lambda: self.cleanup(original, plan=plan))
        self.assert_code(
            "object_not_found", lambda: self.cleanup(original, plan=plan, target_id="alias")
        )
        plan = execution_plan("fleet", "fleet_deploy")
        self.assert_code("invalid_input", lambda: self.cleanup(original, plan=plan))

    def test_resource_subset_superset_and_unrelated_resources_are_rejected(self):
        original = self.original()
        for resources in ([], RESOURCES[:1], [*RESOURCES, "other"], ["other"]):
            with self.subTest(resources=resources):
                self.assert_code(
                    "invalid_input",
                    lambda resources=resources: self.cleanup(original, resources=resources),
                )
        self.assertEqual({row[1] for row in self.reservations()}, {original["operation_id"]})

    def test_unrelated_active_reservation_cannot_be_taken_from_terminal_original(self):
        original = self.original("failed")
        unrelated, _ = self.store.admit(
            target_id="alias",
            request_id=str(uuid4()),
            tool="plan_execute",
            action="fleet_deploy",
            parameters={},
            plan=execution_plan("alias", "fleet_deploy"),
            resources=RESOURCES[:1],
        )
        self.assert_code("resource_busy", lambda: self.cleanup(original))
        self.assertEqual({row[1] for row in self.reservations()}, {unrelated["operation_id"]})

    def test_failed_transfer_rolls_back_admission_and_original_reservations(self):
        original = self.original()
        before = self.reservations()
        plan = cleanup_plan(original["operation_id"])
        request_id = str(uuid4())
        with self.store.connection() as connection:
            connection.execute(
                "CREATE TRIGGER fail_transfer BEFORE INSERT ON reservations "
                "BEGIN SELECT RAISE(ABORT, 'synthetic admission failure'); END"
            )
        self.assert_code(
            "source_unavailable", lambda: self.cleanup(original, plan=plan, request_id=request_id)
        )
        self.assertEqual(self.reservations(), before)
        with self.store.connection() as connection:
            for table in ("operations", "requests", "consumed_plans"):
                self.assertEqual(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1
                )
            connection.execute("DROP TRIGGER fail_transfer")
        self.assertTrue(self.cleanup(original, plan=plan, request_id=request_id)[1])

    def test_repeated_cleanup_preserves_acceptance_after_original_is_removed(self):
        original = self.original("failed")
        plan = cleanup_plan(original["operation_id"])
        request_id = str(uuid4())
        cleanup, _ = self.cleanup(original, plan=plan, request_id=request_id)
        self.store.tombstone("fleet", original["operation_id"])
        for request in (request_id, str(uuid4())):
            repeated, created = self.cleanup(original, plan=plan, request_id=request)
            self.assertFalse(created)
            self.assertEqual(repeated, cleanup)
        self.assert_code(
            "invalid_input",
            lambda: self.cleanup(original, plan=plan, request_id=request_id, resources=[]),
        )

    def test_concurrent_cleanup_admission_cannot_relaunch_original(self):
        original = self.original()
        plan = cleanup_plan(original["operation_id"])
        with ProcessPoolExecutor(
            max_workers=3, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            admissions = [
                pool.submit(admit_cleanup, self.document, plan, str(uuid4())) for _ in range(3)
            ]
            launch = pool.submit(claim_original, self.document, original["operation_id"])
            results = [future.result(timeout=10) for future in admissions]
            self.assertEqual(launch.result(timeout=10), "stale_worker")
        self.assertEqual(len({operation_id for operation_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual({row[1] for row in self.reservations()}, {results[0][0]})

    def test_coordinator_passes_cleanup_binding_and_replays_resources(self):
        original = self.original()
        plan = cleanup_plan(original["operation_id"])
        plan["payload"]["binding"]["snapshot_sha256"] = "a" * 64
        plan["plan_digest"] = hashlib.sha256(canonical(plan["payload"])).hexdigest()
        coordinator = OperationCoordinator(self.registry, adapters={}, launcher=lambda *_: None)
        with patch("narwhal.deployment.management_coordinator.PlanStore") as plans:
            plans.return_value.blob.return_value = json.dumps(
                {"identity": {"resources": RESOURCES}}
            )
            cleanup = coordinator._execute(self.registry.targets[0], plan, str(uuid4()))
        repeated = coordinator.submit_execute("fleet", plan["plan_id"], str(uuid4()))
        self.assertEqual(repeated["operation_id"], cleanup["operation_id"])
        self.assertEqual({row[1] for row in self.reservations()}, {cleanup["operation_id"]})

    def test_cleanup_retry_transfers_reservations_and_preserves_predecessor_evidence(self):
        for state in ("failed", "recovery_required"):
            with self.subTest(state=state):
                original = self.original()
                predecessor, _ = self.cleanup(original)
                predecessor = self.advance(predecessor, state)
                request = self.store.request("fleet", predecessor["operation_id"])
                plan = self.store.plan("fleet", predecessor["operation_id"])
                retry, created = self.cleanup(predecessor)
                self.assertTrue(created)
                self.assertEqual(self.store.read("fleet", predecessor["operation_id"]), predecessor)
                self.assertEqual(self.store.request("fleet", predecessor["operation_id"]), request)
                self.assertEqual(self.store.plan("fleet", predecessor["operation_id"]), plan)
                self.assertEqual(self.store.read("fleet", original["operation_id"]), original)
                self.assertEqual({row[1] for row in self.reservations()}, {retry["operation_id"]})
                if state == "recovery_required":
                    self.store.commit(
                        "fleet",
                        predecessor["operation_id"],
                        terminal(predecessor),
                        predecessor["revision"],
                        None,
                    )
                    self.assertEqual(
                        {row[1] for row in self.reservations()}, {retry["operation_id"]}
                    )
                self.store.commit(
                    "fleet", retry["operation_id"], terminal(retry), retry["revision"], None
                )

    def test_cleanup_retry_rejects_active_predecessor(self):
        original = self.original()
        for state in ("queued", "running", "cancelling"):
            with self.subTest(state=state):
                predecessor, _ = self.cleanup(original)
                predecessor = self.advance(predecessor, state)
                self.assert_code(
                    "resource_busy", lambda predecessor=predecessor: self.cleanup(predecessor)
                )
                self.store.commit(
                    "fleet",
                    predecessor["operation_id"],
                    terminal(predecessor, "cancelled" if state == "cancelling" else "failed"),
                    predecessor["revision"],
                    predecessor["worker"]["fence"] if predecessor["worker"] else None,
                )

    def test_cleanup_retry_rejects_live_or_unverifiable_predecessor_worker(self):
        original = self.original()
        predecessor, _ = self.cleanup(original)
        predecessor = self.advance(predecessor, live=True)
        self.assert_code("resource_busy", lambda: self.cleanup(predecessor))
        with patch(
            "narwhal.deployment.management_executor.worker_alive",
            side_effect=OperationError("recovery_required", "Cannot inspect worker"),
        ):
            self.assert_code("recovery_required", lambda: self.cleanup(predecessor))
        self.assertEqual({row[1] for row in self.reservations()}, {predecessor["operation_id"]})

    def test_cleanup_chain_rejects_cycles_without_releasing_reservations(self):
        original = self.original()
        predecessor, _ = self.cleanup(original)
        predecessor = self.advance(predecessor)
        predecessor = self.rewrite_selection(predecessor, predecessor["operation_id"])
        before = self.reservations()
        self.assert_code("invalid_input", lambda: self.cleanup(predecessor))
        self.assertEqual(self.reservations(), before)

    def test_cleanup_chain_rejects_ancestors_with_another_target_or_resource_set(self):
        original = self.original()
        predecessor, _ = self.cleanup(original)
        predecessor = self.advance(predecessor)
        other, _ = self.store.admit(
            target_id="alias",
            request_id=str(uuid4()),
            tool="plan_execute",
            action="fleet_deploy",
            parameters={},
            plan=execution_plan("alias", "fleet_deploy"),
            resources=["other"],
        )
        predecessor = self.rewrite_selection(predecessor, other["operation_id"])
        self.assert_code("object_not_found", lambda: self.cleanup(predecessor))
        predecessor = self.rewrite_selection(predecessor, original["operation_id"])
        changed = copy.deepcopy(original)
        changed["resources"] = changed["resources"][:1]
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE operations SET document=? WHERE operation_id=?",
                (canonical(changed), original["operation_id"]),
            )
        self.assert_code("invalid_input", lambda: self.cleanup(predecessor))

    def test_cleanup_chain_requires_matching_immutable_request_and_plan(self):
        original = self.original()
        predecessor, _ = self.cleanup(original)
        predecessor = self.advance(predecessor)
        request = self.store.request("fleet", predecessor["operation_id"])
        request["action"] = "fleet_deploy"
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE operations SET request=? WHERE operation_id=?",
                (canonical(request), predecessor["operation_id"]),
            )
        self.assert_code("invalid_input", lambda: self.cleanup(predecessor))

    def test_cleanup_chain_limit_counts_all_predecessors_including_fleet_execution(self):
        predecessor = self.original()
        for _ in range(MAX_CLEANUP_PREDECESSORS):
            cleanup, created = self.cleanup(predecessor)
            self.assertTrue(created)
            predecessor = self.advance(cleanup)
        self.assert_code("invalid_input", lambda: self.cleanup(predecessor))
        self.assertEqual({row[1] for row in self.reservations()}, {predecessor["operation_id"]})
