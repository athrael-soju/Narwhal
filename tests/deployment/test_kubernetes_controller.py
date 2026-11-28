"""The fleet controller against the stub object store: its Lease, its status, edits during a
pass, stop and delete, adoption after a restart, and every write replayed after a lost reply."""

import base64
import contextlib
import copy
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import fleet_resource, kubernetes_controller, kubernetes_target
from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.kubernetes_controller import (
    VALIDATING_ADMISSION_POLICY,
    VALIDATING_ADMISSION_POLICY_BINDING,
    Controller,
    Settings,
)
from narwhal.runtime.kubernetes_api import ApiClient, Connection
from narwhal.runtime.kubernetes_lease import KubernetesLease
from narwhal.serving.control_token import ROUTER_TOKEN_ENV
from tests.deployment import chart_live
from tests.deployment.kubernetes_cluster import (
    CONFIG_MAP,
    CRD,
    DEPLOYMENT,
    JOB,
    LEASE,
    NAMESPACE,
    PERSISTENT_VOLUME_CLAIM,
    POD,
    SERVER,
    SERVICE,
    STATEFUL_SET,
    ApiError,
    Kind,
    Rig,
    entry_of,
)

POLICY = f"{NAMESPACE}-narwhal"
# Test Leases last an hour, so a pass on the rig's clock never outlasts one; the controller's
# own timings are client-go's.
TTL = 3600
DERIVED = (JOB, CONFIG_MAP, SERVICE, STATEFUL_SET, DEPLOYMENT)
WRITES = ("POST", "PUT", "PATCH", "DELETE")
# The gate Jobs one pass starts for the rig's fleet, in order.
JOBS = [
    "key-check",
    *("discovery", "fabric-probe") * 2,
    *("prepare",) * 2,
    *("engine-check",) * 2,
    *("state",) * 4,
    *("sidecar-verify",) * 2,
    "finalize-fleet",
    "profile",
    "calibrate",
    "state",
    "preflight",
    "router-check",
]


class Crash(BaseException):
    """The controller's process ends, as a lost Lease's `os._exit` or a killed pod leaves it."""


class Exited(Exception):
    """`os._exit`, as a test's controller calls it."""


def store_kind(kind: kubernetes_target.Kind) -> Kind:
    return Kind(kind.group, kind.version, kind.kind, kind.plural, namespaced=kind.namespaced)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.rig = Rig(self.root)
        self.store = self.rig.store
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.rig.patched())
        self.fleet_kind = self.store.kind_of(self.rig.fleet)
        self.store.define(store_kind(VALIDATING_ADMISSION_POLICY))
        self.store.define(store_kind(VALIDATING_ADMISSION_POLICY_BINDING))
        self.install_policy()
        self.settings = Settings.of({**self.rig.settings, "admissionPolicy": POLICY})
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.exits: list[tuple[str, int]] = []

    def install_policy(self) -> None:
        for kind, spec in (
            (VALIDATING_ADMISSION_POLICY, {"failurePolicy": "Fail"}),
            (
                VALIDATING_ADMISSION_POLICY_BINDING,
                {"policyName": POLICY, "validationActions": ["Deny"]},
            ),
        ):
            self.store.create(
                {
                    "apiVersion": kind.api_version,
                    "kind": kind.kind,
                    "metadata": {"name": POLICY},
                    "spec": spec,
                },
                field_manager="helm",
            )

    # Controllers.

    def client(self) -> ApiClient:
        return ApiClient(
            Connection(SERVER, NAMESPACE),
            field_manager=objects.MANAGER,
            transport=self.rig.transport,
            sleep=lambda seconds: None,
        )

    def controller(self, holder: str = "controller-a", *, ttl: int = TTL) -> Controller:
        client = self.client()
        lease = KubernetesLease(
            client,
            fleet_resource.CONTROLLER_LEASE,
            holder,
            ttl,
            safety_margin_s=5,
            wall_clock=self.store.clock,
            monotonic_clock=self.store.clock,
        )

        def exit(code: int) -> None:
            self.exits.append((holder, code))
            raise Exited(holder)

        def fenced(timeout_s: float) -> ApiClient:
            return kubernetes_controller.fenced_client(
                Connection(SERVER, NAMESPACE),
                lease,
                timeout_s=timeout_s,
                lost=lambda: self.exits.append((holder, "write")),
                transport=self.rig.transport,
                sleep=lambda seconds: None,
            )

        return Controller(
            fenced(10.0),
            writer=fenced(kubernetes_controller.WRITE_TIMEOUT_SECONDS),
            settings=self.settings,
            lease=lease,
            state=self.state,
            clock=self.store.clock,
            monotonic=self.store.clock,
            exit=exit,
        )

    def leader(self, holder: str = "controller-a", *, ttl: int = TTL) -> Controller:
        """A controller that created the Lease and waited out a lease duration."""
        controller = self.controller(holder, ttl=ttl)
        self.assertFalse(controller.elect())
        # A created Lease may hide a holder this controller never saw, so it serves a lease
        # duration later, renewed meanwhile.
        for _ in range(2):
            self.store.clock.advance(ttl / 2)
            controller.elect()
        self.assertTrue(controller.elect())
        return controller

    def work(self, controller: Controller) -> int:
        """Renew, follow the watch and reconcile until the queue is empty; return how many
        reconciles ran."""
        for count in range(20):
            controller.elect()
            controller.pump()
            if not controller.work_once():
                return count
        raise AssertionError("the queue never emptied")

    # The cluster.

    def fleet(self) -> dict:
        return self.store.get(self.fleet_kind, NAMESPACE, fleet_resource.NAME)

    def status(self) -> dict:
        return self.fleet().get("status") or {}

    def condition(self, kind: str) -> dict:
        found = {c["type"]: c for c in self.status().get("conditions") or []}
        return found.get(kind) or {}

    def edit(self, spec: dict) -> None:
        """Change the fleet's spec as Helm would."""
        self.store.patch(
            self.fleet_kind,
            NAMESPACE,
            fleet_resource.NAME,
            {"spec": spec},
            patch_type="merge",
            field_manager="helm",
        )

    def annotate(self, annotations: dict) -> None:
        self.store.patch(
            self.fleet_kind,
            NAMESPACE,
            fleet_resource.NAME,
            {"metadata": {"annotations": annotations}},
            patch_type="merge",
            field_manager="kubectl",
        )

    def directory(self) -> Path:
        uid = self.fleet()["metadata"]["uid"]
        return self.state / "runs" / NAMESPACE / uid

    def record(self) -> dict:
        return json.loads((self.directory() / "run.json").read_text())

    def derived(self) -> list[dict]:
        owner = objects.Owner.of(self.rig.fleet)
        found = [obj for kind in DERIVED for obj in self.store.live(kind, NAMESPACE)]
        return [obj for obj in found if objects.controlled(owner, obj)]

    def writes(self, since: int = 0) -> list[tuple[str, str, str]]:
        return [
            r for r in self.rig.requests[since:] if r[0] in ("create", "apply", "patch", "delete")
        ]

    def ready(self, controller: Controller | None = None) -> Controller:
        controller = controller or self.leader()
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.READY).get("status"), "True", self.status())
        return controller

    def held(self, kind: str) -> bool:
        return any(
            job["metadata"]["labels"][fleet_resource.GATE_LABEL] == kind
            and (job.get("status") or {}).get("active")
            for job in self.rig.live_jobs()
        )

    def left_behind(self, data: bytes) -> None:
        """Write back the record a killed controller left, whose lock names a process that
        has ended."""
        record = json.loads(data)
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        record["lock"]["owner"]["pid"] = ended.pid
        (self.directory() / "run.json").write_text(json.dumps(record))


