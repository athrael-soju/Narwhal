"""The stub Kubernetes API behaves as the API server does where the controller relies on it."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import httpx

from tests.deployment.kubernetes_cluster import (
    CONFIG_MAP,
    CONFIG_MAP_BYTES,
    JOB,
    LEASE,
    NODE,
    POD,
    STATEFUL_SET,
    ApiError,
    Clock,
    Deletion,
    Exit,
    ObjectStore,
    Reject,
    Rig,
    Run,
    Wait,
    custom_kind,
    managers_of,
)

NS = "team"
FLEET = custom_kind("narwhal.example.com", "v1alpha1", "NarwhalFleet", "narwhalfleets")
GATE = "example.com/placement"


def config_map(name: str, data: dict | None = None, **metadata) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": NS, **metadata},
        "data": data or {},
    }


def fleet(spec: dict | None = None, name: str = "fleet") -> dict:
    return {
        "apiVersion": FLEET.api_version,
        "kind": FLEET.kind,
        "metadata": {"name": name, "namespace": NS},
        "spec": spec or {},
    }


def pod(name: str, **spec) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"containers": [{"name": "main", "image": "registry.example/app:1"}], **spec},
    }


def job(name: str, **spec) -> dict:
    template = {"spec": {"restartPolicy": "Never", "containers": [{"name": "gate", "image": "x"}]}}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"backoffLimit": 0, "template": template, **spec},
    }


def stateful_set(name: str, replicas: int, image: str = "engine:1") -> dict:
    labels = {"app.kubernetes.io/name": name}
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": NS},
        "spec": {
            "replicas": replicas,
            "ordinals": {"start": 1},
            "serviceName": name,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{"name": "engine", "image": image}]},
            },
        },
    }


def owned_by(kind, owner: dict) -> dict:
    return {
        "ownerReferences": [
            {
                "apiVersion": kind.api_version,
                "kind": kind.kind,
                "name": owner["metadata"]["name"],
                "uid": owner["metadata"]["uid"],
                "controller": True,
            }
        ]
    }


def condition(obj: dict, kind: str) -> dict:
    return next(c for c in obj["status"].get("conditions", []) if c["type"] == kind)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = ObjectStore(self.clock)
        self.store.define(FLEET)
        # A watch from resourceVersion "0" starts anywhere, so the tests start after a write.
        self.store.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}})

    def refused(self, code: int, reason: str | None = None):
        test = self

        class Refusal:
            def __enter__(self):
                self.context = test.assertRaises(ApiError)
                self.context.__enter__()
                return self.context

            def __exit__(self, *exc):
                handled = self.context.__exit__(*exc)
                test.assertEqual(self.context.exception.code, code, self.context.exception)
                if reason is not None:
                    test.assertEqual(self.context.exception.reason, reason)
                return handled

        return Refusal()


class ResourceVersionTests(StoreTest):
    def test_each_write_takes_a_new_resource_version_and_a_no_op_keeps_it(self):
        created = self.store.create(config_map("a", {"k": "1"}))
        changed = self.store.replace({**created, "data": {"k": "2"}})
        self.assertGreater(
            int(changed["metadata"]["resourceVersion"]), int(created["metadata"]["resourceVersion"])
        )
        self.assertEqual(changed["metadata"]["resourceVersion"], self.store.resource_version)
        same = self.store.replace(changed)
        again = self.store.patch(CONFIG_MAP, NS, "a", {"data": {"k": "2"}})
        self.assertEqual(
            same["metadata"]["resourceVersion"], changed["metadata"]["resourceVersion"]
        )
        self.assertEqual(
            again["metadata"]["resourceVersion"], changed["metadata"]["resourceVersion"]
        )
        events = self.store.watch(
            CONFIG_MAP, NS, resource_version=created["metadata"]["resourceVersion"]
        )
        self.assertEqual([event["type"] for event in events], ["MODIFIED"])

    def test_an_update_from_a_stale_read_conflicts_and_changes_nothing(self):
        read = self.store.create(config_map("a", {"k": "1"}))
        self.store.patch(CONFIG_MAP, NS, "a", {"data": {"k": "2"}})
        with self.refused(409, "Conflict") as refusal:
            self.store.replace({**read, "data": {"k": "3"}})
        self.assertIn("the object has been modified", refusal.exception.message)
        stale = {"metadata": {"resourceVersion": read["metadata"]["resourceVersion"]}}
        with self.refused(409, "Conflict"):
            self.store.patch(CONFIG_MAP, NS, "a", {**stale, "data": {"k": "3"}})
        self.assertEqual(self.store.get(CONFIG_MAP, NS, "a")["data"], {"k": "2"})

    def test_creating_an_existing_name_is_already_exists(self):
        self.store.create(config_map("a"))
        with self.refused(409, "AlreadyExists") as refusal:
            self.store.create(config_map("a"))
        self.assertEqual(refusal.exception.message, 'configmaps "a" already exists')

    def test_delete_preconditions_refuse_another_object(self):
        created = self.store.create(config_map("a"))
        for preconditions in ({"uid": "another"}, {"resourceVersion": "0"}):
            with self.refused(409):
                self.store.delete(CONFIG_MAP, NS, "a", preconditions=preconditions)
        self.store.get(CONFIG_MAP, NS, "a")
        self.store.delete(CONFIG_MAP, NS, "a", preconditions={"uid": created["metadata"]["uid"]})
        with self.refused(404, "NotFound"):
            self.store.get(CONFIG_MAP, NS, "a")

    def test_an_injected_failure_refuses_the_call_or_loses_its_reply(self):
        self.store.fail("create", CONFIG_MAP, code=503)
        with self.refused(503, "ServiceUnavailable"):
            self.store.create(config_map("a"))
        self.assertEqual(self.store.list(CONFIG_MAP, NS)["items"], [])
        # The write lands, then the connection drops: a retry finds it already there.
        self.store.fail("create", CONFIG_MAP, name="a", code=504, commit=True)
        with self.refused(504):
            self.store.create(config_map("a"))
        with self.refused(409, "AlreadyExists"):
            self.store.create(config_map("a"))
        self.store.fail("get", code=410, times=2)
        for _ in range(2):
            with self.refused(410, "Expired"):
                self.store.get(CONFIG_MAP, NS, "a")
        self.store.get(CONFIG_MAP, NS, "a")

    def test_a_dry_run_changes_nothing(self):
        before = self.store.resource_version
        result = self.store.create(config_map("a"), dry_run=True)
        self.assertEqual(result["metadata"]["name"], "a")
        self.assertEqual(self.store.resource_version, before)
        self.assertEqual(self.store.list(CONFIG_MAP, NS)["items"], [])


class GenerationTests(StoreTest):
    def test_a_spec_change_counts_a_generation_and_metadata_and_status_do_not(self):
        created = self.store.create(fleet({"slo": {"ttftS": 4}}))
        self.assertEqual(created["metadata"]["generation"], 1)
        self.assertNotIn("status", created)
        edited = self.store.patch(FLEET, NS, "fleet", {"spec": {"slo": {"ttftS": 5}}})
        self.assertEqual(edited["metadata"]["generation"], 2)
        labelled = self.store.patch(FLEET, NS, "fleet", {"metadata": {"labels": {"a": "b"}}})
        self.assertEqual(labelled["metadata"]["generation"], 2)
        status = {"status": {"observedGeneration": 2, "serving": True}}
        observed = self.store.patch(FLEET, NS, "fleet", status, subresource="status")
        self.assertEqual(observed["metadata"]["generation"], 2)
        self.assertEqual(observed["status"], status["status"])
        # A write to the object leaves status alone, and a status write leaves the spec alone.
        self.store.patch(FLEET, NS, "fleet", {"status": {"serving": False}})
        spec = {"spec": {"slo": {"ttftS": 9}}, "status": {"serving": False}}
        self.store.patch(FLEET, NS, "fleet", spec, subresource="status")
        current = self.store.get(FLEET, NS, "fleet")
        self.assertEqual(current["spec"]["slo"]["ttftS"], 5)
        self.assertEqual(current["status"], {"observedGeneration": 2, "serving": False})
        self.assertEqual(current["metadata"]["generation"], 2)

    def test_the_statefulset_controller_reports_the_generation_it_observed(self):
        self.store.create(stateful_set("engine", 1))
        self.store.settle()
        status = self.store.get(STATEFUL_SET, NS, "engine")["status"]
        self.assertEqual((status["observedGeneration"], status["readyReplicas"]), (1, 1))
        scaled = self.store.patch(STATEFUL_SET, NS, "engine", {"spec": {"replicas": 2}})
        self.assertEqual(scaled["metadata"]["generation"], 2)
        self.assertEqual(scaled["status"]["observedGeneration"], 1)
        self.store.settle()
        status = self.store.get(STATEFUL_SET, NS, "engine")["status"]
        self.assertEqual((status["observedGeneration"], status["readyReplicas"]), (2, 2))


class WatchTests(StoreTest):
    def test_a_watch_replays_each_change_after_its_resource_version(self):
        start = self.store.resource_version
        self.store.create(config_map("a"))
        self.store.patch(CONFIG_MAP, NS, "a", {"data": {"k": "1"}})
        self.store.delete(CONFIG_MAP, NS, "a")
        events = self.store.watch(CONFIG_MAP, NS, resource_version=start)
        self.assertEqual([e["type"] for e in events], ["ADDED", "MODIFIED", "DELETED"])
        versions = [int(e["object"]["metadata"]["resourceVersion"]) for e in events]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(str(versions[-1]), self.store.resource_version)
        self.assertEqual(events[1]["object"]["data"], {"k": "1"})
        self.assertEqual(self.store.watch(CONFIG_MAP, NS, resource_version=str(versions[-1])), [])
        self.assertEqual(self.store.watch(CONFIG_MAP, "other", resource_version=start), [])

    def test_a_watch_without_a_resource_version_starts_with_the_current_objects(self):
        self.store.create(config_map("a"))
        self.store.create(config_map("b"))
        self.store.delete(CONFIG_MAP, NS, "a")
        events = self.store.watch(CONFIG_MAP, NS, bookmarks=True)
        self.assertEqual(
            [(e["type"], e["object"]["metadata"].get("name")) for e in events],
            [("ADDED", "b"), ("BOOKMARK", None)],
        )
        self.assertEqual(
            events[-1]["object"]["metadata"]["resourceVersion"], self.store.resource_version
        )

    def test_an_object_leaving_a_label_selector_leaves_the_watch(self):
        start = self.store.resource_version
        self.store.create(config_map("a", labels={"app": "x"}))
        self.store.patch(CONFIG_MAP, NS, "a", {"metadata": {"labels": {"app": "y"}}})
        types = {
            selector: [
                e["type"]
                for e in self.store.watch(
                    CONFIG_MAP, NS, resource_version=start, label_selector=selector
                )
            ]
            for selector in ("app=x", "app=y", "app in (x,y)", "!app")
        }
        self.assertEqual(
            types,
            {
                "app=x": ["ADDED", "DELETED"],
                "app=y": ["ADDED"],
                "app in (x,y)": ["ADDED", "MODIFIED"],
                "!app": [],
            },
        )
        with self.refused(400):
            self.store.watch(CONFIG_MAP, NS, label_selector="app in x")

    def test_an_expired_resource_version_is_410_gone_and_a_relist_resumes(self):
        self.store.create(config_map("a"))
        old = self.store.resource_version
        self.store.create(config_map("b"))
        self.store.compact()
        with self.refused(410, "Expired") as refusal:
            self.store.watch(CONFIG_MAP, NS, resource_version=old)
        self.assertIn("too old resource version", refusal.exception.message)
        listed = self.store.list(CONFIG_MAP, NS)
        self.assertEqual([item["metadata"]["name"] for item in listed["items"]], ["a", "b"])
        self.assertNotIn("kind", listed["items"][0])
        self.store.create(config_map("c"))
        events = self.store.watch(
            CONFIG_MAP, NS, resource_version=listed["metadata"]["resourceVersion"]
        )
        self.assertEqual(
            [(e["type"], e["object"]["metadata"]["name"]) for e in events], [("ADDED", "c")]
        )

    def test_a_paged_list_reads_one_snapshot_and_its_token_expires(self):
        for name in ("a", "b", "c"):
            self.store.create(config_map(name))
        first = self.store.list(CONFIG_MAP, NS, limit=2)
        self.assertEqual(first["metadata"]["remainingItemCount"], 1)
        self.store.create(config_map("d"))
        second = self.store.list(CONFIG_MAP, NS, limit=2, continue_=first["metadata"]["continue"])
        self.assertEqual([i["metadata"]["name"] for i in second["items"]], ["c"])
        self.assertEqual(
            second["metadata"]["resourceVersion"], first["metadata"]["resourceVersion"]
        )
        self.assertNotIn("continue", second["metadata"])
        again = self.store.list(CONFIG_MAP, NS, limit=1)
        self.store.delete(CONFIG_MAP, NS, "d")
        self.store.compact()
        with self.refused(410, "Expired"):
            self.store.list(CONFIG_MAP, NS, continue_=again["metadata"]["continue"])

    def test_a_field_selector_names_a_supported_field(self):
        self.store.create(pod("a"))
        self.store.create(pod("b", nodeName="gpu-a"))
        names = [
            p["metadata"]["name"]
            for p in self.store.list(POD, NS, field_selector="spec.nodeName=gpu-a")["items"]
        ]
        self.assertEqual(names, ["b"])
        phases = self.store.list(POD, NS, field_selector="status.phase=Pending,metadata.name!=b")
        self.assertEqual([p["metadata"]["name"] for p in phases["items"]], ["a"])
        with self.refused(400):
            self.store.list(POD, NS, field_selector="spec.hostNetwork=true")


class GarbageCollectionTests(StoreTest):
    def owner_with_job(self) -> tuple[dict, dict]:
        owner = self.store.create(fleet())
        gate = self.store.create(
            {**job("gate"), "metadata": {"name": "gate", "namespace": NS, **owned_by(FLEET, owner)}}
        )
        self.store.kubelet = lambda pod: Run()
        self.store.settle()
        return owner, gate

    def test_background_deletion_leaves_the_dependents_to_the_collector(self):
        self.owner_with_job()
        [gate_pod] = self.store.list(POD, NS)["items"]
        self.store.delete(FLEET, NS, "fleet", propagation_policy="Background")
        with self.refused(404):
            self.store.get(FLEET, NS, "fleet")
        # Until the collector runs, the Job and its pod remain.
        self.store.get(JOB, NS, "gate")
        self.store.settle()
        with self.refused(404):
            self.store.get(JOB, NS, "gate")
        with self.refused(404):
            self.store.get(POD, NS, gate_pod["metadata"]["name"])

    def test_orphan_deletion_keeps_the_dependents_without_their_owner_reference(self):
        owner = self.store.create(fleet())
        self.store.create(config_map("kept", **owned_by(FLEET, owner)))
        terminating = self.store.delete(FLEET, NS, "fleet", propagation_policy="Orphan")
        self.assertEqual(terminating["metadata"]["finalizers"], ["orphan"])
        self.assertIn("deletionTimestamp", terminating["metadata"])
        self.store.settle()
        with self.refused(404):
            self.store.get(FLEET, NS, "fleet")
        self.assertNotIn("ownerReferences", self.store.get(CONFIG_MAP, NS, "kept")["metadata"])

    def test_foreground_deletion_removes_the_dependents_before_the_owner(self):
        owner = self.store.create(fleet())
        self.store.create(config_map("child", **owned_by(FLEET, owner)))
        self.store.delete(FLEET, NS, "fleet", propagation_policy="Foreground")
        self.assertEqual(
            self.store.get(FLEET, NS, "fleet")["metadata"]["finalizers"], ["foregroundDeletion"]
        )
        self.store.settle()
        for kind, name in ((CONFIG_MAP, "child"), (FLEET, "fleet")):
            with self.refused(404):
                self.store.get(kind, NS, name)

    def test_a_dependent_with_another_live_owner_stays(self):
        first = self.store.create(fleet(name="fleet"))
        second = self.store.create(fleet(name="other"))
        references = owned_by(FLEET, first)["ownerReferences"] + [
            {**owned_by(FLEET, second)["ownerReferences"][0], "controller": False}
        ]
        self.store.create(config_map("shared", ownerReferences=references))
        self.store.delete(FLEET, NS, "fleet")
        self.store.settle()
        kept = self.store.get(CONFIG_MAP, NS, "shared")["metadata"]["ownerReferences"]
        self.assertEqual([ref["name"] for ref in kept], ["other"])

    def test_an_owner_recreated_under_its_name_does_not_keep_the_old_dependents(self):
        owner = self.store.create(fleet())
        self.store.create(config_map("child", **owned_by(FLEET, owner)))
        self.store.remove(FLEET, NS, "fleet")
        self.store.create(fleet())
        self.store.settle()
        with self.refused(404):
            self.store.get(CONFIG_MAP, NS, "child")

    def test_every_job_delete_must_send_background(self):
        self.owner_with_job()
        with self.assertRaisesRegex(AssertionError, "must send Background"):
            self.store.delete(JOB, NS, "gate")
        self.assertEqual(self.store.deletes, [Deletion("Job", NS, "gate", None)])
        # The API default orphans a Job's pods, which is why the strict check exists.
        self.store.strict_job_deletes = False
        self.store.delete(JOB, NS, "gate")
        self.store.settle()
        [orphan] = self.store.list(POD, NS)["items"]
        self.assertNotIn("ownerReferences", orphan["metadata"])
        self.store.strict_job_deletes = True
        self.store.create(job("next"))
        self.store.settle()
        self.store.delete(JOB, NS, "next", propagation_policy="Background")
        self.store.settle()
        self.assertEqual(
            [p["metadata"]["name"] for p in self.store.list(POD, NS)["items"]],
            [orphan["metadata"]["name"]],
        )
        self.assertEqual(self.store.deletes[-1], Deletion("Job", NS, "next", "Background"))


class FinalizerTests(StoreTest):
    def test_a_finalizer_holds_a_deleted_object_until_it_is_removed(self):
        self.store.create(config_map("a", finalizers=["example.com/cleanup"]))
        start = self.store.resource_version
        terminating = self.store.delete(CONFIG_MAP, NS, "a")
        self.assertEqual(terminating["metadata"]["deletionTimestamp"], "2026-11-27T10:00:00Z")
        self.assertEqual(
            self.store.get(CONFIG_MAP, NS, "a")["metadata"]["finalizers"], ["example.com/cleanup"]
        )
        # A second delete changes nothing.
        self.assertEqual(
            self.store.delete(CONFIG_MAP, NS, "a")["metadata"]["resourceVersion"],
            terminating["metadata"]["resourceVersion"],
        )
        with self.refused(409, "AlreadyExists"):
            self.store.create(config_map("a"))
        self.store.patch(CONFIG_MAP, NS, "a", {"metadata": {"finalizers": None}})
        with self.refused(404):
            self.store.get(CONFIG_MAP, NS, "a")
        events = self.store.watch(CONFIG_MAP, NS, resource_version=start)
        self.assertEqual([e["type"] for e in events], ["MODIFIED", "DELETED"])

    def test_no_finalizer_can_be_added_while_terminating(self):
        self.store.create(config_map("a", finalizers=["example.com/one"]))
        self.store.delete(CONFIG_MAP, NS, "a")
        with self.refused(422, "Invalid") as refusal:
            self.store.patch(
                CONFIG_MAP,
                NS,
                "a",
                {"metadata": {"finalizers": ["example.com/one", "example.com/two"]}},
            )
        self.assertIn("no new finalizers can be added", refusal.exception.message)

    def test_deleting_a_custom_resource_with_a_finalizer_counts_a_generation(self):
        self.store.create(
            {
                **fleet(),
                "metadata": {"name": "fleet", "namespace": NS, "finalizers": ["example.com/x"]},
            }
        )
        self.assertEqual(self.store.delete(FLEET, NS, "fleet")["metadata"]["generation"], 2)


class ServerSideApplyTests(StoreTest):
    def test_apply_creates_and_records_the_fields_it_manages(self):
        applied = self.store.apply(
            config_map("a", {"x": "1", "y": "2"}), field_manager="narwhal-cli"
        )
        [entry] = applied["metadata"]["managedFields"]
        self.assertEqual(
            (entry["manager"], entry["operation"], entry["fieldsType"]),
            ("narwhal-cli", "Apply", "FieldsV1"),
        )
        self.assertEqual(entry["fieldsV1"], {"f:data": {".": {}, "f:x": {}, "f:y": {}}})
        again = self.store.apply(config_map("a", {"x": "1", "y": "2"}), field_manager="narwhal-cli")
        self.assertEqual(
            again["metadata"]["resourceVersion"], applied["metadata"]["resourceVersion"]
        )

    def test_a_field_another_manager_set_differently_conflicts_until_forced(self):
        self.store.apply(config_map("a", {"x": "1"}), field_manager="helm")
        with self.refused(409, "Conflict") as refusal:
            self.store.apply(config_map("a", {"x": "2"}), field_manager="narwhal-cli")
        self.assertIn('conflict with "helm": .data.x', refusal.exception.message)
        self.assertEqual(refusal.exception.causes[0]["field"], ".data.x")
        # The same value shares the field.
        shared = self.store.apply(config_map("a", {"x": "1"}), field_manager="narwhal-cli")
        self.assertEqual(managers_of(shared, "data.x"), {"helm", "narwhal-cli"})
        forced = self.store.apply(
            config_map("a", {"x": "2"}), field_manager="narwhal-cli", force=True
        )
        self.assertEqual(forced["data"], {"x": "2"})
        self.assertEqual(managers_of(forced, "data.x"), {"narwhal-cli"})

    def test_a_field_dropped_from_the_applied_config_goes_unless_another_manager_owns_it(self):
        self.store.apply(
            config_map("a", {"x": "1", "y": "2", "z": "3"}), field_manager="narwhal-cli"
        )
        self.store.apply(config_map("a", {"y": "2"}), field_manager="helm")
        dropped = self.store.apply(config_map("a", {"x": "1"}), field_manager="narwhal-cli")
        self.assertEqual(dropped["data"], {"x": "1", "y": "2"})
        self.assertEqual(
            self.store.apply(config_map("a"), field_manager="helm")["data"], {"x": "1"}
        )

    def test_associative_lists_merge_by_key_between_managers(self):
        first = {"containers": [{"name": "a", "image": "a:1"}], "args": ["one"]}
        self.store.apply(fleet(first), field_manager="one")
        merged = self.store.apply(
            fleet({"containers": [{"name": "b", "image": "b:1"}]}), field_manager="two"
        )
        self.assertEqual([c["name"] for c in merged["spec"]["containers"]], ["a", "b"])
        # An atomic list is owned as a whole.
        with self.refused(409):
            self.store.apply(fleet({"args": ["two"]}), field_manager="two")
        left = self.store.apply(fleet({"args": ["one"]}), field_manager="one")
        self.assertEqual(left["spec"]["containers"], [{"name": "b", "image": "b:1"}])

    def test_an_update_takes_the_fields_it_changes_from_the_applier(self):
        self.store.apply(fleet({"slo": {"ttftS": 4, "tpotS": 1}}), field_manager="narwhal-cli")
        edited = self.store.patch(
            FLEET, NS, "fleet", {"spec": {"slo": {"ttftS": 5}}}, field_manager="kubectl-edit"
        )
        self.assertEqual(managers_of(edited, "spec.slo.ttftS"), {"kubectl-edit"})
        self.assertEqual(managers_of(edited, "spec.slo.tpotS"), {"narwhal-cli"})
        operations = {(e["manager"], e["operation"]) for e in edited["metadata"]["managedFields"]}
        self.assertEqual(operations, {("narwhal-cli", "Apply"), ("kubectl-edit", "Update")})
        with self.refused(409) as refusal:
            self.store.apply(fleet({"slo": {"ttftS": 4, "tpotS": 1}}), field_manager="narwhal-cli")
        self.assertIn('conflict with "kubectl-edit"', refusal.exception.message)

    def test_status_is_applied_through_its_subresource(self):
        self.store.apply(fleet({"slo": {"ttftS": 4}}), field_manager="narwhal-cli")
        status = {**fleet(), "status": {"serving": True}}
        del status["spec"]
        applied = self.store.apply(status, field_manager="narwhal-controller", subresource="status")
        self.assertEqual(applied["status"], {"serving": True})
        self.assertEqual(
            (applied["metadata"]["generation"], applied["spec"]), (1, {"slo": {"ttftS": 4}})
        )
        entry = next(
            e for e in applied["metadata"]["managedFields"] if e["manager"] == "narwhal-controller"
        )
        self.assertEqual(
            (entry["subresource"], entry["fieldsV1"]),
            ("status", {"f:status": {".": {}, "f:serving": {}}}),
        )
        # Status in an apply to the object itself is dropped.
        ignored = self.store.apply(
            {**fleet({"slo": {"ttftS": 4}}), "status": {"serving": False}},
            field_manager="narwhal-cli",
        )
        self.assertEqual(ignored["status"], {"serving": True})

    def test_apply_needs_a_field_manager_and_a_dry_run_keeps_nothing(self):
        with self.refused(400):
            self.store.apply(config_map("a"), field_manager="")
        self.store.apply(config_map("a"), field_manager="narwhal-cli", dry_run=True)
        with self.refused(404):
            self.store.get(CONFIG_MAP, NS, "a")


class JobTests(StoreTest):
    def test_a_job_runs_its_pod_through_its_phases_to_completion(self):
        self.store.kubelet = lambda pod: Exit(0, "done\n")
        start = self.store.resource_version
        created = self.store.create(job("gate"))
        self.store.settle()
        [gate_pod] = self.store.list(POD, NS, label_selector="job-name=gate")["items"]
        self.assertTrue(gate_pod["metadata"]["name"].startswith("gate-"))
        [reference] = gate_pod["metadata"]["ownerReferences"]
        self.assertEqual(
            (reference["kind"], reference["uid"], reference["controller"]),
            ("Job", created["metadata"]["uid"], True),
        )
        phases = [
            e["object"]["status"].get("phase")
            for e in self.store.watch(POD, NS, resource_version=start)
        ]
        self.assertEqual(
            [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]],
            ["Pending", "Running", "Succeeded"],
        )
        status = self.store.get(JOB, NS, "gate")["status"]
        self.assertEqual(status["succeeded"], 1)
        self.assertEqual(condition({"status": status}, "Complete")["status"], "True")
        self.assertEqual(self.store.logs(NS, gate_pod["metadata"]["name"]), "done\n")
        terminated = gate_pod["status"]["containerStatuses"][0]["state"]["terminated"]
        self.assertEqual((terminated["exitCode"], terminated["reason"]), (0, "Completed"))

    def test_a_failed_pod_fails_its_job_at_the_backoff_limit(self):
        self.store.kubelet = lambda pod: Exit(3, "boom\n")
        self.store.create(job("once"))
        self.store.create(job("twice", backoffLimit=1))
        self.store.settle()
        once = self.store.get(JOB, NS, "once")
        self.assertEqual(
            (condition(once, "Failed")["reason"], once["status"]["failed"]),
            ("BackoffLimitExceeded", 1),
        )
        [failed] = self.store.list(POD, NS, label_selector="job-name=once")["items"]
        self.assertEqual(
            failed["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"], 3
        )
        self.assertEqual(self.store.logs(NS, failed["metadata"]["name"], "gate"), "boom\n")
        self.assertEqual(len(self.store.list(POD, NS, label_selector="job-name=twice")["items"]), 2)

    def test_a_job_held_active_completes_when_its_pod_exits(self):
        outcome: list = [Run()]
        self.store.kubelet = lambda pod: outcome[0]
        self.store.create(job("gate"))
        self.store.settle()
        status = self.store.get(JOB, NS, "gate")["status"]
        self.assertEqual(status["active"], 1)
        self.assertNotIn("conditions", status)
        outcome[0] = Exit(0)
        self.store.settle()
        status = self.store.get(JOB, NS, "gate")["status"]
        self.assertNotIn("active", status)
        self.assertEqual(condition({"status": status}, "Complete")["status"], "True")

    def test_a_job_past_its_deadline_fails_and_loses_its_pods(self):
        self.store.kubelet = lambda pod: Run()
        self.store.create(job("gate", activeDeadlineSeconds=60))
        self.store.settle()
        self.clock.advance(59)
        self.store.settle()
        self.assertEqual(len(self.store.list(POD, NS)["items"]), 1)
        self.clock.advance(1)
        self.store.settle()
        self.assertEqual(
            condition(self.store.get(JOB, NS, "gate"), "Failed")["reason"], "DeadlineExceeded"
        )
        self.assertEqual(self.store.list(POD, NS)["items"], [])

    def test_a_suspended_job_starts_no_pod_and_its_template_cannot_change(self):
        self.store.create(job("gate", suspend=True))
        self.store.settle()
        self.assertEqual(self.store.list(POD, NS)["items"], [])
        image = [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "y"}]
        with self.refused(422, "Invalid") as refusal:
            self.store.patch(JOB, NS, "gate", image, patch_type="json")
        self.assertIn("spec.template: Invalid value: field is immutable", refusal.exception.message)
        self.store.patch(JOB, NS, "gate", {"spec": {"suspend": False}})
        self.store.settle()
        self.assertEqual(len(self.store.list(POD, NS)["items"]), 1)

    def test_a_job_template_must_not_restart_always(self):
        bad = job("gate")
        bad["spec"]["template"]["spec"]["restartPolicy"] = "Always"
        with self.refused(422):
            self.store.create(bad)

    def test_the_kubelet_reports_why_a_pod_cannot_start(self):
        outcomes = {
            "config": Wait("CreateContainerConfigError", 'secret "k" not found'),
            "gpus": Reject("OutOfgpu.example.com/gpu"),
        }
        self.store.kubelet = lambda pod: outcomes[pod["metadata"]["name"]]
        for name in outcomes:
            self.store.create(pod(name, nodeName="gpu-a"))
        self.store.settle()
        waiting = self.store.get(POD, NS, "config")
        self.assertEqual(waiting["status"]["phase"], "Pending")
        self.assertEqual(
            waiting["status"]["containerStatuses"][0]["state"]["waiting"]["reason"],
            "CreateContainerConfigError",
        )
        with self.refused(400):
            self.store.logs(NS, "config")
        rejected = self.store.get(POD, NS, "gpus")["status"]
        self.assertEqual(
            (rejected["phase"], rejected["reason"]), ("Failed", "OutOfgpu.example.com/gpu")
        )

    def test_a_restarted_container_has_a_new_identity(self):
        self.store.create(pod("engine", nodeName="gpu-a"))
        self.store.settle()
        before = self.store.get(POD, NS, "engine")["status"]["containerStatuses"][0]
        self.assertEqual(before["imageID"].rpartition("@")[0], "registry.example/app")
        self.clock.advance(5)
        after = self.store.restart_container(NS, "engine", "main")["status"]["containerStatuses"][0]
        self.assertNotEqual(after["containerID"], before["containerID"])
        self.assertEqual(after["restartCount"], 1)
        self.assertEqual(after["state"]["running"]["startedAt"], "2026-11-27T10:00:05Z")


class SchedulingTests(StoreTest):
    AFFINITY: ClassVar[dict] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchFields": [
                            {"key": "metadata.name", "operator": "In", "values": ["gpu-b"]}
                        ]
                    }
                ]
            }
        }
    }

    def gated(self) -> dict:
        created = self.store.create(pod("engine-1", schedulingGates=[{"name": GATE}]))
        self.store.settle()
        return created

    def test_a_gated_pod_waits_and_its_gates_can_only_be_removed(self):
        self.gated()
        held = self.store.get(POD, NS, "engine-1")
        self.assertNotIn("nodeName", held["spec"])
        self.assertEqual(held["status"]["phase"], "Pending")
        self.assertEqual(condition(held, "PodScheduled")["reason"], "SchedulingGated")
        added = [
            {"op": "add", "path": "/spec/schedulingGates/-", "value": {"name": "example.com/other"}}
        ]
        with self.refused(422) as refusal:
            self.store.patch(POD, NS, "engine-1", added, patch_type="json")
        self.assertIn("only deletion is allowed", refusal.exception.message)
        with self.refused(422):
            self.store.create(pod("both", nodeName="gpu-a", schedulingGates=[{"name": GATE}]))

    def test_a_gated_pod_takes_node_affinity_once_and_schedules_on_that_node(self):
        self.gated()
        pin = [{"op": "add", "path": "/spec/affinity", "value": self.AFFINITY}]
        self.store.patch(POD, NS, "engine-1", pin, patch_type="json")
        moved = json.loads(json.dumps(self.AFFINITY).replace("gpu-b", "gpu-c"))
        with self.refused(422):
            self.store.patch(
                POD,
                NS,
                "engine-1",
                [{"op": "replace", "path": "/spec/affinity", "value": moved}],
                patch_type="json",
            )
        ungate = [
            {"op": "test", "path": "/spec/schedulingGates/0/name", "value": GATE},
            {"op": "remove", "path": "/spec/schedulingGates/0"},
        ]
        self.store.patch(POD, NS, "engine-1", ungate, patch_type="json")
        self.store.settle()
        placed = self.store.get(POD, NS, "engine-1")
        self.assertNotIn("schedulingGates", placed["spec"])
        self.assertEqual(
            (placed["spec"]["nodeName"], placed["status"]["phase"]), ("gpu-b", "Running")
        )
        # Once scheduled, the pod's affinity is fixed.
        with self.refused(422):
            self.store.patch(
                POD,
                NS,
                "engine-1",
                [{"op": "replace", "path": "/spec/affinity", "value": moved}],
                patch_type="json",
            )

    def test_a_json_patch_applies_all_or_nothing(self):
        created = self.gated()
        operations = [
            {"op": "add", "path": "/spec/affinity", "value": self.AFFINITY},
            {"op": "test", "path": "/spec/schedulingGates/0/name", "value": "example.com/other"},
            {"op": "remove", "path": "/spec/schedulingGates/0"},
        ]
        before = self.store.get(POD, NS, "engine-1")
        with self.refused(422) as refusal:
            self.store.patch(POD, NS, "engine-1", operations, patch_type="json")
        self.assertIn(
            "testing value /spec/schedulingGates/0/name failed", refusal.exception.message
        )
        self.assertEqual(self.store.get(POD, NS, "engine-1"), before)
        self.assertEqual(created["metadata"]["uid"], before["metadata"]["uid"])

    def test_a_pod_for_a_node_the_cluster_lacks_is_unschedulable(self):
        self.store.create({"apiVersion": "v1", "kind": "Node", "metadata": {"name": "gpu-a"}})
        self.store.create(pod("lost", nodeSelector={"kubernetes.io/hostname": "gpu-z"}))
        self.store.create(pod("found", nodeSelector={"kubernetes.io/hostname": "gpu-a"}))
        self.store.create(pod("any"))
        self.store.settle()
        self.assertEqual(
            condition(self.store.get(POD, NS, "lost"), "PodScheduled")["reason"], "Unschedulable"
        )
        self.assertEqual(self.store.get(POD, NS, "found")["spec"]["nodeName"], "gpu-a")
        self.assertEqual(self.store.get(POD, NS, "any")["spec"]["nodeName"], "gpu-a")
        self.assertEqual([n["metadata"]["name"] for n in self.store.list(NODE)["items"]], ["gpu-a"])

    def test_a_statefulset_keeps_its_ordinal_pods_at_its_template(self):
        self.store.create(stateful_set("engine", 2))
        self.store.settle()
        pods = {p["metadata"]["name"]: p for p in self.store.list(POD, NS)["items"]}
        self.assertEqual(sorted(pods), ["engine-1", "engine-2"])
        self.assertEqual(pods["engine-1"]["spec"]["subdomain"], "engine")
        self.store.delete(POD, NS, "engine-1")
        self.store.settle()
        replaced = self.store.get(POD, NS, "engine-1")
        self.assertNotEqual(replaced["metadata"]["uid"], pods["engine-1"]["metadata"]["uid"])
        image = [
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "engine:2"}
        ]
        self.store.patch(STATEFUL_SET, NS, "engine", image, patch_type="json")
        self.store.settle()
        status = self.store.get(STATEFUL_SET, NS, "engine")["status"]
        hashes = {
            p["metadata"]["labels"]["controller-revision-hash"]
            for p in self.store.list(POD, NS)["items"]
        }
        self.assertEqual(hashes, {status["updateRevision"]})
        self.assertEqual(status["currentRevision"], status["updateRevision"])
        selector = {"spec": {"selector": {"matchLabels": {"app.kubernetes.io/name": "other"}}}}
        with self.refused(422) as refusal:
            self.store.patch(STATEFUL_SET, NS, "engine", selector)
        self.assertIn(
            "updates to statefulset spec for fields other than", refusal.exception.message
        )


class LeaseTests(StoreTest):
    def lease(self, holder: str, transitions: int = 0) -> dict:
        return {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": "narwhal-router", "namespace": NS},
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": 15,
                "acquireTime": "2026-11-27T10:00:00.000000Z",
                "renewTime": "2026-11-27T10:00:00.000000Z",
                "leaseTransitions": transitions,
            },
        }

    def test_two_candidates_race_for_a_lease_and_one_wins(self):
        self.store.create(self.lease("router-0"))
        with self.refused(409, "AlreadyExists"):
            self.store.create(self.lease("router-1"))
        held = self.store.get(LEASE, NS, "narwhal-router")
        seen = self.store.get(LEASE, NS, "narwhal-router")
        held["spec"]["renewTime"] = "2026-11-27T10:00:02.000000Z"
        self.store.replace(held)
        # The standby read before the renewal: its takeover loses.
        seen["spec"].update(holderIdentity="router-1", leaseTransitions=1)
        with self.refused(409, "Conflict"):
            self.store.replace(seen)
        fresh = self.store.get(LEASE, NS, "narwhal-router")
        self.assertEqual(fresh["spec"]["holderIdentity"], "router-0")
        fresh["spec"].update(holderIdentity="router-1", leaseTransitions=1)
        taken = self.store.replace(fresh)
        self.assertEqual(
            (taken["spec"]["holderIdentity"], taken["spec"]["leaseTransitions"]), ("router-1", 1)
        )
        self.assertNotIn("generation", taken["metadata"])

    def test_a_lease_refuses_invalid_timings(self):
        for field_name, value in (
            ("leaseDurationSeconds", 0),
            ("renewTime", "yesterday"),
            ("leaseTransitions", -1),
        ):
            lease = self.lease("router-0")
            lease["spec"][field_name] = value
            with self.refused(422) as refusal:
                self.store.create(lease)
            self.assertIn(f"spec.{field_name}", refusal.exception.message)


class ValidationTests(StoreTest):
    def test_names_labels_and_namespaces_are_checked(self):
        for bad in (
            config_map("Upper"),
            config_map("a", labels={"bad key!": "x"}),
            config_map("a", labels={"key": "x" * 64}),
        ):
            with self.refused(422, "Invalid"):
                self.store.create(bad)
        with self.refused(422):
            self.store.create(job("j" * 64))
        no_namespace = config_map("a")
        del no_namespace["metadata"]["namespace"]
        with self.refused(400):
            self.store.create(no_namespace)
        with self.refused(404):
            self.store.create(
                {"apiVersion": "v9", "kind": "Unknown", "metadata": {"name": "a", "namespace": NS}}
            )

    def test_a_config_map_holds_valid_keys_and_at_most_one_mebibyte(self):
        with self.refused(422) as refusal:
            self.store.create(config_map("a", {"x": "y" * CONFIG_MAP_BYTES}))
        self.assertIn("Too long", refusal.exception.message)
        with self.refused(422):
            self.store.create(config_map("a", {"config/a.txt": "x"}))
        with self.refused(422):
            self.store.create({**config_map("a"), "binaryData": {"x": "not base64!"}})
        self.store.create({**config_map("a", {"k": "1"}), "immutable": True})
        with self.refused(422):
            self.store.patch(CONFIG_MAP, NS, "a", {"data": {"k": "2"}})

    def test_a_kind_takes_extra_validators(self):
        def one_per_namespace(obj, old):
            if obj["metadata"]["name"] != "fleet":
                raise ValueError("metadata.name: Invalid value: the fleet must be named fleet")

        self.store.validators[FLEET] = [one_per_namespace]
        with self.refused(422) as refusal:
            self.store.create(fleet(name="second"))
        self.assertIn("the fleet must be named fleet", refusal.exception.message)
        self.store.create(fleet())


class TransportTests(StoreTest):
    def setUp(self):
        super().setUp()
        self.http = httpx.Client(
            transport=self.store.transport(),
            base_url="https://kubernetes.stub",
            headers={"User-Agent": "narwhal-test/1"},
        )
        self.addCleanup(self.http.close)

    def test_the_api_serves_objects_and_refusals_over_http(self):
        path = f"/api/v1/namespaces/{NS}/configmaps"
        created = self.http.post(path, json=config_map("a", {"k": "1"}))
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["metadata"]["managedFields"][0]["manager"], "narwhal-test")
        self.assertEqual(self.http.get(f"{path}/a").json()["data"], {"k": "1"})
        missing = self.http.get(f"{path}/b")
        self.assertEqual(
            (missing.status_code, missing.headers["content-type"]), (404, "application/json")
        )
        self.assertEqual((missing.json()["kind"], missing.json()["reason"]), ("Status", "NotFound"))
        stale = {**created.json(), "data": {"k": "2"}}
        self.http.patch(
            f"{path}/a",
            content=json.dumps({"data": {"k": "3"}}),
            headers={"Content-Type": "application/merge-patch+json"},
        )
        self.assertEqual(self.http.put(f"{path}/a", json=stale).status_code, 409)
        patched = self.http.patch(
            f"{path}/a",
            content=json.dumps([{"op": "replace", "path": "/data/k", "value": "4"}]),
            headers={"Content-Type": "application/json-patch+json"},
        )
        self.assertEqual(patched.json()["data"], {"k": "4"})
        listed = self.http.get(path, params={"labelSelector": ""}).json()
        self.assertEqual((listed["kind"], len(listed["items"])), ("ConfigMapList", 1))
        self.assertEqual(self.http.get("/api/v1/namespaces/other/configmaps/a").status_code, 404)
        self.assertEqual(self.http.get("/apis/example.com/v1/things").status_code, 404)
        self.assertEqual(self.http.get("/version").json()["gitVersion"], "v1.31.0")

    def test_a_copied_transport_reaches_the_same_store(self):
        # lightkube deep-copies the transport it is given.
        transport = self.store.transport()
        self.assertIs(copy.deepcopy(transport), transport)
        with httpx.Client(transport=copy.deepcopy(transport), base_url="https://stub") as client:
            client.post(f"/api/v1/namespaces/{NS}/configmaps", json=config_map("a"))
        self.store.get(CONFIG_MAP, NS, "a")

    def test_a_delete_over_http_sends_its_propagation_policy(self):
        self.store.create(job("gate"))
        path = f"/apis/batch/v1/namespaces/{NS}/jobs/gate"
        deleted = self.http.delete(path, params={"propagationPolicy": "Background"})
        self.assertEqual(deleted.json()["status"], "Success")
        self.store.create(job("other"))
        body = json.dumps({"propagationPolicy": "Background"})
        self.http.request("DELETE", f"/apis/batch/v1/namespaces/{NS}/jobs/other", content=body)
        self.assertEqual(
            self.store.deletes,
            [Deletion("Job", NS, "gate", "Background"), Deletion("Job", NS, "other", "Background")],
        )

    def test_a_watch_streams_events_and_reports_gone_as_an_error_event(self):
        path = f"/api/v1/namespaces/{NS}/configmaps"
        start = self.store.resource_version
        self.store.create(config_map("a"))
        self.store.create(config_map("b"))
        response = self.http.get(path, params={"watch": "true", "resourceVersion": start})
        lines = [json.loads(line) for line in response.iter_lines()]
        self.assertEqual(
            [(e["type"], e["object"]["metadata"]["name"]) for e in lines],
            [("ADDED", "a"), ("ADDED", "b")],
        )
        self.store.create(config_map("c"))
        self.store.compact()
        [error] = [
            json.loads(line)
            for line in self.http.get(
                path, params={"watch": "1", "resourceVersion": start}
            ).iter_lines()
        ]
        self.assertEqual(
            (error["type"], error["object"]["code"], error["object"]["reason"]),
            ("ERROR", 410, "Expired"),
        )

    def test_apply_over_http_needs_a_field_manager_and_reports_conflicts(self):
        path = f"/apis/{FLEET.group}/{FLEET.version}/namespaces/{NS}/narwhalfleets/fleet"
        apply = {"Content-Type": "application/apply-patch+yaml"}
        body = json.dumps(fleet({"slo": {"ttftS": 4}}))
        self.assertEqual(self.http.patch(path, content=body, headers=apply).status_code, 400)
        first = self.http.patch(path, content=body, headers=apply, params={"fieldManager": "helm"})
        self.assertEqual(first.status_code, 200)
        other = json.dumps(fleet({"slo": {"ttftS": 5}}))
        conflict = self.http.patch(
            path, content=other, headers=apply, params={"fieldManager": "narwhal-cli"}
        )
        self.assertEqual((conflict.status_code, conflict.json()["reason"]), (409, "Conflict"))
        forced = self.http.patch(
            path,
            content=other,
            headers=apply,
            params={"fieldManager": "narwhal-cli", "force": "true"},
        )
        self.assertEqual(forced.json()["spec"]["slo"]["ttftS"], 5)
        yaml_body = (
            "apiVersion: narwhal.example.com/v1alpha1\nkind: NarwhalFleet\n"
            "metadata:\n  name: fleet\nspec:\n  slo:\n    ttftS: 5\n"
        )
        self.assertEqual(
            self.http.patch(
                path, content=yaml_body, headers=apply, params={"fieldManager": "narwhal-cli"}
            ).status_code,
            200,
        )
        status = json.dumps({"status": {"serving": True}})
        reported = self.http.patch(
            f"{path}/status",
            content=status,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        self.assertEqual(reported.json()["status"], {"serving": True})

    def test_a_pods_log_is_served_as_text(self):
        self.store.kubelet = lambda pod: Exit(0, "narwhal-manifest {}\n")
        self.store.create(pod("gate", nodeName="gpu-a", restartPolicy="Never"))
        self.store.settle()
        response = self.http.get(
            f"/api/v1/namespaces/{NS}/pods/gate/log", params={"container": "main"}
        )
        self.assertEqual((response.status_code, response.text), (200, "narwhal-manifest {}\n"))
        self.assertEqual(
            self.http.get(
                f"/api/v1/namespaces/{NS}/pods/gate/log", params={"container": "x"}
            ).status_code,
            400,
        )


@unittest.skipUnless(importlib.util.find_spec("lightkube"), "lightkube is not installed")
class LightkubeTests(StoreTest):
    """The client `runtime/kubernetes_api.py` uses runs against the store unchanged."""

    def test_lightkube_reads_writes_applies_watches_and_deletes(self):
        from lightkube import ApiError as LightkubeError
        from lightkube import Client, KubeConfig
        from lightkube.resources.batch_v1 import Job
        from lightkube.resources.core_v1 import ConfigMap, Pod
        from lightkube.types import CascadeType

        config = KubeConfig.from_dict(
            {
                "clusters": [{"name": "stub", "cluster": {"server": "https://kubernetes.stub"}}],
                "users": [{"name": "controller", "user": {"token": "stub-token"}}],
                "contexts": [
                    {
                        "name": "stub",
                        "context": {"cluster": "stub", "user": "controller", "namespace": NS},
                    }
                ],
                "current-context": "stub",
            }
        )
        client = Client(
            config, transport=self.store.transport(), field_manager="narwhal-controller"
        )
        created = client.create(ConfigMap.from_dict(config_map("a", {"k": "1"})))
        self.assertEqual(client.get(ConfigMap, "a").data, {"k": "1"})
        client.apply(ConfigMap.from_dict(config_map("a", {"k": "1", "j": "2"})), force=True)
        self.assertEqual(
            managers_of(self.store.get(CONFIG_MAP, NS, "a"), "data.j"), {"narwhal-controller"}
        )
        with self.assertRaises(LightkubeError) as raised:
            client.create(ConfigMap.from_dict(config_map("a")))
        self.assertEqual(raised.exception.status.code, 409)
        listed = client.list(ConfigMap)
        self.assertEqual([c.metadata.name for c in listed], ["a"])
        events = []
        for kind, obj in client.watch(ConfigMap, resource_version=created.metadata.resourceVersion):
            events.append((kind, obj.data))
            break
        self.assertEqual(events, [("MODIFIED", {"k": "1", "j": "2"})])
        self.store.compact()
        with self.assertRaises(LightkubeError) as gone:
            next(iter(client.watch(ConfigMap, resource_version=created.metadata.resourceVersion)))
        self.assertEqual(gone.exception.status.code, 410)
        self.store.create(job("gate"))
        self.store.settle()
        client.delete(Job, "gate", cascade=CascadeType.BACKGROUND)
        self.store.settle()
        self.assertEqual(list(client.list(Pod)), [])
        self.assertEqual(self.store.deletes[-1], Deletion("Job", NS, "gate", "Background"))


class RigTests(unittest.TestCase):
    def test_the_task_18_rig_runs_its_gate_jobs_as_store_jobs(self):
        rig = Rig(Path(tempfile.mkdtemp()))
        cluster = rig.cluster(rig.env, rig.root / "logs")
        values = json.loads(Path(rig.env["NARWHAL_KUBERNETES_VALUES"]).read_text())
        values["state"]["claimName"] = "narwhal-state"
        entry = {
            "name": "probe",
            "kind": "fabric-probe",
            "command": ["ip"],
            "deadlineSeconds": 1,
            "node": "gpu-a",
            "keep": "deploy/x/jobs/probe",
        }
        values.update(engines={}, gates=[entry], placement={})
        values["routers"]["enabled"] = False
        cluster.upgrade(Path("chart"), values)
        stored = rig.store.get(JOB, "fleet", "probe")
        self.assertEqual(condition(stored, "Complete")["status"], "True")
        [probe] = cluster.items("pod", "job-name=probe")
        self.assertEqual(probe["spec"]["nodeName"], "gpu-a")
        self.assertEqual(probe["metadata"]["ownerReferences"][0]["uid"], stored["metadata"]["uid"])
        self.assertIn("narwhal-interfaces", cluster.logs("probe"))
        self.assertEqual(rig.jobs["probe"]["entry"], entry)
        # Gate pods are the Jobs', not the workloads the tests count.
        self.assertEqual(list(rig.pods), [])


if __name__ == "__main__":
    unittest.main()
