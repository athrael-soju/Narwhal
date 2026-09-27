"""Inherited remote ownership must be resolved before cleanup releases reservations."""

import copy
import unittest
from uuid import uuid4

from narwhal.deployment.management_executor import (
    ReconcileOutcome,
    StageOutcome,
    cancel_queued_operation,
    reconcile_operation,
    run_operation,
)
from narwhal.deployment.management_records import OperationError, utc_now
from tests.deployment import test_management_cleanup as admission_fixture
from tests.deployment.test_management_store import terminal


class CompletionAdapter:
    def __init__(self, effects=()):
        self.effects = list(effects)
        self.executed = False
        self.read_only = None

    def execute_stage(self, context, stage, plan):
        self.executed = True
        return StageOutcome(effects=self.effects)

    def reconcile(self, context, operation):
        self.read_only = context.read_only
        return ReconcileOutcome(helpers_stopped=True, complete=True, effects=self.effects)


class CleanupCompletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = admission_fixture.CleanupAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.registry = self.fixture.registry
        original = self.fixture.original()
        token = str(uuid4())
        self.effect = {
            "resource_id": f"ssh:engine-one:{token}",
            "kind": "engine",
            "host_id": "engine-one",
            "owner": {
                "operation_id": original["operation_id"],
                "stage_id": "launch",
                "launch_token": token,
            },
            "identity": {"job_id": token},
            "effect": "confirmed",
            "observed_at": utc_now(),
        }
        changed = copy.deepcopy(original)
        changed["stages"][0]["effects"] = [self.effect]
        self.original = self.store.commit(
            "fleet", original["operation_id"], changed, original["revision"], None
        )
        self.cleanup, _ = self.fixture.cleanup(self.original)

    def assert_held(self, operation):
        self.assertEqual(operation["state"], "recovery_required")
        self.assertIsNone(operation["result"])
        self.assertEqual(
            [(resource, owner) for resource, owner, _ in self.fixture.reservations()],
            [(resource, operation["operation_id"]) for resource in admission_fixture.RESOURCES],
        )

    def run_cleanup(self, operation, adapter, **kwargs):
        return run_operation(
            self.registry,
            "fleet",
            operation["operation_id"],
            adapter=adapter,
            plan=self.store.plan("fleet", operation["operation_id"]),
            **kwargs,
        )

    def absent(self):
        return {**copy.deepcopy(self.effect), "effect": "absent", "observed_at": utc_now()}

    def test_queued_cancellation_retains_inherited_reservations(self):
        self.store.request_cancel("fleet", self.cleanup["operation_id"])
        result = cancel_queued_operation(self.registry, "fleet", self.cleanup["operation_id"])
        self.assert_held(result)
        self.assertIsNone(result["worker"])

    def test_cancelled_queue_entering_worker_retains_reservations(self):
        self.store.request_cancel("fleet", self.cleanup["operation_id"])
        adapter = CompletionAdapter()
        self.assert_held(self.run_cleanup(self.cleanup, adapter))
        self.assertFalse(adapter.executed)

    def test_before_start_failure_retains_inherited_reservations(self):
        def fail(context):
            raise OperationError("stale_plan", "Fixture input changed")

        adapter = CompletionAdapter()
        self.assert_held(self.run_cleanup(self.cleanup, adapter, before_start=fail))
        self.assertFalse(adapter.executed)

    def test_lost_worker_completion_claim_does_not_release_reservations(self):
        operation = self.fixture.advance(self.cleanup, "running")
        adapter = CompletionAdapter()
        result = reconcile_operation(
            self.registry, "fleet", operation["operation_id"], adapter=adapter
        )
        self.assertTrue(adapter.read_only)
        self.assert_held(result)

    def test_direct_terminal_commit_cannot_bypass_obligations(self):
        self.fixture.assert_code(
            "cleanup_incomplete",
            lambda: self.store.commit(
                "fleet",
                self.cleanup["operation_id"],
                terminal(self.cleanup),
                self.cleanup["revision"],
                None,
            ),
        )
        self.assertEqual(self.store.read("fleet", self.cleanup["operation_id"]), self.cleanup)
        self.assertEqual(
            {row[1] for row in self.fixture.reservations()}, {self.cleanup["operation_id"]}
        )

    def test_absence_requires_exact_resource_host_kind_owner_and_identity(self):
        changes = [
            {"resource_id": "ssh:unrelated"},
            {"host_id": "another-host"},
            {"kind": "router"},
            {"identity": None},
            *[
                {"owner": {**self.effect["owner"], field: value}}
                for field, value in (
                    ("operation_id", str(uuid4())),
                    ("stage_id", "another-stage"),
                    ("launch_token", str(uuid4())),
                )
            ],
        ]
        for change in changes:
            with self.subTest(change=change):
                candidate = terminal(self.cleanup)
                candidate["stages"][0]["effects"] = [{**self.absent(), **change}]
                self.fixture.assert_code(
                    "cleanup_incomplete",
                    lambda candidate=candidate: self.store.commit(
                        "fleet",
                        self.cleanup["operation_id"],
                        candidate,
                        self.cleanup["revision"],
                        None,
                    ),
                )
        self.assertEqual(self.store.read("fleet", self.cleanup["operation_id"]), self.cleanup)

    def test_verified_absence_on_retry_releases_inherited_reservations(self):
        predecessor = self.fixture.advance(self.cleanup)
        retry, _ = self.fixture.cleanup(predecessor)
        self.assert_held(self.fixture.advance(retry))
        final_retry, _ = self.fixture.cleanup(self.store.read("fleet", retry["operation_id"]))
        result = self.run_cleanup(final_retry, CompletionAdapter([self.absent()]))
        self.assertEqual(result["state"], "succeeded", result["recovery"])
        self.assertEqual(self.fixture.reservations(), [])
        self.assertEqual(self.store.read("fleet", predecessor["operation_id"]), predecessor)
        self.assertEqual(self.store.read("fleet", self.original["operation_id"]), self.original)

    def assert_retry_retains(self, receipt):
        predecessor = self.fixture.advance(self.cleanup)
        changed = copy.deepcopy(predecessor)
        changed["stages"][0]["effects"] = [receipt]
        predecessor = self.store.commit(
            "fleet",
            predecessor["operation_id"],
            changed,
            predecessor["revision"],
            None,
        )
        retry, _ = self.fixture.cleanup(predecessor)
        self.assert_held(self.run_cleanup(retry, CompletionAdapter()))

    def test_retry_cannot_treat_unidentified_absence_as_verified(self):
        self.assert_retry_retains({**self.absent(), "identity": None})

    def test_retry_cannot_use_another_owners_absence(self):
        receipt = self.absent()
        receipt["owner"]["launch_token"] = str(uuid4())
        self.assert_retry_retains(receipt)

    def test_retry_cannot_use_another_hosts_absence(self):
        self.assert_retry_retains({**self.absent(), "host_id": "another-host"})

    def test_admission_obligations_survive_later_predecessor_changes(self):
        changed = copy.deepcopy(self.original)
        changed["stages"][0]["effects"] = [self.absent()]
        self.store.commit(
            "fleet", changed["operation_id"], changed, self.original["revision"], None
        )
        self.assert_held(self.run_cleanup(self.cleanup, CompletionAdapter()))

    def test_legacy_cleanup_without_obligations_retains_reservations(self):
        with self.store.connection() as connection:
            connection.execute(
                "DELETE FROM cleanup_obligations WHERE operation_id=?",
                (self.cleanup["operation_id"],),
            )
        self.assert_held(self.run_cleanup(self.cleanup, CompletionAdapter([self.absent()])))


if __name__ == "__main__":
    unittest.main()