class ReconcileTests(ControllerTest):
    def test_a_fleet_reaches_ready_and_serving_and_a_resync_changes_nothing(self):
        controller = self.ready()
        status = self.status()
        self.assertEqual(self.rig.kinds, JOBS)
        self.assertTrue(status["serving"])
        self.assertEqual(status["engines"], "2/2")
        self.assertEqual(status["stage"], "router-start")
        self.assertEqual(status["observedGeneration"], self.fleet()["metadata"]["generation"])
        for kind in (fleet_resource.RECONCILING, fleet_resource.STALLED, fleet_resource.STOPPED):
            self.assertEqual(self.condition(kind)["status"], "False", kind)
        profile = self.condition(fleet_resource.stage_condition("profile"))
        self.assertEqual((profile["status"], profile["reason"]), ("True", "Passed"))
        self.assertEqual(profile["message"], "no attempt yet")
        self.assertEqual(status["stages"]["engine-start"]["passed"], 2)
        self.assertEqual(status["stages"]["engine-start"]["total"], 2)
        self.assertIn("lastPassedTime", status["stages"]["router-start"])
        # The run directory is the fleet's, by uid, and private.
        self.assertEqual(oct(self.directory().stat().st_mode & 0o777), "0o700")
        # A resync runs a pass that starts no Job and writes nothing.
        created = len(self.rig.created)
        since = len(self.rig.requests)
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        self.assertTrue(controller.informer.resync())
        self.assertEqual(self.work(controller), 1)
        self.assertEqual(self.rig.created[created:], [])
        self.assertEqual([w for w in self.writes(since) if w[2] != "leases"], [])

    def test_a_failed_stage_stalls_the_fleet_until_an_edit_or_a_new_request(self):
        self.rig.fail_kind = "preflight"
        controller = self.leader()
        self.work(controller)
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "StageFailed"))
        self.assertIn("preflight failed", stalled["message"])
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "False")
        self.assertEqual(self.condition(fleet_resource.RECONCILING)["status"], "False")
        preflight = self.condition(fleet_resource.stage_condition("preflight"))
        self.assertEqual((preflight["status"], preflight["reason"]), ("False", "Failed"))
        self.assertIn("the check rejected the engine", preflight["message"])
        self.assertEqual(self.status()["stage"], "preflight")
        # A resync, or a restarted controller, runs nothing for the same generation.
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        self.assertTrue(controller.informer.resync())
        jobs = len(self.rig.kinds)
        self.assertEqual(self.work(controller), 1)
        self.assertEqual(self.rig.kinds[jobs:], [])
        # A new requested-at value runs the failed stage again.
        self.annotate({fleet_resource.REQUESTED_AT: "2026-11-28T10:00:00Z"})
        self.work(controller)
        self.assertEqual(self.rig.kinds.count("preflight"), 2)
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "True")
        # So does an edit, which here also fixes the cause.
        self.rig.fail_kind = None
        self.edit({"slo": {"ttftS": "5"}})
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True", self.status())
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "False")
        self.assertFalse((self.directory() / kubernetes_controller.STALL_FILE).exists())

    def test_a_status_write_queues_no_reconcile_and_status_is_written_only_on_change(self):
        controller = self.leader()
        # The pass writes status as it goes; those writes come back as watch events, and none
        # queues another pass.
        self.assertEqual(self.work(controller), 1)
        self.assertEqual(self.rig.kinds, JOBS)
        since = len(self.rig.requests)
        controller.pump()
        self.assertFalse(controller.work_once())
        self.assertEqual(self.writes(since), [])

    def test_a_status_write_whose_reply_is_lost_is_written_again(self):
        controller = self.ready()
        crd = self.store.get(CRD, None, fleet_resource.CRD_NAME)
        crd["spec"]["versions"][0]["additionalPrinterColumns"] = []
        self.store.put(crd)
        # The write never lands, and its reply is lost.
        self.store.fail("patch", self.fleet_kind, name=fleet_resource.NAME, code=504)
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.STALLED)["reason"], "SchemaStale")

    def test_a_status_write_never_lands_on_a_fleet_made_again_under_the_name(self):
        controller = self.ready()
        old = controller.informer.get(f"{NAMESPACE}/{fleet_resource.NAME}")
        # The fleet is deleted and made again before the informer hears of either.
        self.store.delete(self.fleet_kind, NAMESPACE, fleet_resource.NAME)
        fresh = copy.deepcopy(self.rig.fleet)
        for field in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields"):
            fresh["metadata"].pop(field, None)
        fresh.pop("status", None)
        made = self.store.create(fresh, field_manager="helm")
        controller._publish(old, kubernetes_controller._Outcome(stalled=("StageFailed", "old")))
        self.assertEqual(self.fleet()["metadata"]["uid"], made["metadata"]["uid"])
        self.assertNotIn("status", self.fleet())

    def test_old_run_directories_are_pruned_to_retain_runs_and_named_in_status(self):
        parent = self.state / "runs" / NAMESPACE
        parent.mkdir(parents=True, mode=0o700)
        old = [f"0000000{n}-0000-4000-8000-000000000000" for n in range(4)]
        for age, uid in enumerate(old):
            (parent / uid).mkdir(mode=0o700)
            os.utime(parent / uid, (1000 + age, 1000 + age))
        (parent / "not-a-run").mkdir()
        self.edit({"retainRuns": 2})
        self.ready()
        self.assertEqual(self.status()["previousRuns"], [old[3]])
        kept = sorted(path.name for path in parent.iterdir())
        self.assertEqual(kept, sorted([old[3], self.fleet()["metadata"]["uid"], "not-a-run"]))

    def test_the_controller_uses_only_the_verbs_the_charts_rules_grant(self):
        controller = self.ready()
        self.edit({"stopped": True})
        self.work(controller)
        self.edit({"stopped": False})
        self.work(controller)
        granted = {
            (verb, group, resource)
            for group, resource, verb, _ in (
                *chart_live.CONTROLLER_RULES,
                *chart_live.cluster_rules(NAMESPACE),
            )
        }
        used = set()
        for verb, group, resource in self.rig.requests:
            if (verb, group, resource) == ("patch", "coordination.k8s.io", "leases"):
                verbs: tuple[str, ...] = ("update",)  # the rig names a PUT patch
            else:
                verbs = kubernetes_target.VERBS[verb]
            used |= {(needed, group, resource) for needed in verbs}
        self.assertLessEqual(used, granted)
        resources = {resource for _, _, resource in self.rig.requests}
        self.assertNotIn("secrets", resources)
        self.assertFalse({"roles", "rolebindings", "pods/exec"} & resources)


