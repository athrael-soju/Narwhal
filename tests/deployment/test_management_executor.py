"""Exercise durable execution with CPU helpers and interrupted effect receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment import stages
from narwhal.deployment.management_executor import (
    ReconcileOutcome,
    StageContext,
    StageOutcome,
    cancel_recovery_operation,
    reconcile_operation,
    run_operation,
    worker_identity,
)
from narwhal.deployment.management_records import OperationError, canonical, utc_now
from narwhal.deployment.management_registry import load_registry
from narwhal.deployment.management_store import OperationStore
from narwhal.deployment.management_worker import launch_worker


class CPUAdapter:
    def __init__(self, root, mode="normal"):
        self.root, self.mode = root, mode

    def execute_stage(self, context, stage, plan):
        if self.mode == "block":
            time.sleep(30)
        if self.mode == "unknown":
            context.record_intent(self.receipt(context, "unknown"))
            raise RuntimeError("private-error-marker")
        if self.mode == "unknown-success":
            context.record_intent(self.receipt(context, "unknown"))
            return StageOutcome()
        result = context.run_command(
            [
                sys.executable,
                "-c",
                "print('private-output-marker')",
            ]
        )
        return StageOutcome(data={"stdout": result.stdout})

    def receipt(self, context, effect):
        return {
            "resource_id": "cpu:effect",
            "kind": "private_artifact",
            "host_id": "local",
            "owner": {
                "operation_id": context.operation_id,
                "stage_id": "cpu",
                "launch_token": None,
            },
            "identity": {"path": str(self.root / "effect")},
            "effect": effect,
            "observed_at": utc_now(),
        }

    def reconcile(self, context, operation):
        receipts = [
            copy.deepcopy(item) for stage in operation["stages"] for item in stage["effects"]
        ]
        for receipt in receipts:
            receipt["effect"] = "confirmed" if (self.root / "effect").exists() else "absent"
        return ReconcileOutcome(helpers_stopped=True, effects=receipts)


_WORKER = """
import json, os, signal, sys, time
from pathlib import Path
from narwhal.deployment import stages
from narwhal.deployment.management_registry import load_registry
from narwhal.deployment.management_store import OperationStore
from narwhal.deployment.management_records import utc_now
from narwhal.deployment.management_executor import StageOutcome, run_operation

registry = load_registry(Path(sys.argv[1]))
operation_id, mode = sys.argv[2:]
root = Path(sys.argv[1]).parent
class Adapter:
    def execute_stage(self, context, stage, plan):
        if mode == "effect-gap":
            context.record_intent({
                "resource_id":"cpu:effect", "kind":"private_artifact", "host_id":"local",
                "owner":{"operation_id":operation_id,"stage_id":"cpu","launch_token":None},
                "identity":{"path":str(root / "effect")},"effect":"unknown","observed_at":utc_now(),
            })
            (root / "effect").write_text("one effect")
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "gate-gap":
            original = stages.run
            def gated(*args, **kwargs):
                prior = kwargs["before_start"]
                def interrupted(evidence):
                    prior(evidence)
                    os.kill(os.getpid(), signal.SIGKILL)
                kwargs["before_start"] = interrupted
                return original(*args, **kwargs)
            stages.run = gated
        context.run_command([sys.executable,"-c",
            "from pathlib import Path; import time; Path('started').write_text('yes'); "
            + ("time.sleep(30); " if mode == "cancel" else "time.sleep(.2); ")
            + "Path('done').write_text('yes')"], cwd=root)
        return StageOutcome(data={"cpu":True})
store = OperationStore(registry)
run_operation(registry, "cpu", operation_id, adapter=Adapter(),
    plan=store.plan("cpu", operation_id))
