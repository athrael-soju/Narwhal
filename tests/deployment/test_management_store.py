"""Exercise durable admission, exclusion, cancellation and snapshot retention."""

import copy
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment.management_records import OperationError, canonical, utc_now
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.deployment.management_store import OperationStore
from tests.deployment.test_management_registry import registry_document


def execution_plan(target="local-dev", action="dev_up"):
    payload = {
        "target_id": target,
        "action": action,
        "parameters": {},
        "binding": {"inputs": []},
        "stages": [
            {
                "stage_id": "launch",
                "gate": None,
                "operation": "fixture.launch",
                "depends_on": [],
                "subjects": [target],
                "input_names": [],
                "timeout_ms": 1000,
                "cleanup": {
                    "policy": "temporary_only",
                    "term_grace_ms": 200,
                    "kill_grace_ms": 200,
                    "reconcile_ms": 200,
                },
                "retain_on_success": [],
            }
        ],
    }
    return {
        "schema": "narwhal.deployment-plan",
        "schema_version": 1,
        "plan_id": str(uuid4()),
        "plan_digest": hashlib.sha256(canonical(payload)).hexdigest(),
        "created_at": utc_now(),
        "payload": payload,
    }


def identity():
    return {
        "worker_id": str(uuid4()),
        "host_id": "local",
        "boot_id": str(uuid4()),
        "pid": os.getpid(),
        "start_ticks": 42,
        "heartbeat_at": utc_now(),
    }


def terminal(record, state="failed"):
    result = copy.deepcopy(record)
    result.update(state=state, current_stage=None, finished_at=utc_now())
    if state == "succeeded":
        for stage in result["stages"]:
            stage.update(state="succeeded", started_at=utc_now(), finished_at=utc_now())
    data = (
        {"plan_id": str(uuid4())}
        if state == "succeeded" and record["tool"] == "plan_prepare"
        else {"summary_artifact_id": str(uuid4())}
    )
    result["result"] = {
        "status": {"succeeded": "success", "failed": "error", "cancelled": "interrupted"}[state],
        "data": data,
        "errors": [],
        "artifacts": [],
        "command_result": None,
    }
    return result


def concurrent_admit(document, request_id):
    store = OperationStore(ManagementRegistry.model_validate_json(json.dumps(document)))
    record, created = store.admit(
        target_id="local-dev",
        request_id=request_id,
        tool="plan_prepare",
        action="dev_up",
        parameters={},
    )
    return record["operation_id"], created


def interrupted_transaction(document):
    store = OperationStore(ManagementRegistry.model_validate_json(json.dumps(document)))
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO reservations VALUES (?, ?, ?)", ("uncommitted-gpu", str(uuid4()), 1)
        )
        os._exit(17)


class ManagementStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = registry_document()
        self.document["state_dir"] = str(self.root / "state")
        alias = copy.deepcopy(self.document["targets"][0])
        alias["id"] = "alias"
        self.document["targets"].append(alias)
        self.store = self.make_store()

    def make_store(self, document=None):
        return OperationStore(
            ManagementRegistry.model_validate_json(json.dumps(document or self.document))
        )

    def prepare(self, **kwargs):
        return self.store.admit(
            target_id="local-dev",
            request_id=kwargs.pop("request_id", str(uuid4())),
            tool="plan_prepare",
            action="dev_up",
            parameters=kwargs.pop("parameters", {}),
            **kwargs,
        )

    def execute(self, *, plan=None, target="local-dev", resources=(), **kwargs):
        return self.store.admit(
            target_id=target,
            request_id=kwargs.pop("request_id", str(uuid4())),
            tool=kwargs.pop("tool", "plan_execute"),
            action="dev_up",
            parameters={},
            plan=plan or execution_plan(target),
            resources=resources,
            **kwargs,
        )

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(OperationError) as error:
            function(*args, **kwargs)
        self.assertEqual(error.exception.code, code)

    def test_constructor_and_invalid_access_do_not_touch_storage(self):
        self.assertFalse((self.root / "state").exists())
        self.assert_code("target_not_found", self.store.read, "missing", str(uuid4()))
        self.assert_code("invalid_input", self.prepare, parameters={"unexpected": 1.0})
        self.assert_code("invalid_cursor", self.store.list, "local-dev", cursor="../../private")
        self.assertFalse((self.root / "state").exists())

    def test_processes_atomically_deduplicate_admission_and_restart_access(self):
        request_id = str(uuid4())
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            futures = [pool.submit(concurrent_admit, self.document, request_id) for _ in range(8)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(len({result[0] for result in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        operation_id = results[0][0]
        self.assertEqual(self.make_store().read("local-dev", operation_id)["state"], "queued")
        self.assertEqual(self.store.request("local-dev", operation_id)["parameters"], {})
        self.assertIsNone(self.store.plan("local-dev", operation_id))
        self.assert_code(
            "request_id_conflict", self.prepare, request_id=request_id, parameters={"changed": True}
        )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM operations").fetchone()[0], 1)
        for path in (self.root / "state").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_resource_admission_rolls_back_across_target_aliases(self):
        first, _ = self.execute(resources=["gpu:physical-uuid", "port:router:8000"])
        request_id = str(uuid4())
        plan = execution_plan("alias")
        self.assert_code(
            "resource_busy",
            self.execute,
            target="alias",
            plan=plan,
            request_id=request_id,
            resources=["new-resource", "gpu:physical-uuid"],
        )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests").fetchone()[0], 1)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM reservations WHERE resource_id='new-resource'"
                ).fetchone()
            )
        ended = terminal(first)
        self.store.commit("local-dev", first["operation_id"], ended, first["revision"], None)
        second, created = self.execute(
            target="alias", plan=plan, request_id=request_id, resources=["gpu:physical-uuid"]
        )
        self.assertTrue(created)
        self.assertNotEqual(second["operation_id"], first["operation_id"])

    def test_plan_consumption_and_tombstones_preserve_every_request(self):
        plan = execution_plan()
        first_id, second_id = str(uuid4()), str(uuid4())
        operation, _ = self.execute(plan=plan, request_id=first_id)
        repeated, created = self.execute(plan=plan, request_id=second_id)
        self.assertFalse(created)
        self.assertEqual(repeated["operation_id"], operation["operation_id"])
        self.assertEqual(self.store.plan("local-dev", operation["operation_id"]), plan)
        self.assert_code(
            "plan_scope_mismatch",
            self.execute,
            plan=plan,
            tool="operation_resume",
            parent_operation_id=str(uuid4()),
        )
        self.assert_code(
            "recovery_required", self.store.tombstone, "local-dev", operation["operation_id"]
        )
        self.store.commit(
            "local-dev", operation["operation_id"], terminal(operation), operation["revision"], None
        )
        self.store.tombstone("local-dev", operation["operation_id"])
        for request_id in (first_id, second_id, str(uuid4())):
            self.assert_code(
                "operation_record_removed", self.execute, plan=plan, request_id=request_id
            )
        self.assert_code(
            "operation_record_removed", self.store.read, "local-dev", operation["operation_id"]
        )

    def test_claim_cancellation_revision_and_fence_checks(self):
        operation, _ = self.execute(resources=["gpu:0"])
        operation_id = operation["operation_id"]
        running = self.store.claim("local-dev", operation_id, identity())
        fence = running["worker"]["fence"]
        self.assertEqual(running["state"], "running")
        self.assertIsNotNone(running["deadline_at"])
        self.assertEqual(running["resources"][0]["fence"], fence)
        self.assert_code("stale_worker", self.store.claim, "local-dev", operation_id, identity())
        changed = copy.deepcopy(running)
        changed["extension"] = {"retained": True}
        self.assert_code(
            "stale_worker",
            self.store.commit,
            "local-dev",
            operation_id,
            changed,
            running["revision"],
            fence + 1,
        )
        cancelling = self.store.request_cancel("local-dev", operation_id)
        self.assertEqual(cancelling["state"], "cancelling")
        self.assertEqual(self.store.request_cancel("local-dev", operation_id), cancelling)
        self.assert_code(
            "stale_revision",
            self.store.commit,
            "local-dev",
            operation_id,
            changed,
            running["revision"],
            fence,
        )
        finished = terminal(cancelling, "cancelled")
        finished["extension"] = {"retained": True}
        committed = self.store.commit(
            "local-dev", operation_id, finished, cancelling["revision"], fence
        )
        self.assertEqual(committed["extension"], {"retained": True})
        self.assertEqual(self.store.request_cancel("local-dev", operation_id), committed)
        self.assert_code(
            "invalid_input",
            self.store.commit,
            "local-dev",
            operation_id,
            committed,
            committed["revision"],
            fence,
        )

    def test_recovery_keeps_reservations_and_reconciled_resume_creates_child(self):
        operation, _ = self.execute(resources=["gpu:0"])
        operation_id = operation["operation_id"]
        running = self.store.claim("local-dev", operation_id, identity())
        recovery = copy.deepcopy(running)
        recovery["state"] = "recovery_required"
        recovery["recovery"].update(reason="worker_lost", observed_at=utc_now())
        recovered = self.store.commit(
            "local-dev", operation_id, recovery, running["revision"], None
        )
        self.assert_code("resource_busy", self.execute, target="alias", resources=["gpu:0"])
        self.assert_code(
            "recovery_required",
            self.execute,
            tool="operation_resume",
            parent_operation_id=operation_id,
        )
        ended = self.store.commit(
            "local-dev", operation_id, terminal(recovered), recovered["revision"], None
        )
        child, created = self.execute(
            tool="operation_resume", parent_operation_id=operation_id, resources=["gpu:0"]
        )
        self.assertTrue(created)
        self.assertEqual(child["parent_operation_id"], operation_id)
        self.assertEqual(self.store.read("local-dev", operation_id), ended)

    def test_listing_freezes_order_values_and_survives_removed_records(self):
        timestamp = "2026-09-27T12:00:00Z"
        with patch("narwhal.deployment.management_records.utc_now", return_value=timestamp):
            records = [self.prepare()[0] for _ in range(4)]
        ordered = sorted(records, key=lambda record: record["operation_id"])
        first = self.store.list("local-dev", limit=1)
        self.assertEqual(first["operations"][0]["operation_id"], ordered[0]["operation_id"])
        token = first["next_cursor"]
        second = self.store.list("local-dev", limit=1, cursor=token)
        self.store.request_cancel("local-dev", ordered[1]["operation_id"])
        record = self.store.read("local-dev", ordered[2]["operation_id"])
        self.store.commit(
            "local-dev",
            record["operation_id"],
            terminal(record, "cancelled"),
            record["revision"],
            None,
        )
        self.store.tombstone("local-dev", record["operation_id"])
        self.prepare()
        self.assertEqual(self.make_store().list("local-dev", limit=1, cursor=token), second)
        seen = first["operations"][:]
        while token:
            page = self.store.list("local-dev", limit=1, cursor=token)
            seen.extend(page["operations"])
            token = page["next_cursor"]
        self.assertEqual(
            [row["operation_id"] for row in seen], [row["operation_id"] for row in ordered]
        )
        self.assertTrue(all(row["state"] == "queued" for row in seen))
        self.assert_code(
            "invalid_cursor", self.store.list, "alias", limit=1, cursor=first["next_cursor"]
        )
        self.assert_code(
            "invalid_cursor", self.store.list, "local-dev", limit=2, cursor=first["next_cursor"]
        )

    def test_revoked_grants_prevent_admission_read_or_cancellation(self):
        preparation, _ = self.prepare()
        execution, _ = self.execute()
        document = copy.deepcopy(self.document)
        document["targets"][0].update(capabilities=["inspect"], actions=[])
        store = self.make_store(document)
        self.assert_code(
            "permission_denied",
            store.admit,
            target_id="local-dev",
            request_id=str(uuid4()),
            tool="plan_prepare",
            action="dev_up",
            parameters={},
        )
        self.assert_code(
            "permission_denied", store.request_cancel, "local-dev", execution["operation_id"]
        )
        self.assertIsNotNone(
            store.request_cancel("local-dev", preparation["operation_id"])["cancellation"][
                "requested_at"
            ]
        )
        document["targets"][0]["capabilities"] = []
        self.assert_code(
            "permission_denied",
            self.make_store(document).read,
            "local-dev",
            preparation["operation_id"],
        )

    def test_state_authority_and_storage_links_are_rejected(self):
        record, _ = self.prepare()
        document = copy.deepcopy(self.document)
        document["registry_id"] = str(uuid4())
        self.assert_code(
            "permission_denied", self.make_store(document).read, "local-dev", record["operation_id"]
        )
        root = self.root / "state"
        victim = self.root / "victim"
        victim.write_text("private marker")
        for name in (
            "operations.sqlite3-journal",
            "operations.sqlite3-wal",
            "operations.sqlite3-shm",
        ):
            path = root / name
            path.symlink_to(victim)
            self.assert_code(
                "permission_denied", self.store.read, "local-dev", record["operation_id"]
            )
            self.assertEqual(victim.read_text(), "private marker")
            path.unlink()
        database = root / "operations.sqlite3"
        database.rename(root / "saved")
        database.symlink_to(victim)
        self.assert_code("permission_denied", self.store.read, "local-dev", record["operation_id"])
        self.assertEqual(victim.read_text(), "private marker")

    def test_transaction_failure_does_not_leave_request_or_reservations(self):
        with (
            patch(
                "narwhal.deployment.management_store.encode_record",
                side_effect=RuntimeError("interrupted"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.execute(resources=["gpu:0"])
        with self.store.connection() as connection:
            for name in ("operations", "requests", "consumed_plans", "reservations"):
                self.assertEqual(
                    connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0], 0
                )

    def test_abrupt_process_exit_rolls_back_an_uncommitted_reservation(self):
        self.prepare()
        process = multiprocessing.get_context("fork").Process(
            target=interrupted_transaction, args=(self.document,)
        )
        process.start()
        process.join(timeout=5)
        self.assertEqual(process.exitcode, 17)
        with self.make_store().connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM reservations WHERE resource_id='uncommitted-gpu'"
                ).fetchone()
            )
        self.execute(resources=["uncommitted-gpu"])

    def test_lookup_access_survives_terminal_state_and_rejects_removed_records(self):
        request_id = str(uuid4())
        plan = execution_plan()
        record, _ = self.execute(plan=plan, request_id=request_id)
        self.assertEqual(
            self.store.lookup_request("local-dev", request_id),
            {
                **self.store.request("local-dev", record["operation_id"]),
                "operation_id": record["operation_id"],
            },
        )
        self.assertEqual(
            self.store.lookup_plan("local-dev", plan["plan_id"]),
            self.store.lookup_request("local-dev", request_id),
        )
        self.assertIsNone(self.store.lookup_request("local-dev", str(uuid4())))
        self.assertIsNone(self.store.lookup_plan("alias", plan["plan_id"]))
        self.store.commit(
            "local-dev", record["operation_id"], terminal(record), record["revision"], None
        )
        self.assertIsNotNone(self.store.lookup_request("local-dev", request_id))
        self.store.tombstone("local-dev", record["operation_id"])
        self.assert_code(
            "operation_record_removed", self.store.lookup_request, "local-dev", request_id
        )
        self.assert_code(
            "operation_record_removed", self.store.lookup_plan, "local-dev", plan["plan_id"]
        )
        revoked = copy.deepcopy(self.document)
        revoked["targets"][0].update(capabilities=["inspect"], actions=[])
        restricted = self.make_store(revoked)
        self.assert_code("permission_denied", restricted.lookup_request, "local-dev", request_id)
        self.assert_code("permission_denied", restricted.lookup_plan, "local-dev", plan["plan_id"])

    def test_commits_reject_identity_changes_and_unfenced_worker_writes(self):
        record, _ = self.execute()
        record = self.store.claim("local-dev", record["operation_id"], identity())
        for field, value in (("plan_id", str(uuid4())), ("resources", []), ("deadline_at", None)):
            altered = copy.deepcopy(record)
            altered[field] = value
            if altered == record:
                continue
            self.assert_code(
                "invalid_input",
                self.store.commit,
                "local-dev",
                record["operation_id"],
                altered,
                record["revision"],
                record["worker"]["fence"],
            )
        self.assert_code(
            "stale_worker",
            self.store.commit,
            "local-dev",
            record["operation_id"],
            record,
            record["revision"],
            None,
        )

    def test_page_byte_limit_and_registration_changes(self):
        for _ in range(5):
            self.prepare()
        with patch("narwhal.deployment.management_store.MAX_PAGE_BYTES", 1300):
            page = self.store.list("local-dev")
        self.assertLess(len(page["operations"]), 5)
        self.assertLessEqual(len(json.dumps(page).encode()), 1300)
        document = copy.deepcopy(self.document)
        document["targets"][0]["working_directory"] = "/changed"
        self.assert_code(
            "invalid_cursor",
            self.make_store(document).list,
            "local-dev",
            cursor=page["next_cursor"],
        )

    def test_unsafe_regular_modes_hardlinks_and_fifo_fail_without_blocking(self):
        record, _ = self.prepare()
        database = self.root / "state" / "operations.sqlite3"
        database.chmod(0o644)
        self.assert_code("permission_denied", self.store.read, "local-dev", record["operation_id"])
        database.chmod(0o600)
        os.link(database, self.root / "hardlink")
        self.assert_code("permission_denied", self.store.read, "local-dev", record["operation_id"])
        (self.root / "hardlink").unlink()
        database.rename(database.with_suffix(".saved"))
        os.mkfifo(database, 0o600)
        self.assert_code("permission_denied", self.store.read, "local-dev", record["operation_id"])

    def test_registry_removal_checks_persisted_work_even_after_frontend_restart(self):
        record, _ = self.prepare()
        revoked = copy.deepcopy(self.document)
        revoked["targets"][0].update(capabilities=[], actions=[])
        self.store.check_registry(ManagementRegistry.model_validate_json(json.dumps(revoked)))
        removed = copy.deepcopy(self.document)
        removed["targets"] = [
            target for target in removed["targets"] if target["id"] != "local-dev"
        ]
        registry = ManagementRegistry.model_validate_json(json.dumps(removed))
        restarted = OperationStore(registry)
        self.assert_code("resource_busy", self.store.check_registry, registry)
        self.assert_code("resource_busy", restarted.check_registry, registry)
        self.store.commit(
            "local-dev", record["operation_id"], terminal(record), record["revision"], None
        )
        restarted.check_registry(registry)