class EditTests(ControllerTest):
    def test_an_edit_that_reruns_the_running_stage_cancels_it(self):
        controller = self.leader()
        self.rig.hold = {"profile"}
        profiles: list[str] = []

        def edit() -> None:
            running = [
                job["metadata"]["name"]
                for job in self.rig.live_jobs()
                if job["metadata"]["labels"][fleet_resource.GATE_LABEL] == "profile"
                and (job.get("status") or {}).get("active")
            ]
            for name in running:
                if name not in profiles:
                    profiles.append(name)
                    if len(profiles) == 1:
                        # The held Job never ends by itself: only a cancel ends its attempt.
                        self.edit({"profiling": {"warmupRequests": 9}})
                        controller.pump()
                    else:
                        self.rig.released.add(name)

        self.rig.on_tick = edit
        self.work(controller)
        self.rig.on_tick = None
        self.assertEqual(len(profiles), 2)
        entry = self.record()["stages"]["profile"]
        self.assertEqual((entry["status"], entry["attempt"]), ("passed", 2))
        deletes = [d for d in self.store.deletes if d.kind == "Job" and d.name == profiles[0]]
        self.assertTrue(deletes)
        self.assertTrue(all(d.propagation == "Background" for d in deletes))
        self.assertEqual(self.status()["observedGeneration"], 2)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")

    def test_an_edit_that_does_not_rerun_the_running_stage_waits_for_its_instance(self):
        controller = self.leader()
        self.rig.hold = {"profile"}
        seen: dict[str, list[str]] = {}

        def edit() -> None:
            if self.held("profile") and not seen:
                seen["before"] = list(self.rig.kinds)
                self.edit({"slo": {"ttftS": "5"}})
                self.rig.hold = set()
                controller.pump()

        self.rig.on_tick = edit
        controller.elect()
        controller.pump()
        self.assertTrue(controller.work_once())
        self.rig.on_tick = None
        # The first pass let profile finish and stopped before calibrate.
        record = self.record()
        self.assertEqual(record["stages"]["profile"]["status"], "passed")
        self.assertNotIn("calibrate", record["stages"])
        self.assertEqual(self.condition(fleet_resource.RECONCILING)["status"], "True")
        self.work(controller)
        self.assertEqual(self.rig.kinds.count("profile"), 1)
        self.assertEqual(self.record()["stages"]["profile"]["attempt"], 1)
        self.assertEqual(self.status()["observedGeneration"], 2)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")

    def test_an_edit_just_before_the_pass_starts_still_stops_it(self):
        controller = self.leader()
        checks = controller._blocked
        edited = []

        def blocked(fleet):
            # The edit lands, and the informer hears it, after the reconcile read the fleet.
            if not edited:
                edited.append(True)
                self.edit({"profiling": {"warmupRequests": 9}})
                controller.pump()
            return checks(fleet)

        with patch.object(controller, "_blocked", blocked):
            self.assertEqual(self.work(controller), 2)
        self.assertEqual(self.rig.kinds.count("profile"), 1)
        self.assertEqual(self.record()["stages"]["profile"]["attempt"], 1)
        self.assertEqual(self.status()["observedGeneration"], 2)

    def test_an_edit_whose_interrupt_fails_is_heard_again(self):
        controller = self.leader()
        self.rig.hold = {"profile"}
        profiles: list[str] = []
        interrupt = controller._interrupt
        failures = []

        def failing(running, fleet):
            if not failures:
                failures.append(True)
                raise RuntimeError("the interrupt failed")
            interrupt(running, fleet)

        def edit() -> None:
            for job in self.rig.live_jobs():
                name = job["metadata"]["name"]
                if job["metadata"]["labels"][fleet_resource.GATE_LABEL] != "profile":
                    continue
                if not (job.get("status") or {}).get("active") or name in profiles:
                    continue
                profiles.append(name)
                if len(profiles) == 1:
                    self.edit({"profiling": {"warmupRequests": 9}})
                    with (
                        patch.object(controller, "_interrupt", failing),
                        self.assertRaises(RuntimeError),
                    ):
                        controller.pump()
                    # The informer hears the same event again on its next watch.
                    controller.pump()
                else:
                    self.rig.released.add(name)

        self.rig.on_tick = edit
        self.work(controller)
        self.rig.on_tick = None
        self.assertEqual(len(profiles), 2)
        self.assertEqual(self.record()["stages"]["profile"]["attempt"], 2)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")

    def test_a_relist_that_finds_the_fleet_made_again_ends_the_old_pass(self):
        controller = self.leader()
        self.rig.hold = {"profile"}
        old_uid = self.fleet()["metadata"]["uid"]
        old = self.directory()
        made: list[dict] = []

        def recreate() -> None:
            if self.held("profile") and not made:
                self.store.delete(
                    self.fleet_kind, NAMESPACE, fleet_resource.NAME, propagation_policy="Background"
                )
                fresh = copy.deepcopy(self.rig.fleet)
                for field in ("uid", "resourceVersion", "creationTimestamp", "generation"):
                    fresh["metadata"].pop(field, None)
                fresh["metadata"].pop("managedFields", None)
                fresh.pop("status", None)
                made.append(self.store.create(fresh, field_manager="helm"))
                # The watch expired, so the informer hears only what a list finds.
                self.store.compact()
                controller.pump()
                self.rig.hold = set()

        self.rig.on_tick = recreate
        controller.elect()
        controller.pump()
        self.assertTrue(controller.work_once())
        self.rig.on_tick = None
        self.assertEqual(
            json.loads((old / "run.json").read_text())["stages"]["profile"]["status"], "cancelled"
        )
        for state in (controller._written, controller._ready, controller._previous):
            self.assertNotIn(old_uid, state)
        self.rig.fleet = made[0]
        self.work(controller)
        self.assertNotEqual(self.directory(), old)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True", self.status())

    def test_an_expired_watch_lists_again_and_sees_the_edit(self):
        controller = self.ready()
        self.edit({"slo": {"ttftS": "5"}})
        self.store.compact()
        jobs = len(self.rig.kinds)
        with patch.object(
            controller.informer, "_relist", wraps=controller.informer._relist
        ) as relist:
            self.work(controller)
        self.assertTrue(relist.called)
        self.assertEqual(self.status()["observedGeneration"], 2)
        self.assertIn("preflight", self.rig.kinds[jobs:])
        self.assertNotIn("profile", self.rig.kinds[jobs:])
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")