"""


class ManagementExecutorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "registry.json"
        self.path.write_text(
            json.dumps(
                {
                    "schema": "narwhal.management-registry",
                    "schema_version": 1,
                    "registry_id": str(uuid4()),
                    "state_dir": str(self.root / "state"),
                    "targets": [
                        {
                            "id": "cpu",
                            "kind": "dev",
                            "working_directory": str(self.root),
                            "artifact_root": str(self.root / "artifacts"),
                            "instance_dir": str(self.root / "instance"),
                            "fleet_file": None,
                            "adapter": {"id": "local-dev-v1", "settings_path": None},
                            "capabilities": ["inspect", "mutate", "measure"],
                            "actions": ["dev_up", "dev_down"],
                            "credential_env": ["CPU_SECRET"],
                        }
                    ],
                }
            )
        )
        self.path.chmod(0o600)
        self.registry = load_registry(self.path)
        self.store = OperationStore(self.registry)
        self.script = self.root / "worker.py"
        self.script.write_text(_WORKER)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        for path in (self.root / "artifacts").glob("operation-*/*.stage.json"):
            record = json.loads(path.read_text())
            owned = stages.active(record)
            if owned:
                stages._signal(owned, signal.SIGKILL)

    def admit(self, *, timeout=3000, count=1, cleanup=None):
        payload = {
            "target_id": "cpu",
            "action": "dev_up",
            "parameters": {},
            "binding": {},
            "stages": [
                {
                    "stage_id": "cpu" if index == 0 else f"cpu-{index}",
                    "operation": "cpu.run",
                    "depends_on": [],
                    "input_names": [],
                    "timeout_ms": timeout,
                    "cleanup": {
                        "policy": "temporary_only",
                        "term_grace_ms": 100 if cleanup is None else cleanup,
                        "kill_grace_ms": 100 if cleanup is None else cleanup,
                        "reconcile_ms": 500 if cleanup is None else cleanup,
                    },
                }
                for index in range(count)
            ],
        }
        self.plan = {
            "schema": "narwhal.deployment-plan",
            "schema_version": 1,
            "plan_id": str(uuid4()),
            "plan_digest": hashlib.sha256(canonical(payload)).hexdigest(),
            "created_at": utc_now(),
            "payload": payload,
        }
        operation, _ = self.store.admit(
            target_id="cpu",
            request_id=str(uuid4()),
            tool="plan_execute",
            action="dev_up",
            parameters={},
            plan=self.plan,
            resources=["cpu:exclusive"],
        )
        return operation["operation_id"]

    def test_success_transfers_only_confirmed_service_processes(self):
        operation_id = self.admit()
        self.plan["payload"]["stages"][0]["retain_on_success"] = ["router"]

        class Adapter:
            def execute_stage(adapter, context, stage, plan):
                def retain(evidence):
                    processes = {
                        str(pid): ticks
                        for pid, ticks in stages.active(evidence).items()
                        if pid != evidence["pid"]
                    }
                    return [
                        {
                            "resource_id": "dev:router",
                            "kind": "router",
                            "host_id": "management",
                            "owner": {
                                "operation_id": context.operation_id,
                                "stage_id": context.stage_id,
                                "launch_token": str(uuid4()),
                            },
                            "identity": {"boot_id": evidence["boot_id"], "processes": processes},
                            "effect": "confirmed",
                            "observed_at": utc_now(),
                        }
                    ]

                context.run_command(
                    [
                        sys.executable,
                        "-c",
                        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                        "'import time; time.sleep(30)'], start_new_session=True)",
                    ],
                    retain_on_success=retain,
                )
                return StageOutcome()

        result = run_operation(
            self.registry, "cpu", operation_id, adapter=Adapter(), plan=self.plan
        )
        self.assertEqual(result["state"], "succeeded", result)
        effects = {item["kind"]: item for item in result["stages"][0]["effects"]}
        helper, router = effects["measurement_helper"], effects["router"]
        self.assertEqual(helper["effect"], "absent")
        self.assertFalse(stages.active(helper["identity"]))
        self.assertTrue(stages.active(router["identity"]))

    def test_unrecorded_survivor_prevents_release_and_is_cleaned(self):
        operation_id = self.admit()
        self.plan["payload"]["stages"][0]["retain_on_success"] = ["router"]

        class Adapter:
            def execute_stage(adapter, context, stage, plan):
                context.run_command(
                    [
                        sys.executable,
                        "-c",
                        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                        "'import time; time.sleep(30)'], start_new_session=True)",
                    ],
                    retain_on_success=lambda evidence: [],
                )
                return StageOutcome()

        result = run_operation(
            self.registry, "cpu", operation_id, adapter=Adapter(), plan=self.plan
        )
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("ownership_conflict", json.dumps(result["result"]))
        self.assertFalse(stages.active(result["stages"][0]["effects"][0]["identity"]))

    def test_command_descriptor_reaches_helper_through_supervisor(self):
        descriptor = os.memfd_create("narwhal-test-context", os.MFD_CLOEXEC)
        self.addCleanup(os.close, descriptor)
        os.write(descriptor, b"authenticated-test-input")
        result = stages.run(
            [
                sys.executable,
                "-c",
                f"import os; print(os.pread({descriptor}, 100, 0).decode())",
            ],
            stage="fd",
            log=self.root / "fd.log",
            pass_fds=(descriptor,),
            before_start=lambda value: None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "authenticated-test-input")

    def test_inspection_reconciles_lost_worker_in_detached_process(self):
        from narwhal.deployment.management_coordinator import OperationCoordinator

        operation_id = self.admit()
        identity = worker_identity()
        identity["boot_id"] = str(uuid4())
        self.store.claim("cpu", operation_id, identity)
        coordinator = OperationCoordinator(self.registry, self.path)
        view = coordinator.inspect_operation("cpu", operation_id)
        self.assertEqual(view.data["operation"]["state"], "recovery_required")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = self.store.read("cpu", operation_id)
            if record["state"] == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(record["state"], "failed", record)
        self.assertEqual(record["result"]["errors"][0]["code"], "operation_interrupted")

    def test_degraded_and_interrupted_commands_keep_original_results(self):
        from narwhal import command_results, contracts

        for status, state, outer in (
            ("degraded", "failed", "failed_gate"),
            ("interrupted", "cancelled", "interrupted"),
        ):
            with self.subTest(status=status):
                operation_id = self.admit()
                command = contracts.versioned(
                    contracts.COMMAND_RESULT,
                    {
                        "command": "narwhal",
                        "operation": "dev verify",
                        "status": status,
                        "exit_code": command_results.EXIT_CODES[status],
                        "data": {"status": "degraded"},
                        "errors": [],
                        "artifacts": [],
                    },
                )

                class Adapter:
                    def execute_stage(
                        adapter, context, stage, plan, status=status, command=command
                    ):
                        return StageOutcome(status=status, command_result=command)

                result = run_operation(
                    self.registry, "cpu", operation_id, adapter=Adapter(), plan=self.plan
                )
                self.assertEqual(result["state"], state, result)
                self.assertEqual(result["result"]["status"], outer)
                self.assertEqual(result["result"]["command_result"], command)

    def test_retention_callback_obeys_action_deadline_and_cleans_helper(self):
        operation_id = self.admit(timeout=1000)
        entered = []

        class Adapter:
            def execute_stage(adapter, context, stage, plan):
                def retain(evidence):
                    entered.append(True)
                    time.sleep(30)
                    return []

                context.run_command([sys.executable, "-c", "pass"], retain_on_success=retain)
                return StageOutcome()

        started = time.monotonic()
        result = run_operation(
            self.registry, "cpu", operation_id, adapter=Adapter(), plan=self.plan
        )
        self.assertTrue(entered)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["result"]["errors"][0]["code"], "stage_timeout")
        self.assertFalse(stages.active(result["stages"][0]["effects"][0]["identity"]))

    def test_reconciled_preparation_cannot_succeed_without_retained_plan_result(self):
        record, _ = self.store.admit(
            target_id="cpu",
            request_id=str(uuid4()),
            tool="plan_prepare",
            action="dev_up",
            parameters={},
        )
        operation_id = record["operation_id"]
        identity = worker_identity()
        identity["boot_id"] = str(uuid4())
        record = self.store.claim("cpu", operation_id, identity)
        record["stages"][0].update(state="succeeded", started_at=utc_now(), finished_at=utc_now())
        self.store.commit(
            "cpu",
            operation_id,
            record,
            expected_revision=record["revision"],
            fence=record["worker"]["fence"],
        )

        class Adapter:
            def reconcile(adapter, context, operation):
                return ReconcileOutcome(helpers_stopped=True, complete=True)

        result = reconcile_operation(self.registry, "cpu", operation_id, adapter=Adapter())
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["errors"][0]["code"], "operation_interrupted")

    def worker(self, operation_id, mode):
        child = subprocess.Popen(
            [
                sys.executable,
                str(self.script),
                str(self.path),
                operation_id,
                mode,
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.children.append(child)
        return child

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("CPU fixture did not reach its expected state")

    def test_stage_execution_redacts_result_and_records_helper_before_start(self):
        operation_id = self.admit()
        with patch.dict(os.environ, {"CPU_SECRET": "private-output-marker"}):
            result = run_operation(
                self.registry, "cpu", operation_id, adapter=CPUAdapter(self.root), plan=self.plan
            )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["stages"][0]["effects"][0]["effect"], "absent")
        self.assertGreater(result["stages"][0]["effects"][0]["identity"]["start_ticks"], 0)
        self.assertNotEqual(
            result["stages"][0]["effects"][0]["owner"]["launch_token"], "[REDACTED]"
        )
        self.assertNotIn("private-output-marker", json.dumps(result))
        for path in (self.root / "artifacts").glob("operation-*/*"):
            if path.is_file():
                self.assertNotIn(b"private-output-marker", path.read_bytes(), path.name)
        self.assertIsNotNone(result["result"]["data"]["summary_artifact_id"])

    def test_cancellation_before_claim_does_not_invoke_adapter_or_start_clock(self):
        operation_id = self.admit()
        self.store.request_cancel("cpu", operation_id)
        adapter = CPUAdapter(self.root)
        with patch.object(adapter, "execute_stage", side_effect=AssertionError("must not execute")):
            result = run_operation(
                self.registry, "cpu", operation_id, adapter=adapter, plan=self.plan
            )
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNone(result["worker"])
        self.assertIsNone(result["started_at"])

    def test_success_cannot_clear_an_unknown_effect(self):
        operation_id = self.admit(count=2)
        result = run_operation(
            self.registry,
            "cpu",
            operation_id,
            adapter=CPUAdapter(self.root, "unknown-success"),
            plan=self.plan,
        )
        self.assertEqual(result["state"], "recovery_required")
        self.assertIsNone(result["result"])
        self.assertEqual(result["stages"][0]["state"], "running")
        self.assertEqual(result["stages"][1]["state"], "pending")

    def test_stale_preflight_returns_failed_gate_before_claim(self):
        operation_id = self.admit()

        def stale(context):
            raise OperationError("stale_plan", "Plan inputs changed")

        result = run_operation(
            self.registry,
            "cpu",
            operation_id,
            adapter=CPUAdapter(self.root),
            plan=self.plan,
            before_start=stale,
        )
        self.assertEqual(result["result"]["status"], "failed_gate")
        self.assertEqual(result["result"]["errors"][0]["code"], "stale_plan")
        self.assertIsNone(result["worker"])

    def test_multiple_stages_record_their_own_outcomes(self):
        operation_id = self.admit(count=2)
        result = run_operation(
            self.registry, "cpu", operation_id, adapter=CPUAdapter(self.root), plan=self.plan
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual([row["state"] for row in result["stages"]], ["succeeded", "succeeded"])
        self.assertTrue(all(len(row["effects"]) == 1 for row in result["stages"]))

    def test_callback_deadline_is_bounded_without_a_command(self):
        operation_id = self.admit(timeout=100)
        started = time.monotonic()
        result = run_operation(
            self.registry,
            "cpu",
            operation_id,
            adapter=CPUAdapter(self.root, "block"),
            plan=self.plan,
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["errors"][0]["code"], "stage_timeout")

    def test_bookkeeping_overrun_stops_the_next_stage_with_operation_timeout(self):
        operation_id = self.admit(timeout=100, count=2, cleanup=20)
        original = StageContext.update

        def delayed(context, change):
            result = original(context, change)
            if change.__name__ == "completed":
                time.sleep(0.35)
            return result

        adapter = CPUAdapter(self.root)
        with (
            patch.object(StageContext, "update", delayed),
            patch.object(
                adapter,
                "execute_stage",
                return_value=StageOutcome(),
            ) as execute,
        ):
            result = run_operation(
                self.registry, "cpu", operation_id, adapter=adapter, plan=self.plan
            )
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["errors"][0]["code"], "operation_timeout")
        self.assertEqual(result["stages"][1]["state"], "pending")

    def test_worker_finishes_after_its_submitting_process_exits(self):
        operation_id = self.admit()
        controller = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess,sys; subprocess.Popen([sys.executable,*sys.argv[1:]], "
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, "
                "close_fds=True,start_new_session=True)",
                str(self.script),
                str(self.path),
                operation_id,
                "normal",
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _, stderr = controller.communicate(timeout=5)
        self.assertEqual(controller.returncode, 0, stderr.decode())
        self.wait_for(lambda: self.store.read("cpu", operation_id)["state"] == "succeeded")
        self.assertEqual((self.root / "done").read_text(), "yes")

    def test_detached_worker_is_preserved_by_new_coordinator_and_cancels_owned_helper(self):
        operation_id = self.admit(timeout=10000)
        child = self.worker(operation_id, "cancel")
        self.wait_for(lambda: (self.root / "started").exists())
        before = self.store.read("cpu", operation_id)
        inspected = reconcile_operation(
            self.registry, "cpu", operation_id, adapter=CPUAdapter(self.root)
        )
        self.assertEqual(inspected["worker"]["worker_id"], before["worker"]["worker_id"])
        self.assertIsNone(inspected["recovery"]["reason"])
        self.assertEqual(inspected["state"], "running")
        self.store.request_cancel("cpu", operation_id)
        _, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr.decode())
        record = self.store.read("cpu", operation_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["stages"][0]["effects"][0]["effect"], "absent")
        self.assertFalse((self.root / "done").exists())

    def test_effect_receipt_gap_requires_reconciliation_without_repeating_effect(self):
        operation_id = self.admit()
        child = self.worker(operation_id, "effect-gap")
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        unknown = reconcile_operation(self.registry, "cpu", operation_id)
        self.assertEqual(unknown["state"], "recovery_required")
        self.assertIsNone(unknown["result"])
        with patch.object(CPUAdapter, "execute_stage", side_effect=AssertionError("cannot replay")):
            known = reconcile_operation(
                self.registry, "cpu", operation_id, adapter=CPUAdapter(self.root)
            )
        self.assertEqual(known["state"], "failed")
        self.assertEqual(known["stages"][0]["effects"][0]["effect"], "confirmed")
        self.assertEqual((self.root / "effect").read_text(), "one effect")

    def test_worker_crash_before_launch_gate_does_not_start_command(self):
        operation_id = self.admit()
        child = self.worker(operation_id, "gate-gap")
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        record = self.store.read("cpu", operation_id)
        identity = record["stages"][0]["effects"][0]["identity"]
        self.wait_for(lambda: not stages.active(identity))
        self.assertFalse((self.root / "started").exists())
        self.assertEqual(
            reconcile_operation(self.registry, "cpu", operation_id)["state"], "recovery_required"
        )

    def test_reconciliation_cannot_ignore_a_live_recorded_helper(self):
        operation_id = self.admit(timeout=10000)
        child = self.worker(operation_id, "cancel")
        self.wait_for(lambda: (self.root / "started").exists())
        child.kill()
        child.communicate(timeout=5)
        record = reconcile_operation(
            self.registry, "cpu", operation_id, adapter=CPUAdapter(self.root)
        )
        self.assertEqual(record["state"], "recovery_required")
        self.assertIsNone(record["result"])
        identity = record["stages"][0]["effects"][0]["identity"]
        self.assertTrue(stages.active(identity))

    def test_explicit_cancel_after_worker_loss_stops_only_owned_local_helpers(self):
        operation_id = self.admit(timeout=10000)
        child = self.worker(operation_id, "cancel")
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        self.children.append(unrelated)
        self.wait_for(lambda: (self.root / "started").exists())
        child.kill()
        child.communicate(timeout=5)
        before = self.store.read("cpu", operation_id)
        receipt = before["stages"][0]["effects"][0]
        self.assertTrue(stages.active(receipt["identity"]))
        unchanged = cancel_recovery_operation(self.registry, "cpu", operation_id)
        self.assertEqual(unchanged["revision"], before["revision"])
        self.store.request_cancel("cpu", operation_id)
        started = time.monotonic()
        result = cancel_recovery_operation(self.registry, "cpu", operation_id)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["stages"][0]["effects"][0]["effect"], "absent")
        self.assertFalse(stages.active(result["stages"][0]["effects"][0]["identity"]))
        self.assertEqual(
            result["stages"][0]["effects"][0]["owner"]["launch_token"],
            receipt["owner"]["launch_token"],
        )
        self.assertIsNone(unrelated.poll())
        self.assertFalse((self.root / "done").exists())

    def test_explicit_cancel_keeps_unknown_nonlocal_effects_reserved(self):
        operation_id = self.admit()
        child = self.worker(operation_id, "effect-gap")
        child.communicate(timeout=5)
        self.store.request_cancel("cpu", operation_id)
        result = cancel_recovery_operation(self.registry, "cpu", operation_id)
        self.assertEqual(result["state"], "recovery_required")
        self.assertEqual(result["stages"][0]["effects"][0]["effect"], "unknown")
        self.assertEqual((self.root / "effect").read_text(), "one effect")
        with self.assertRaises(OperationError) as error:
            self.admit()
        self.assertEqual(error.exception.code, "resource_busy")

    def test_concurrent_explicit_cancellation_has_one_cleanup_owner(self):
        operation_id = self.admit(timeout=10000)
        child = self.worker(operation_id, "cancel")
        self.wait_for(lambda: (self.root / "started").exists())
        child.kill()
        child.communicate(timeout=5)
        self.store.request_cancel("cpu", operation_id)
        entered, release = threading.Event(), threading.Event()
        original = stages._cleanup
        results = []
        errors = []

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Cleanup test was not released")
            return original(*args, **kwargs)

        def cancel():
            try:
                results.append(cancel_recovery_operation(self.registry, "cpu", operation_id))
            except Exception as error:
                errors.append(error)

        with patch.object(stages, "_cleanup", side_effect=blocked) as cleanup:
            thread = threading.Thread(target=cancel)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                pending = cancel_recovery_operation(self.registry, "cpu", operation_id)
                self.assertEqual(pending["state"], "recovery_required")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            self.assertEqual(results[0]["state"], "cancelled")
        self.assertEqual(cleanup.call_count, 1)

    def test_launcher_uses_fixed_package_code_and_closes_transport_handles(self):
        with patch("narwhal.deployment.management_worker.subprocess.Popen") as launch:
            launch_worker(self.path, "cpu", str(uuid4()))
        arguments, options = launch.call_args
        self.assertEqual(
            arguments[0][:3], [sys.executable, "-m", "narwhal.deployment.management_worker"]
        )
        self.assertTrue(options["start_new_session"])
        self.assertTrue(options["close_fds"])
        for descriptor in ("stdin", "stdout", "stderr"):
            self.assertEqual(options[descriptor], subprocess.DEVNULL)


class StageLaunchGateTests(unittest.TestCase):
    def test_managed_output_limit_cleans_helper_and_keeps_only_bounded_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(stages.StageOutputLimit) as error:
                stages.run(
                    [sys.executable, "-c", "print('x' * 100000)"],
                    stage="bounded",
                    log=root / "run.log",
                    redact=lambda value: value,
                    max_output_bytes=256,
                    timeout=1,
                    cleanup_grace=0.1,
                    kill_grace=0.1,
                )
            self.assertTrue(error.exception.context["output_truncated"])
            self.assertEqual(error.exception.context["cleanup"]["surviving_processes"], {})
            self.assertLessEqual(Path(error.exception.context["stdout"]).stat().st_size, 256)

    def test_failed_prelaunch_commit_cannot_start_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def reject(evidence):
                self.assertGreater(evidence["processes"][evidence["pid"]], 0)
                raise RuntimeError("commit rejected")

            with self.assertRaisesRegex(RuntimeError, "commit rejected"):
                stages.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('effect').touch()",
                    ],
                    stage="gated",
                    cwd=root,
                    log=root / "run.log",
                    before_start=reject,
                    timeout=1,
                    cleanup_grace=0.1,
                    kill_grace=0.1,
                )
            self.assertFalse((root / "effect").exists())