class StallTests(ControllerTest):
    def test_a_stale_crd_schema_stalls_the_fleet_before_anything_runs(self):
        crd = self.store.get(CRD, None, fleet_resource.CRD_NAME)
        crd["spec"]["versions"][0]["additionalPrinterColumns"] = []
        self.store.put(crd)
        self.work(self.leader())
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "SchemaStale"))
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "False")
        self.assertEqual(self.rig.workloads, [])
        self.assertEqual(self.status()["observedGeneration"], 1)

    def test_a_missing_admission_policy_stalls_the_fleet_before_anything_runs(self):
        self.store.remove(store_kind(VALIDATING_ADMISSION_POLICY_BINDING), None, POLICY)
        self.work(self.leader())
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "AdmissionPolicyMissing"))
        self.assertEqual(self.rig.workloads, [])

    def test_a_model_volume_the_admin_does_not_allow_is_refused(self):
        self.edit(
            {
                "engines": {
                    "modelVolume": {"persistentVolumeClaim": None, "hostPath": {"path": "/models"}}
                }
            }
        )
        since = len(self.rig.requests)
        self.work(self.leader())
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "InputRefused"))
        self.assertIn(
            "engines.modelVolume.hostPath: the cluster admin allows only persistentVolumeClaim",
            stalled["message"],
        )
        self.assertEqual(self.rig.workloads, [])
        writes = {resource for _, _, resource in self.writes(since)}
        self.assertEqual(writes, {"leases", f"{fleet_resource.PLURAL}/status"})

    def test_a_fleet_without_the_control_token_secret_is_refused(self):
        self.edit({"controlTokenSecret": None})
        self.work(self.leader())
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "InputRefused"))
        self.assertIn("spec.controlTokenSecret", stalled["message"])
        self.assertEqual(self.rig.workloads, [])

    def test_a_control_token_in_the_engine_keys_secret_key_is_refused(self):
        token = self.fleet()["spec"]["controlTokenSecret"]
        self.edit({"engines": {"apiKeySecret": dict(token)}})
        self.work(self.leader())
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "InputRefused"))
        self.assertIn("keep the control token in its own Secret", stalled["message"])
        self.assertEqual(self.rig.workloads, [])

    def test_a_new_control_token_secret_restarts_only_the_routers(self):
        controller = self.ready()
        before = {key: entry["directory"] for key, entry in self.record()["stages"].items()}
        self.store.create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "router-token-v2", "namespace": NAMESPACE},
                "data": {"token": base64.b64encode(b"e5" * 32).decode()},
            }
        )
        self.rig.control_secret = {"name": "router-token-v2", "key": "token"}
        self.edit({"controlTokenSecret": {"name": "router-token-v2"}})
        self.ready(controller)
        after = {key: entry["directory"] for key, entry in self.record()["stages"].items()}
        self.assertEqual({key for key in after if after[key] != before.get(key)}, {"router-start"})
        [router] = [obj for obj in self.derived() if obj["metadata"]["name"] == "router"]
        env = router["spec"]["template"]["spec"]["containers"][0]["env"]
        [token] = [item for item in env if item["name"] == ROUTER_TOKEN_ENV]
        self.assertEqual(token["valueFrom"]["secretKeyRef"]["name"], "router-token-v2")

    def test_a_runtime_class_the_admin_does_not_list_is_refused(self):
        self.edit({"engines": {"resources": {"runtimeClassName": "unlisted"}}})
        self.work(self.leader())
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual(stalled["reason"], "InputRefused")
        self.assertIn("runtimeClassName", stalled["message"])
        self.assertEqual(self.rig.workloads, [])

    def test_a_router_storage_change_stalls_instead_of_failing_each_apply(self):
        controller = self.ready()
        since = len(self.rig.requests)
        jobs = len(self.rig.kinds)
        self.edit({"routers": {"size": "2Gi"}})
        self.work(controller)
        stalled = self.condition(fleet_resource.STALLED)
        self.assertEqual((stalled["status"], stalled["reason"]), ("True", "RouterStorageImmutable"))
        self.assertIn("spec.routers.size is 2Gi", stalled["message"])
        self.assertIn("router-state-router-0", stalled["message"])
        self.assertEqual(self.rig.kinds[jobs:], [])
        self.assertNotIn(("apply", "apps", "statefulsets"), self.writes(since))
        # A resync checks again and still applies nothing.
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(self.rig.kinds[jobs:], [])
        # Putting the block back is no change.
        self.edit({"routers": {"size": "1Gi"}})
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True", self.status())

    def test_a_stall_file_behind_a_link_is_not_read(self):
        controller = self.ready()
        forged = self.root / "forged.json"
        forged.write_text(json.dumps({"generation": 1, "requested_at": "", "message": "forged"}))
        (self.directory() / kubernetes_controller.STALL_FILE).symlink_to(forged)
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "False")
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")

    def test_a_run_directory_other_users_may_write_is_refused_before_its_files_are_read(self):
        directory = self.directory()
        directory.mkdir(parents=True, mode=0o700)
        stall = {"generation": 1, "requested_at": "", "message": "written by another user"}
        (directory / kubernetes_controller.STALL_FILE).write_text(json.dumps(stall))
        directory.chmod(0o777)
        self.work(self.leader())
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "False")
        reconciling = self.condition(fleet_resource.RECONCILING)
        self.assertEqual(reconciling["reason"], "Retrying")
        self.assertIn("chmod 700", reconciling["message"])
        self.assertEqual(self.rig.workloads, [])

    def test_router_storage_compares_quantities_by_value(self):
        live = {
            "spec": {
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": objects.ROUTER_VOLUME},
                        "spec": {
                            "storageClassName": "local",
                            "resources": {"requests": {"storage": "1Gi"}},
                        },
                    }
                ]
            }
        }
        change = kubernetes_controller.router_storage_change
        self.assertIsNone(change(live, {"storageClassName": "local", "size": "1024Mi"}))
        self.assertIn("storageClassName", change(live, {"storageClassName": "fast", "size": "1Gi"}))
        self.assertIn("size", change(live, {"storageClassName": "local", "size": "1G"}))


class StopTests(ControllerTest):
    def router_claims(self) -> None:
        """The router-state claims the StatefulSet controller makes, which nothing owns."""
        for pod in objects.ROUTER_PODS:
            self.store.create(
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": f"{objects.ROUTER_VOLUME}-{pod}", "namespace": NAMESPACE},
                    "spec": {"accessModes": ["ReadWriteOnce"]},
                }
            )

    def claims(self) -> list[str]:
        return sorted(
            c["metadata"]["name"] for c in self.store.live(PERSISTENT_VOLUME_CLAIM, NAMESPACE)
        )

    def test_stopped_deletes_the_workloads_in_order_and_keeps_the_state(self):
        controller = self.ready()
        self.router_claims()
        since = len(self.store.deletes)
        self.edit({"stopped": True})
        self.work(controller)
        stopped = self.condition(fleet_resource.STOPPED)
        self.assertEqual((stopped["status"], stopped["reason"]), ("True", "Stopped"))
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "False")
        self.assertEqual(self.condition(fleet_resource.RECONCILING)["status"], "False")
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "False")
        self.assertFalse(self.status()["serving"])
        self.assertEqual(self.status()["engines"], "0/2")
        self.rig.settle()
        self.assertEqual(self.derived(), [])
        deletes = self.store.deletes[since:]
        self.assertTrue(all(d.propagation == "Background" for d in deletes))
        order = [(d.kind, d.name) for d in deletes]
        self.assertLess(
            order.index(("StatefulSet", "router")), order.index(("Deployment", "haproxy"))
        )
        self.assertLess(
            order.index(("Deployment", "haproxy")), order.index(("StatefulSet", "engine"))
        )
        self.assertEqual(self.claims(), ["router-state-router-0", "router-state-router-1"])
        self.assertTrue((self.directory() / "run.json").is_file())
        # Clearing it starts the engines and routers again from the record.
        jobs = len(self.rig.kinds)
        self.edit({"stopped": False})
        self.work(controller)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True", self.status())
        self.assertEqual(self.condition(fleet_resource.STOPPED)["status"], "False")
        self.assertTrue(self.status()["serving"])
        self.assertNotIn("discovery", self.rig.kinds[jobs:])
        self.assertEqual(self.record()["stages"]["engine-start[engine-1]"]["attempt"], 2)

    def test_an_unstopped_fleet_does_not_serve_until_its_routers_start_again(self):
        controller = self.ready()
        self.edit({"stopped": True})
        self.work(controller)
        self.assertFalse(self.status()["serving"])
        record = self.record()
        for stage in kubernetes_target.STOP_INVALIDATES:
            statuses = {e["status"] for e in record["stages"].values() if e["stage"] == stage}
            self.assertEqual(statuses, {"invalidated"}, stage)
        self.rig.hold = {"engine-check"}
        seen: list[tuple[bool, set[str]]] = []

        def look() -> None:
            if self.held("engine-check") and not seen:
                routers = {
                    o["metadata"]["name"] for o in self.derived() if o["kind"] == "StatefulSet"
                }
                seen.append((self.status().get("serving"), routers))
                self.rig.released.update(j["metadata"]["name"] for j in self.rig.live_jobs())
                self.rig.hold = set()

        self.rig.on_tick = look
        self.edit({"stopped": False})
        self.work(controller)
        self.rig.on_tick = None
        [(serving, sets)] = seen
        self.assertNotIn("router", sets)
        self.assertFalse(serving)
        self.assertTrue(self.status()["serving"])

    def test_stopped_during_a_stage_cancels_it(self):
        controller = self.leader()
        self.rig.hold = {"profile"}
        stopped = []

        def stop() -> None:
            if self.held("profile") and not stopped:
                stopped.append(True)
                self.edit({"stopped": True})
                controller.pump()

        self.rig.on_tick = stop
        self.work(controller)
        self.rig.on_tick = None
        self.assertEqual(self.record()["stages"]["profile"]["status"], "cancelled")
        self.assertEqual(self.condition(fleet_resource.STOPPED)["status"], "True")
        self.rig.settle()
        self.assertEqual(self.derived(), [])

    def test_deleting_the_fleet_mid_stage_leaves_cleanup_to_the_garbage_collector(self):
        controller = self.leader()
        self.router_claims()
        self.rig.hold = {"profile"}
        deleted: list[int] = []
        directory = self.directory()

        def delete() -> None:
            if self.held("profile") and not deleted:
                deleted.append(len(self.rig.requests))
                self.store.delete(
                    self.fleet_kind, NAMESPACE, fleet_resource.NAME, propagation_policy="Background"
                )
                controller.pump()

        self.rig.on_tick = delete
        self.work(controller)
        self.rig.on_tick = None
        self.rig.settle()
        # No finalizer holds the fleet, and the garbage collector took every derived object.
        with self.assertRaises(ApiError) as missing:
            self.fleet()
        self.assertEqual(missing.exception.code, 404)
        owner = objects.Owner.of(self.rig.fleet)
        found = [obj for kind in (*DERIVED, POD) for obj in self.store.live(kind, NAMESPACE)]
        self.assertEqual([o for o in found if objects.controlled(owner, o)], [])
        self.assertEqual([o["metadata"]["name"] for o in self.store.live(POD, NAMESPACE)], [])
        # The controller wrote nothing to the fleet after it went, and kept its state.
        after = self.writes(deleted[0])
        self.assertNotIn(("patch", fleet_resource.GROUP, f"{fleet_resource.PLURAL}/status"), after)
        self.assertTrue((directory / "run.json").is_file())
        self.assertEqual(self.claims(), ["router-state-router-0", "router-state-router-1"])
        self.assertFalse(controller.work_once())


class LeaseTests(ControllerTest):
    def test_only_the_lease_holder_acts(self):
        leader = self.leader("controller-a")
        other = self.controller("controller-b")
        since = len(self.rig.requests)
        self.assertFalse(other.elect())
        other.pump()
        self.assertFalse(other.work_once())
        self.ready(leader)
        jobs = len(self.rig.kinds)
        since = len(self.rig.requests)
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        other.informer.resync()
        other.pump()
        self.assertFalse(other.elect())
        self.assertFalse(other.work_once())
        self.assertEqual(self.writes(since), [])
        self.assertEqual(self.rig.kinds[jobs:], [])
        lease = self.store.get(LEASE, NAMESPACE, fleet_resource.CONTROLLER_LEASE)
        self.assertEqual(lease["spec"]["holderIdentity"], "controller-a")

    def test_a_holder_stops_acting_before_another_can_take_the_lease(self):
        # client-go's timings: a 15 s lease, renewed within 10 s.
        first = self.controller("controller-a", ttl=kubernetes_controller.LEASE_SECONDS)
        second = self.controller("controller-b", ttl=kubernetes_controller.LEASE_SECONDS)
        first.elect()
        for _ in range(8):
            self.store.clock.advance(kubernetes_controller.RETRY_SECONDS)
            first.elect()
        self.assertTrue(first.lease.valid())
        self.assertFalse(second.elect())
        # The first stops renewing: past its renew deadline it fences, while the second still
        # waits out the lease duration.
        self.store.clock.advance(kubernetes_controller.RENEW_DEADLINE_SECONDS)
        with self.assertRaises(Exited):
            first.fence()
        self.assertEqual(self.exits, [("controller-a", 1)])
        self.assertFalse(second.elect())
        self.store.clock.advance(
            kubernetes_controller.LEASE_SECONDS - kubernetes_controller.RENEW_DEADLINE_SECONDS
        )
        self.assertTrue(second.elect())
        self.assertFalse(first.work_once())

    def test_a_write_after_the_lease_is_lost_mid_pass_is_never_sent(self):
        controller = self.leader()
        self.rig.hold = {"profile"}
        lost: list[int] = []
        valid = patch.object(controller.lease, "valid", return_value=False)

        def lose() -> None:
            if self.held("profile") and not lost:
                lost.append(len(self.rig.requests))
                valid.start()
                self.addCleanup(valid.stop)
                self.rig.hold = set()

        self.rig.on_tick = lose
        controller.pump()
        with self.assertRaises(kubernetes_controller.LeaseLost):
            controller.work_once()
        self.rig.on_tick = None
        self.assertEqual(self.writes(lost[0]), [])
        self.assertIn(("controller-a", "write"), self.exits)

    def crash_at(self, controller: Controller, kind: str) -> bytes:
        """Run until a Job of `kind` runs, then end the controller as a killed pod ends."""
        saved: dict[str, bytes] = {}

        def crash() -> None:
            if self.held(kind) and not saved:
                saved["record"] = (self.directory() / "run.json").read_bytes()
                raise Crash

        self.rig.on_tick = crash
        with self.assertRaises(Crash):
            self.work(controller)
        self.rig.on_tick = None
        return saved["record"]

    def test_a_new_lease_holder_adopts_the_job_a_killed_controller_left_running(self):
        ttl = kubernetes_controller.LEASE_SECONDS
        first = self.leader("controller-a", ttl=ttl)
        self.rig.hold = {"profile"}
        saved = self.crash_at(first, "profile")
        self.left_behind(saved)
        [name] = self.record()["stages"]["profile"]["jobs"]
        self.assertIsNotNone(self.store.get(JOB, NAMESPACE, name))
        # The replacement pod waits for the dead holder's Lease to expire.
        second = self.controller("controller-b", ttl=ttl)
        self.assertFalse(second.elect())
        self.store.clock.advance(ttl)
        self.assertTrue(second.elect())
        self.rig.hold = set()
        self.work(second)
        entry = self.record()["stages"]["profile"]
        self.assertEqual((entry["status"], entry["attempt"], entry["jobs"]), ("passed", 1, [name]))
        self.assertEqual(self.rig.kinds.count("profile"), 1)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "False")
        self.assertEqual(
            self.store.get(LEASE, NAMESPACE, fleet_resource.CONTROLLER_LEASE)["spec"][
                "holderIdentity"
            ],
            "controller-b",
        )

    def test_a_restarted_controller_adopts_every_job_of_a_multi_job_stage(self):
        ttl = kubernetes_controller.LEASE_SECONDS
        first = self.leader("controller-a", ttl=ttl)
        self.rig.hold = {"fabric-probe"}
        saved = self.crash_at(first, "fabric-probe")
        self.left_behind(saved)
        names = self.record()["stages"]["discover"]["jobs"]
        self.assertEqual(len(names), 4)
        second = self.controller("controller-b", ttl=ttl)
        second.elect()
        self.store.clock.advance(ttl)
        self.rig.hold = set()
        self.ready(second)
        self.assertEqual(self.rig.kinds.count("discovery"), 2)
        self.assertEqual(self.rig.kinds.count("fabric-probe"), 2)
        self.assertEqual(self.record()["stages"]["discover"]["jobs"], names)

    def test_an_interrupted_attempt_is_retried_and_adopted_and_never_stalls(self):
        controller = self.leader()
        self.rig.hold = {"profile"}

        def refuse_logs() -> None:
            if self.held("profile") and not self.rig.calls["logs refused"]:
                self.rig.calls["logs refused"] += 1
                self.rig.hold = set()
                self.store.fail(
                    "logs", POD, code=403, message="forbidden", times=kubernetes_target.LOG_TRIES
                )

        self.rig.on_tick = refuse_logs
        controller.elect()
        controller.pump()
        self.assertTrue(controller.work_once())
        self.rig.on_tick = None
        self.assertEqual(self.record()["stages"]["profile"]["status"], "interrupted")
        reconciling = self.condition(fleet_resource.RECONCILING)
        self.assertEqual((reconciling["status"], reconciling["reason"]), ("True", "Retrying"))
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "False")
        # The fleet comes back after the queue's backoff, and the pass adopts the Job.
        self.assertFalse(controller.work_once())
        self.store.clock.advance(1)
        self.work(controller)
        entry = self.record()["stages"]["profile"]
        self.assertEqual((entry["status"], entry["attempt"]), ("passed", 1))
        self.assertEqual(self.rig.kinds.count("profile"), 1)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")

    def test_a_signal_ends_the_process_without_touching_any_job(self):
        since = len(self.rig.requests)
        with (
            patch.object(kubernetes_controller.os, "_exit", side_effect=Exited) as exited,
            self.assertRaises(Exited),
        ):
            kubernetes_controller._exit_now(signal.SIGTERM, None)
        exited.assert_called_once_with(0)
        self.assertEqual(self.rig.requests[since:], [])


class KeyCheckTests(ControllerTest):
    def key_checks(self, since: int = 0) -> list[dict]:
        return [e for e in self.rig.created[since:] if e["kind"] == "key-check"]

    def test_the_key_is_checked_after_an_edit_or_a_request_or_once_a_period(self):
        controller = self.ready()
        self.assertEqual(len(self.key_checks()), 1)
        created = len(self.rig.created)
        for _ in range(3):
            self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
            controller.informer.resync()
            self.work(controller)
        self.assertEqual(self.key_checks(created), [])
        self.store.clock.advance(self.settings.key_check_period)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(len(self.key_checks(created)), 1)
        created = len(self.rig.created)
        self.annotate({fleet_resource.REQUESTED_AT: "2026-11-28T11:00:00Z"})
        self.work(controller)
        self.assertEqual(len(self.key_checks(created)), 1)
        created = len(self.rig.created)
        self.edit({"slo": {"ttftS": "5"}})
        self.work(controller)
        self.assertEqual(len(self.key_checks(created)), 1)

    def test_the_period_comes_from_the_settings(self):
        self.settings = Settings.of(
            {**self.rig.settings, "admissionPolicy": POLICY, "keyCheckPeriodSeconds": 30}
        )
        controller = self.ready()
        created = len(self.rig.created)
        self.store.clock.advance(kubernetes_controller.RESYNC_SECONDS)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(len(self.key_checks(created)), 1)

    def test_a_key_checks_log_and_kept_files_go_once_read(self):
        def keep(obj: dict, old: dict | None) -> None:
            labels = obj["metadata"].get("labels") or {}
            if old is None and labels.get(fleet_resource.GATE_LABEL) == "key-check":
                entry = entry_of(obj)
                kept = self.state / entry["keep"]
                kept.mkdir(parents=True)
                (kept / "log").write_text("kept\n")

        self.store.validators[JOB].append(keep)
        controller = self.ready()
        # A later check writes its log beside the run record.
        self.store.clock.advance(self.settings.key_check_period)
        controller.informer.resync()
        self.work(controller)
        self.assertEqual(len(self.key_checks()), 2)
        kept = self.state / "deploy" / "key-check"
        self.assertTrue(kept.is_dir())
        self.assertEqual(list(kept.iterdir()), [])
        self.assertEqual(list((self.directory() / "live").rglob("*.log")), [])

    def test_a_key_check_a_stopped_controller_left_is_deleted_not_adopted(self):
        ttl = kubernetes_controller.LEASE_SECONDS
        first = self.ready(self.leader("controller-a", ttl=ttl))
        # The next check, with the recorded salt, is under way when the process is killed.
        self.store.clock.advance(self.settings.key_check_period)
        first.informer.resync()
        self.rig.hold = {"key-check"}

        def crash() -> None:
            if self.held("key-check"):
                raise Crash

        self.rig.on_tick = crash
        # A killed process runs no cleanup.
        with (
            patch.object(kubernetes_target.KubernetesDeployment, "remove"),
            self.assertRaises(Crash),
        ):
            self.work(first)
        self.rig.on_tick = None
        [name] = {e["name"] for e in self.key_checks()}
        self.assertIsNotNone(self.store.get(JOB, NAMESPACE, name))
        second = self.controller("controller-b", ttl=ttl)
        second.elect()
        self.store.clock.advance(ttl)
        self.rig.hold = set()
        self.ready(second)
        # Its HMAC may be of an earlier key, so the Job is made again rather than adopted.
        self.assertEqual(len(self.key_checks()), 3)
        deletes = [d for d in self.store.deletes if d.name == name and d.kind == "Job"]
        self.assertTrue(deletes and all(d.propagation == "Background" for d in deletes))
        self.rig.settle()
        self.assertEqual(self.rig.live_jobs(), [])


class ReplayTests(ControllerTest):
    def test_every_write_whose_reply_is_lost_is_replayed_safely(self):
        faults = [
            ("create", lambda: JOB, None),
            ("apply", lambda: STATEFUL_SET, None),
            ("apply", lambda: CONFIG_MAP, None),
            ("patch", lambda: POD, None),
            ("delete", lambda: JOB, None),
            ("patch", lambda: self.fleet_kind, fleet_resource.NAME),
            ("create", lambda: LEASE, None),
            ("replace", lambda: LEASE, None),
        ]
        for verb, kind, name in faults:
            with self.subTest(verb=verb, kind=kind().kind):
                self.doCleanups()
                self.setUp()
                controller = self.controller()
                self.store.fail(verb, kind(), name=name, code=504, commit=True)
                controller.elect()
                for _ in range(2):
                    self.store.clock.advance(TTL / 2)
                    controller.elect()
                self.work(controller)
                self.assertFalse(
                    [f for f in self.store.faults if f.verb == verb], "the fault never fired"
                )
                self.assertEqual(self.rig.kinds, JOBS)
                self.assertEqual(
                    self.condition(fleet_resource.READY).get("status"), "True", self.status()
                )
                self.assertEqual(len(self.store.live(LEASE, NAMESPACE)), 1)


class SettingsTests(unittest.TestCase):
    def test_settings_need_the_charts_names_and_lists(self):
        good = {
            "stateClaim": "narwhal-state",
            "engineServiceAccount": "narwhal-engine",
            "admissionPolicy": "example-narwhal",
            "runtimeClassNames": ["nvidia"],
        }
        settings = Settings.of(good)
        self.assertEqual(settings.runtime_classes, ("nvidia",))
        self.assertEqual(settings.policy.sources, ("persistentVolumeClaim",))
        for field, value in (
            ("admissionPolicy", None),
            ("stateClaim", "Not A Name"),
            ("runtimeClassNames", "nvidia"),
            ("modelHostPathPrefixes", [1]),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                Settings.of({k: v for k, v in {**good, field: value}.items() if v is not None})

    def test_the_settings_file_is_read_as_one_object(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "controller.json"
            path.write_text("[]")
            with self.assertRaisesRegex(ValueError, "one JSON object"):
                Settings.load(path)
            path.write_text(
                json.dumps({"stateClaim": "s", "engineServiceAccount": "e", "admissionPolicy": "p"})
            )
            self.assertEqual(Settings.load(path).admission_policy, "p")

    def test_the_process_needs_its_pod_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "controller.json"
            path.write_text(
                json.dumps({"stateClaim": "s", "engineServiceAccount": "e", "admissionPolicy": "p"})
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                kubernetes_controller.main(["--settings", str(path)])


if __name__ == "__main__":
    unittest.main()
