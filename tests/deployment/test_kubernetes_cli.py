"""`narwhal deploy --target kubernetes` from the workstation, against the stub object store and
the real fleet controller: the fleet it builds and applies, following its status, the export
Jobs that copy the evidence back, `down`, and the RBAC the workstation needs."""

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import yaml

from narwhal.deployment import (
    fleet_resource,
    kubernetes_cli,
    kubernetes_export,
    kubernetes_gate,
    kubernetes_target,
    run_record,
)
from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.deploy import InputError
from narwhal.deployment.fleet_resource import CLI_MANAGER, VolumePolicy
from narwhal.deployment.kubernetes_target import FLEET, ApiCluster
from narwhal.dev import cli
from narwhal.runtime.kubernetes_api import ApiClient, Connection, Kind, Listing
from tests.deployment import chart_fixtures, fleet_fixtures
from tests.deployment.kubernetes_cluster import (
    JOB,
    NAMESPACE,
    NARWHAL_IMAGE,
    POD,
    SERVER,
    STATE_CLAIM,
    Exit,
    StoreTransport,
    Wait,
    _rbac,
)
from tests.deployment.test_helm_chart import CHART, HELM, rules
from tests.deployment.test_kubernetes_controller import ControllerTest, Exited
from tests.deployment.test_kubernetes_objects import policy_refusals

# What a workstation's environment holds beside the fleet's variables, which build leaves out.
WORKSTATION_ONLY = (
    kubernetes_target.FLEET_UID,
    kubernetes_target.ENGINE_ACCOUNT,
    kubernetes_target.SIDECAR_USER,
)


class ApiWorkstation(ApiCluster):
    """The workstation's verbs over the API client, as `KubectlWorkstation` sends them."""

    def apply_fleet(self, fleet: dict, *, dry_run: bool) -> dict:
        return self.client.apply(FLEET, fleet, force=False, dry_run=dry_run)

    def annotate(self, kind: Kind, name: str, annotations: dict) -> dict:
        return self.client.patch(
            kind, name, {"metadata": {"annotations": dict(annotations)}}, patch_type="merge"
        )


def workstation_env(fleet: dict) -> dict[str, str]:
    """The variables a workstation sets for `fleet`, as `spec_env` gives them."""
    env = fleet_resource.spec_env(fleet, state_claim=STATE_CLAIM, policy=VolumePolicy())
    for name in WORKSTATION_ONLY:
        env.pop(name, None)
    return env


class BuildTests(unittest.TestCase):
    def test_build_inverts_spec_env(self):
        fleet = fleet_fixtures.fleet()
        env = workstation_env(fleet)
        built = kubernetes_cli.build(env)
        expected = {k: v for k, v in fleet["spec"].items() if k != "retainRuns"}
        self.assertEqual(built.fleet["spec"], expected)
        self.assertEqual(built.fleet["metadata"], {"name": "fleet", "namespace": NAMESPACE})
        self.assertEqual(built.dropped, ())
        # The controller reads back every variable the workstation set.
        stored = {**built.fleet, "metadata": fleet["metadata"]}
        again = fleet_resource.spec_env(stored, state_claim=STATE_CLAIM, policy=VolumePolicy())
        self.assertEqual({name: again.get(name) for name in env}, env)

    def test_an_engine_with_its_own_ports_takes_them_back_as_its_own(self):
        # Engines that share a node each set their own ports, as on one SSH host.
        fleet = fleet_fixtures.fleet()
        own = {
            "engine": 8101,
            "attestation": 8201,
            "sideChannel": 5601,
            "ucxTcpPortRange": "20100-20199",
        }
        fleet["spec"]["engines"]["nodes"][1]["ports"] = own
        env = workstation_env(fleet)
        self.assertEqual(env["NARWHAL_NODE_2_KV_SIDE_CHANNEL_PORT"], "5601")
        built = kubernetes_cli.build(env)
        self.assertEqual(built.dropped, ())
        engines = built.fleet["spec"]["engines"]
        self.assertEqual(engines["ports"], fleet["spec"]["engines"]["ports"])
        self.assertEqual(engines["nodes"][1]["ports"], own)
        self.assertNotIn("ports", engines["nodes"][0])
        # Only the ports that differ from engines.ports are the engine's own.
        env["NARWHAL_NODE_2_ENGINE_PORT"] = env["NARWHAL_ENGINE_PORT"]
        env["NARWHAL_NODE_2_URL"] = env["NARWHAL_NODE_2_URL"].replace(":8101", ":8100")
        partial = kubernetes_cli.build(env).fleet["spec"]["engines"]["nodes"][1]["ports"]
        self.assertEqual(partial, {k: v for k, v in own.items() if k != "engine"})

    def test_a_variable_without_a_field_is_a_launch_input_unless_the_fleet_refuses_it(self):
        env = workstation_env(fleet_fixtures.fleet())
        env.update(
            NARWHAL_TRANSFER_NET_DEVICES="mlx5_0",
            NARWHAL_HF_TOKEN="token-value",
            NARWHAL_SSH_USER="operator",
            NARWHAL_HOSTS="config/hosts.json",
        )
        built = kubernetes_cli.build(env)
        launch = {item["name"]: item["value"] for item in built.fleet["spec"]["engines"]["launch"]}
        self.assertEqual(launch["NARWHAL_TRANSFER_NET_DEVICES"], "mlx5_0")
        self.assertEqual(built.dropped, ("NARWHAL_HF_TOKEN", "NARWHAL_SSH_USER"))
        # No credential reaches the fleet, which anyone who may read it reads.
        self.assertNotIn("token-value", json.dumps(built.fleet))
        self.assertNotIn("NARWHAL_HOSTS", launch)

    def test_build_names_every_input_a_fleet_cannot_hold(self):
        env = workstation_env(fleet_fixtures.fleet())
        for name in (fleet_resource.HAPROXY_IMAGE_ENV, "NARWHAL_TTFT_S"):
            env.pop(name)
        env["NARWHAL_NODE_2_ENGINE_IMAGE"] = "registry.example/other@sha256:" + "d" * 64
        env["NARWHAL_KUBERNETES_VALUES"] = "values.json"
        env["NARWHAL_NODE_1_URL"] = "http://elsewhere:8100"
        env["NARWHAL_PROFILE_PREFILL_LENS"] = "256,many"
        with self.assertRaises(InputError) as caught:
            kubernetes_cli.build(env, until="monitoring")
        problems = "\n".join(caught.exception.problems)
        self.assertIn("NARWHAL_NODE_2_ENGINE_IMAGE", problems)
        self.assertIn("unset NARWHAL_KUBERNETES_VALUES", problems)
        self.assertIn("NARWHAL_PROFILE_PREFILL_LENS must be whole numbers", problems)
        self.assertIn("--until monitoring", problems)
        env = workstation_env(fleet_fixtures.fleet())
        env.pop(fleet_resource.HAPROXY_IMAGE_ENV)
        env["NARWHAL_NODE_1_URL"] = "http://elsewhere:8100"
        env["NARWHAL_ENGINE_API_KEY"] = "key-value"
        env["NARWHAL_ROUTER_TOKEN"] = "token-value"
        with self.assertRaises(InputError) as caught:
            kubernetes_cli.build(env)
        problems = "\n".join(caught.exception.problems)
        self.assertIn("NARWHAL_ENGINE_API_KEY", problems)
        self.assertIn("NARWHAL_ROUTER_TOKEN holds the router control token", problems)
        env.pop("NARWHAL_ENGINE_API_KEY")
        env.pop("NARWHAL_ROUTER_TOKEN")
        control = env.pop(fleet_resource.CONTROL_TOKEN_SECRET_ENV)
        with self.assertRaises(InputError) as caught:
            kubernetes_cli.build(env)
        self.assertIn(
            f"set {fleet_resource.CONTROL_TOKEN_SECRET_ENV}", "\n".join(caught.exception.problems)
        )
        env[fleet_resource.CONTROL_TOKEN_SECRET_ENV] = control
        with self.assertRaises(InputError) as caught:
            kubernetes_cli.build(env)
        problems = "\n".join(caught.exception.problems)
        self.assertIn(f"set {fleet_resource.HAPROXY_IMAGE_ENV}", problems)
        self.assertIn("NARWHAL_NODE_1_URL: the fleet serves this engine at", problems)

    def test_spec_managers_name_every_writer_of_the_spec_but_the_cli_and_the_controller(self):
        fleet = {
            "metadata": {
                "managedFields": [
                    {"manager": CLI_MANAGER, "operation": "Apply", "fieldsV1": {"f:spec": {}}},
                    {
                        "manager": objects.MANAGER,
                        "operation": "Apply",
                        "subresource": "status",
                        "fieldsV1": {"f:status": {}},
                    },
                    {"manager": "kubectl", "operation": "Update", "fieldsV1": {"f:metadata": {}}},
                ]
            }
        }
        self.assertEqual(kubernetes_cli.spec_managers(fleet), [])
        fleet["metadata"]["managedFields"] += [
            {"manager": "helm", "operation": "Update", "fieldsV1": {"f:spec": {}}},
            {"manager": "argocd-controller", "operation": "Apply", "fieldsV1": {"f:spec": {}}},
            {"manager": CLI_MANAGER, "operation": "Update", "fieldsV1": {"f:spec": {}}},
        ]
        self.assertEqual(
            kubernetes_cli.spec_managers(fleet),
            ["argocd-controller", "helm (Update)", "narwhal-cli (Update)"],
        )


class WorkstationTest(ControllerTest):
    """The workstation runs `narwhal deploy --target kubernetes`; each of its waits lets the
    real controller work, and an export Job's pod runs its program on the state directory."""

    def setUp(self):
        super().setUp()
        self.fleet_template = copy.deepcopy(self.rig.fleet)
        self.fleet_template["metadata"] = {"name": fleet_resource.NAME, "namespace": NAMESPACE}
        self.fleet_template.pop("status", None)
        # The workstation makes the fleet; the rig's own goes.
        self.store.remove(self.fleet_kind, NAMESPACE, fleet_resource.NAME)
        self.env = {**workstation_env(self.fleet_template)}
        self.env.pop("NARWHAL_KUBERNETES_API_KEY_SECRET", None)
        secret = self.fleet_template["spec"]["engines"]["apiKeySecret"]
        self.env["NARWHAL_KUBERNETES_API_KEY_SECRET"] = f"{secret['name']}/{secret['key']}"
        self.run_dir = self.root / "workstation" / "run"
        # Each request the workstation sends, as RBAC authorizes it.
        self.requests: list[tuple[str, str, str]] = []
        transport = StoreTransport(self._workstation)
        self.ws = ApiWorkstation(
            ApiClient(
                Connection(SERVER, NAMESPACE),
                field_manager=CLI_MANAGER,
                transport=transport,
                sleep=lambda seconds: None,
            )
        )
        self.controllers = [self.leader()]
        self.store.kubelet = self.kubelet
        self.exports: list[dict[str, Any]] = []
        self.outcomes: dict[str, Exit] = {}
        # Changes a test makes to an export pod's log.
        self.tamper = None
        # Called on each wait of the workstation instead of the controller, when set.
        self.on_wait = None
        # Changes a test makes to a pod log only the workstation reads.
        self.ws_tamper = None
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(kubernetes_cli, "connect", lambda env, logs: self.ws))
        stack.enter_context(patch.object(kubernetes_cli, "_sleep", self.wait))
        stack.enter_context(patch.object(kubernetes_cli, "_clock", self.store.clock))
        stack.enter_context(patch.dict(os.environ, self.env, clear=True))

    def _workstation(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(_rbac(request))
        response = self.rig._handle(request)
        if self.ws_tamper is not None and request.url.path.endswith("/log"):
            response.read()
            return httpx.Response(response.status_code, content=self.ws_tamper(response.content))
        return response

    def wait(self, seconds: float) -> None:
        self.store.clock.advance(seconds)
        if self.on_wait is not None:
            self.on_wait()
        else:
            with contextlib.suppress(Exited):
                self.work(self.controllers[0])
        self.store.settle()

    def kubelet(self, pod: dict) -> Any:
        labels = pod["metadata"].get("labels") or {}
        if labels.get(fleet_resource.GATE_LABEL) != kubernetes_export.EXPORT:
            return self.rig.kubelet(pod)
        uid = pod["metadata"]["uid"]
        if uid not in self.outcomes:
            self.outcomes[uid] = self.export_pod(pod)
        return self.outcomes[uid]

    def export_pod(self, pod: dict) -> Any:
        """Run the export Job's program on a root that holds only what its mounts name, as
        the kubelet would mount them."""
        [container] = pod["spec"]["containers"]
        root = Path(tempfile.mkdtemp(dir=self.root))
        for mount in container["volumeMounts"]:
            self.assertIs(mount["readOnly"], True)
            source = self.state / mount["subPath"]
            if not source.exists():
                return Wait("CreateContainerConfigError", "failed to create subPath directory")
            relative = Path(mount["mountPath"]).relative_to(kubernetes_export.MOUNT)
            if source.is_dir():
                shutil.copytree(source, root / relative, symlinks=True)
            else:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, root / relative)
        command = container["command"]
        self.assertEqual(command[:4], ["python3", "-I", "-B", "-c"])
        self.assertEqual(command[5], kubernetes_export.MOUNT)
        env = {item["name"]: item["value"] for item in container["env"]}
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", command[4], str(root)],
            env={"PATH": os.environ.get("PATH", "/usr/bin")} | env,
            capture_output=True,
            text=True,
            check=False,
        )
        log = result.stdout
        if self.tamper is not None:
            log = self.tamper(log)
        self.exports.append({"pod": copy.deepcopy(pod), "log": log})
        return Exit(result.returncode, log=log)

    def deploy(self, action: str, *args: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["deploy", action, "--run", str(self.run_dir), "--target", "kubernetes", *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--format", "json"])
        return code, json.loads(out.getvalue()), err.getvalue()

    def live(self) -> dict | None:
        found = list(self.store.live(self.fleet_kind, NAMESPACE))
        return found[0] if found else None

    def mirrored(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.run_dir).as_posix(): path.read_bytes()
            for path in self.run_dir.rglob("*")
            if path.is_file()
        }

    def controller_files(self) -> dict[str, bytes]:
        directory = self.directory()
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def exported(self, index: int = -1) -> set[str]:
        """The files one export Job's log carried."""
        files = kubernetes_target.gate_files(self.exports[index]["log"])
        return set(files)


class RunTests(WorkstationTest):
    def test_run_applies_the_fleet_follows_it_to_ready_and_mirrors_the_evidence(self):
        code, result, err = self.deploy("run")
        self.assertEqual(code, 0, (result, err))
        live = self.live()
        self.assertIsNotNone(live)
        # The fleet is the workstation's apply, as narwhal-cli, and nothing forced.
        managers = {(m["manager"], m["operation"]) for m in live["metadata"]["managedFields"]}
        self.assertIn((CLI_MANAGER, "Apply"), managers)
        self.assertEqual(kubernetes_cli.spec_managers(live), [])
        self.assertEqual(
            live["spec"]["engines"]["nodes"], self.fleet_template["spec"]["engines"]["nodes"]
        )
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")
        data = result["data"]
        self.assertTrue(data["applied"])
        self.assertEqual(data["fleet"]["uid"], live["metadata"]["uid"])
        self.assertTrue(data["evidence"]["complete"])
        # The run directory names the fleet it follows and mirrors the controller's record.
        bound = json.loads((self.run_dir / kubernetes_cli.FLEET_FILE).read_text())
        self.assertEqual(bound["uid"], live["metadata"]["uid"])
        controller = self.controller_files()
        mirrored = self.mirrored()
        self.assertEqual(mirrored["run.json"], controller["run.json"])
        self.assertEqual(data["run_id"], json.loads(controller["run.json"])["run_id"])
        # Every attempt has ended, so the mirror holds each attempt's evidence whole, and of the
        # run directory's root only the record.
        for name, content in controller.items():
            if name.startswith("stages/"):
                self.assertEqual(mirrored.get(name), content, name)
        roots = {name for name in mirrored if "/" not in name}
        local = {kubernetes_cli.FLEET_FILE, kubernetes_cli.EXPORT_FILE, run_record.LOCK}
        self.assertEqual(roots, {"run.json", *local})
        # The workstation never touched a Job: the controller made every export Job.
        self.assertFalse([r for r in self.requests if r[2] == "jobs"], self.requests)
        # Task 18's checks hold: the directory and every file are this user's alone.
        for path in [self.run_dir, *self.run_dir.rglob("*")]:
            mode = stat.S_IMODE(path.lstat().st_mode)
            self.assertEqual(mode & 0o077, 0, path)
        # Each stage's progress was printed as it changed.
        self.assertIn("router-start: passed (1/1 passed)", err)

    def test_a_second_run_applies_nothing_new_and_exports_only_new_attempts(self):
        self.assertEqual(self.deploy("run")[0], 0)
        generation = self.live()["metadata"]["generation"]
        before = len(self.exports)
        code, result, _ = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.live()["metadata"]["generation"], generation)
        carried = set().union(*(self.exported(i) for i in range(before, len(self.exports))))
        # The run directory holds every attempt, so only the record travels again.
        self.assertEqual(carried, {"run.json"})
        held = set(self.mirrored())
        self.env["NARWHAL_TTFT_S"] = "3"
        before = len(self.exports)
        with patch.dict(os.environ, self.env):
            self.assertEqual(self.deploy("run")[0], 0)
        carried = set().union(*(self.exported(i) for i in range(before, len(self.exports))))
        # The edit's new attempts travel, and nothing the run directory held.
        self.assertTrue({name for name in carried if "/fleet-limits/attempt-2/" in name})
        self.assertFalse((carried - {"run.json"}) & held)

    def test_an_apply_that_meets_another_managers_field_is_a_conflict_and_writes_nothing(self):
        self.assertEqual(self.deploy("run")[0], 0)
        generation = self.live()["metadata"]["generation"]
        self.env["NARWHAL_TTFT_S"] = "3"
        handled = self.rig._handle

        def race(request: httpx.Request) -> httpx.Response:
            # Another client edits the SLO between the workstation's read and its apply.
            if request.headers.get("content-type", "").startswith("application/apply-patch"):
                self.store.patch(
                    self.fleet_kind,
                    NAMESPACE,
                    fleet_resource.NAME,
                    {"spec": {"slo": {"ttftS": "5"}}},
                    patch_type="merge",
                    field_manager="kubectl-edit",
                )
            return handled(request)

        with patch.object(self.rig, "_handle", race), patch.dict(os.environ, self.env):
            code, result, _ = self.deploy("run")
        self.assertEqual((code, result["status"]), (4, "error"), result)
        [error] = result["errors"]
        self.assertEqual(error["code"], "apply_conflict")
        self.assertIn(
            {"manager": "kubectl-edit", "field": ".spec.slo.ttftS"}, error["context"]["fields"]
        )
        self.assertEqual(self.live()["spec"]["slo"]["ttftS"], "5")
        self.assertEqual(self.live()["metadata"]["generation"], generation + 1)

    def test_a_fleet_whose_spec_gitops_owns_is_followed_and_never_written(self):
        self.store.create(copy.deepcopy(self.fleet_template), field_manager="helm")
        code, result, err = self.deploy("run")
        self.assertEqual(code, 0, (result, err))
        self.assertFalse(result["data"]["applied"])
        self.assertEqual(result["data"]["managers"], ["helm (Update)"])
        writes = [r for r in self.requests if r[0] in ("apply", "patch", "create", "delete")]
        # The workstation writes nothing of the fleet but its export request.
        fleet_writes = [r for r in writes if r[2] == fleet_resource.PLURAL]
        self.assertTrue(fleet_writes)
        self.assertEqual({r[0] for r in fleet_writes}, {"patch"})
        live = self.live()
        self.assertEqual(live["spec"], {**self.fleet_template["spec"], "retainRuns": 3})
        for entry in live["metadata"]["managedFields"]:
            if entry["manager"] == CLI_MANAGER:
                self.assertEqual(set(entry["fieldsV1"]), {"f:metadata"})
                annotations = entry["fieldsV1"]["f:metadata"]["f:annotations"]
                self.assertEqual(
                    set(annotations) - {"."}, {f"f:{kubernetes_export.EXPORT_REQUEST}"}
                )
        self.assertEqual(kubernetes_cli.spec_managers(self.live()), ["helm (Update)"])
        # down refuses to stop a fleet another manager owns.
        code, result, _ = self.deploy("down")
        self.assertEqual(code, 2, result)
        self.assertIn("helm (Update) own the fleet's spec", result["errors"][0]["message"])
        self.assertFalse(self.live()["spec"]["stopped"])

    def test_a_failed_stage_stalls_the_run_with_its_recovery_and_mirrored_evidence(self):
        self.rig.fail_kind = "engine-check"
        code, result, err = self.deploy("run")
        self.assertEqual((code, result["status"]), (1, "failed_gate"), (result, err))
        [error] = result["errors"]
        self.assertEqual(error["code"], "gate_failed")
        failed = result["data"]["failed_stage"]
        self.assertTrue(failed.startswith("engine-check["), failed)
        self.assertIn("/deploy/Kubernetes/", error["context"]["recovery"])
        record = json.loads((self.run_dir / "run.json").read_text())
        evidence = self.run_dir / record["stages"][failed]["directory"]
        self.assertTrue(evidence.is_dir())
        self.assertIn(f"Evidence: {evidence}", err)
        self.assertEqual(self.condition(fleet_resource.STALLED)["status"], "True")

    def test_a_run_of_a_stalled_fleet_asks_for_a_retry_and_waits_out_the_old_stall(self):
        self.rig.fail_kind = "engine-check"
        self.assertEqual(self.deploy("run")[0], 1)
        stall = self.condition(fleet_resource.STALLED)
        self.rig.fail_kind = None
        code, result, err = self.deploy("run")
        self.assertEqual(code, 0, (result, err))
        annotations = self.live()["metadata"]["annotations"]
        self.assertIn(fleet_resource.REQUESTED_AT, annotations)
        self.assertNotEqual(self.condition(fleet_resource.STALLED), stall)
        self.assertEqual(self.condition(fleet_resource.READY)["status"], "True")

    def test_a_stall_on_a_refused_input_is_invalid_input(self):
        # The cluster admin allows only a PersistentVolumeClaim; the fleet names a host path.
        self.env[kubernetes_target.MODEL_VOLUME] = json.dumps(
            {"hostPath": {"path": "/srv/models", "type": "Directory"}}
        )
        with patch.dict(os.environ, self.env):
            code, result, err = self.deploy("run")
        self.assertEqual((code, result["status"]), (2, "invalid_input"), (result, err))
        self.assertIn("InputRefused", result["errors"][0]["message"])

    def test_following_ends_at_the_timeout_and_leaves_the_fleet_to_the_controller(self):
        self.on_wait = lambda: None
        code, result, _ = self.deploy("run", "--timeout", "30")
        self.assertEqual((code, result["status"]), (4, "error"), result)
        self.assertEqual(result["errors"][0]["code"], "follow_timeout")
        self.assertIsNotNone(self.live())

    def test_a_run_directory_follows_one_fleet(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.store.remove(self.fleet_kind, NAMESPACE, fleet_resource.NAME)
        self.store.create(copy.deepcopy(self.fleet_template), field_manager="helm")
        for action in ("run", "status", "down"):
            code, result, _ = self.deploy(action)
            self.assertEqual(code, 2, (action, result))
            self.assertIn("start a new run directory", result["errors"][0]["message"])

    def test_options_of_the_cluster_are_refused_on_ssh_hosts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(
                ["deploy", "down", "--run", str(self.run_dir), "--delete", "--format", "json"]
            )
        self.assertEqual(code, 2)
        self.assertIn(
            "--delete follows a Kubernetes", json.loads(out.getvalue())["errors"][0]["message"]
        )


class PlanTests(WorkstationTest):
    def test_plan_dry_runs_the_apply_and_starts_no_job(self):
        code, result, err = self.deploy("plan")
        self.assertEqual(code, 0, (result, err))
        self.assertIsNone(self.live())
        self.assertFalse(self.store.live(JOB, NAMESPACE))
        self.assertTrue(all(row["runs"] for row in result["data"]["stages"]))
        self.assertFalse(self.run_dir.exists())

    def test_plan_reruns_only_what_an_slo_edit_reruns(self):
        self.assertEqual(self.deploy("run")[0], 0)
        jobs = len(self.store.live(JOB, NAMESPACE))
        code, result, _ = self.deploy("plan")
        self.assertEqual(code, 0, result)
        self.assertEqual([row["stage"] for row in result["data"]["stages"] if row["runs"]], [])
        self.env["NARWHAL_TTFT_S"] = "3"
        with patch.dict(os.environ, self.env):
            code, result, _ = self.deploy("plan")
        self.assertEqual(code, 0, result)
        ran = [row["stage"] for row in result["data"]["stages"] if row["runs"]]
        self.assertEqual(ran, ["fleet-limits", "preflight", "router-start"])
        self.assertEqual(len(self.store.live(JOB, NAMESPACE)), jobs)
        self.assertEqual(self.live()["spec"]["slo"]["ttftS"], "4")


class PlanResultTests(WorkstationTest):
    def test_a_dry_run_that_meets_another_managers_field_is_an_apply_conflict(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.env["NARWHAL_TTFT_S"] = "3"
        handled = self.rig._handle

        def race(request: httpx.Request) -> httpx.Response:
            if request.headers.get("content-type", "").startswith("application/apply-patch"):
                self.store.patch(
                    self.fleet_kind,
                    NAMESPACE,
                    fleet_resource.NAME,
                    {"spec": {"slo": {"ttftS": "5"}}},
                    patch_type="merge",
                    field_manager="kubectl-edit",
                )
            return handled(request)

        with patch.object(self.rig, "_handle", race), patch.dict(os.environ, self.env):
            code, result, _ = self.deploy("plan")
        self.assertEqual((code, result["status"]), (4, "error"), result)
        [error] = result["errors"]
        self.assertEqual(error["code"], "apply_conflict")
        self.assertIn(
            {"manager": "kubectl-edit", "field": ".spec.slo.ttftS"}, error["context"]["fields"]
        )

    def test_each_plan_into_a_run_directory_logs_apart_from_the_last(self):
        self.assertEqual(self.deploy("run")[0], 0)
        seen: list[Path] = []

        def connect(env: dict, logs: Path) -> ApiWorkstation:
            seen.append(logs)
            return self.ws

        with patch.object(kubernetes_cli, "connect", connect):
            for _ in range(2):
                self.assertEqual(self.deploy("plan")[0], 0)
        self.assertEqual(len(set(seen)), 2)
        for logs in seen:
            self.assertEqual(logs.parent, self.run_dir.resolve() / kubernetes_cli.KUBECTL_LOGS)

    def test_plan_checks_the_run_directory_before_it_writes_there(self):
        self.run_dir.mkdir(parents=True, mode=0o700)
        self.run_dir.chmod(0o777)
        code, result, _ = self.deploy("plan")
        self.assertEqual(code, 2, result)
        self.assertFalse((self.run_dir / kubernetes_cli.KUBECTL_LOGS).exists())


class ExportTests(WorkstationTest):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.deploy("run")[0], 0)

    def session(self) -> kubernetes_cli.Session:
        session = kubernetes_cli.Session(
            self.run_dir.resolve(), self.env, announce=lambda line: None
        )
        return session

    def export_jobs(self) -> list[dict]:
        return [
            w
            for w in self.rig.workloads
            if w["kind"] == "Job"
            and w["metadata"]["labels"].get(fleet_resource.GATE_LABEL) == kubernetes_export.EXPORT
        ]

    def forget(self, attempt: str) -> None:
        """Make the run directory hold `attempt` no longer, as a fresh workstation would."""
        shutil.rmtree(self.run_dir / attempt, ignore_errors=True)
        path = self.run_dir / kubernetes_cli.EXPORT_FILE
        state = json.loads(path.read_text())
        parent, _, number = attempt.rpartition("/attempt-")
        state["held"][parent].remove(int(number))
        path.write_text(json.dumps(state))

    def test_the_controller_makes_each_export_job_from_its_own_image_and_mounts_no_secret(self):
        jobs = self.export_jobs()
        self.assertTrue(jobs)
        live = self.live()
        uid = live["metadata"]["uid"]
        runs = f"runs/{NAMESPACE}/{uid}"
        for job in jobs:
            # The fleet owns it, so it goes with the fleet; stopping the fleet leaves it be.
            [owner] = job["metadata"]["ownerReferences"]
            self.assertEqual((owner["uid"], owner["controller"]), (uid, False))
            pod = job["spec"]["template"]["spec"]
            self.assertIs(pod["automountServiceAccountToken"], False)
            self.assertNotIn("serviceAccountName", pod)
            self.assertNotIn("hostNetwork", pod)
            [volume] = pod["volumes"]
            self.assertEqual(
                volume["persistentVolumeClaim"], {"claimName": STATE_CLAIM, "readOnly": True}
            )
            [container] = pod["containers"]
            # The controller's own image.
            self.assertEqual(container["image"], NARWHAL_IMAGE)
            for mount in container["volumeMounts"]:
                self.assertIs(mount["readOnly"], True)
                relative = mount["subPath"].removeprefix(runs + "/")
                self.assertNotEqual(relative, mount["subPath"])
                # The record, or one attempt's directory: never the root, the salt or the HMACs.
                self.assertTrue(
                    relative == "run.json"
                    or kubernetes_export.INSTANCE_DIRECTORY.fullmatch(
                        relative.rpartition("/attempt-")[0]
                    ),
                    relative,
                )
            self.assertIs(container["securityContext"]["readOnlyRootFilesystem"], True)
            self.assertIs(pod["securityContext"]["runAsNonRoot"], True)
            self.assertFalse([e for e in container["env"] if "valueFrom" in e])
            self.assertLessEqual(
                job["spec"]["activeDeadlineSeconds"], kubernetes_export.EXPORT_SECONDS
            )
            # The admission policy admits it from the controller, and from no one else.
            self.assertEqual(policy_refusals(job, state_claim=STATE_CLAIM), [])
            self.assertIn(
                "state claim",
                policy_refusals(job, requester="user:workstation", state_claim=STATE_CLAIM),
            )
        # The status names each request's Job under its nonce.
        answers = self.status()["exports"]
        self.assertTrue(all(kubernetes_export.NONCE.fullmatch(a["nonce"]) for a in answers))
        self.assertLessEqual({a["job"] for a in answers}, {j["metadata"]["name"] for j in jobs})

    def test_no_export_reaches_the_salts_or_the_key_checks_hmacs(self):
        controller = self.controller_files()
        self.assertIn(kubernetes_target.CREDENTIAL_FILE, controller)
        salt = json.loads(controller[kubernetes_target.CREDENTIAL_FILE])["salt"]
        self.assertNotIn(kubernetes_target.CREDENTIAL_FILE, self.mirrored())
        self.assertIn(run_record.SALT, controller)
        self.assertNotIn(run_record.SALT, self.mirrored())
        key = controller[run_record.SALT]
        for export in self.exports:
            self.assertNotIn(base64.b64encode(key).decode(), export["log"])
            self.assertNotIn(salt, export["log"])
            mounts = export["pod"]["spec"]["containers"][0]["volumeMounts"]
            for mount in mounts:
                for secret in (run_record.SALT, kubernetes_target.CREDENTIAL_FILE):
                    self.assertFalse(mount["subPath"].endswith(secret), mount)
                self.assertNotRegex(mount["subPath"], r"^runs/[^/]+/[^/]+$")

    def test_each_export_job_stays_within_its_budget_and_the_next_carries_the_rest(self):
        record = (self.directory() / "run.json").stat().st_size
        budget = kubernetes_export.EXPORT_RESERVE + 4 * record // 3 + 1024 + 48 * 1024
        attempt = "stages/access/attempt-1"
        # Larger than any log of the budget can carry.
        (self.directory() / attempt / "large.bin").write_bytes(os.urandom(budget))
        parts = {f"{attempt}/part-{n}.bin": os.urandom(12 * 1024) for n in range(6)}
        for name, data in parts.items():
            (self.directory() / name).write_bytes(data)
        self.forget(attempt)
        before = len(self.exports)
        with patch.object(kubernetes_export, "EXPORT_LOG_BYTES", budget):
            done = self.session().export()
        logs = [e["log"] for e in self.exports[before:]]
        self.assertGreater(len(logs), 1)
        self.assertTrue(all(len(log.encode()) <= budget for log in logs))
        for name, data in parts.items():
            self.assertEqual((self.run_dir / name).read_bytes(), data)
        # A file no export Job's log can hold stays in the cluster, and the result names it.
        self.assertFalse((self.run_dir / attempt / "large.bin").exists())
        self.assertEqual(done.large, [f"{attempt}/large.bin"])
        self.assertTrue(done.data()["complete"])
        # The rest of the run directory travelled no second time.
        carried = set().union(*(self.exported(i) for i in range(before, len(self.exports))))
        self.assertLessEqual(carried, {"run.json", *parts, f"{attempt}/access.json"})

    def test_an_export_job_mounts_at_most_its_attempts_and_the_next_carries_on(self):
        controller = {n: d for n, d in self.controller_files().items() if n.startswith("stages/")}
        shutil.rmtree(self.run_dir / "stages")
        (self.run_dir / kubernetes_cli.EXPORT_FILE).unlink()
        before = len(self.exports)
        with patch.object(kubernetes_export, "MAX_ATTEMPTS", 3):
            done = self.session().export()
        self.assertTrue(done.data()["complete"])
        pods = [e["pod"] for e in self.exports[before:]]
        self.assertGreater(len(pods), 3)
        for pod in pods:
            self.assertLessEqual(len(pod["spec"]["containers"][0]["volumeMounts"]), 4)
        mirrored = {n: d for n, d in self.mirrored().items() if n.startswith("stages/")}
        self.assertEqual(mirrored, controller)

    def test_an_export_carries_only_the_attempts_the_run_directory_lacks(self):
        before = len(self.exports)
        self.session().export()
        self.assertEqual(self.exported(), {"run.json"})
        # A held attempt is never read again; one the run directory lacks is.
        attempt = "stages/access/attempt-1"
        (self.directory() / attempt / "late.json").write_text("{}")
        self.session().export()
        self.assertEqual(self.exported(), {"run.json"})
        self.forget(attempt)
        self.session().export()
        self.assertEqual(
            self.exported(), {"run.json", f"{attempt}/access.json", f"{attempt}/late.json"}
        )
        self.assertEqual(len(self.exports), before + 3)

    def test_a_running_attempt_waits_until_it_ends(self):
        record = json.loads((self.directory() / "run.json").read_text())
        entry = record["stages"]["access"]
        entry["status"] = run_record.RUNNING
        held = kubernetes_export.Request("0" * 32, {}, "")
        self.assertNotIn(
            entry["directory"], kubernetes_export.wanted(record, held, self.directory())
        )
        entry["status"] = run_record.INTERRUPTED
        self.assertNotIn(
            entry["directory"], kubernetes_export.wanted(record, held, self.directory())
        )
        entry["status"] = run_record.FAILED
        self.assertIn(entry["directory"], kubernetes_export.wanted(record, held, self.directory()))

    def late_attempt(self) -> str:
        """A file the controller's run directory holds and the workstation's does not."""
        attempt = "stages/access/attempt-1"
        (self.directory() / attempt / "late.json").write_text('{"a": 1}')
        self.forget(attempt)
        return f"{attempt}/late.json"

    def test_the_controller_names_the_pod_it_read_and_its_logs_digest(self):
        self.late_attempt()
        self.exports.clear()
        self.session().export()
        [run] = self.exports
        log = run["log"].encode()
        [entry] = [e for e in self.live()["status"]["exports"] if e.get("pod")][-1:]
        self.assertEqual(entry["pod"], run["pod"]["metadata"]["name"])
        self.assertEqual(entry["sha256"], hashlib.sha256(log).hexdigest())
        self.assertEqual(entry["size"], len(log))

    def test_a_log_other_than_the_one_the_controller_read_writes_nothing(self):
        name = self.late_attempt()
        before = self.mirrored()

        def forged(log: bytes) -> bytes:
            files = {**kubernetes_target.gate_files(log.decode()), name: b'{"a": 2}'}
            lines = [
                r for r in log.decode().splitlines() if r.startswith(kubernetes_export.SUMMARY)
            ]
            body = "".join(
                f"{kubernetes_gate.FILE}{n} {base64.b64encode(d).decode()}\n"
                for n, d in files.items()
            )
            manifest = {
                n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
                for n, d in files.items()
            }
            text = body + lines[-1] + "\n" + kubernetes_gate.MANIFEST + json.dumps(manifest)
            return (text + "\n").encode()

        # The log the workstation reads holds a whole, consistent manifest; only its digest
        # tells it from the log the controller read.
        self.ws_tamper = forged
        with self.assertRaisesRegex(kubernetes_cli.ExportError, "differs from the one the"):
            self.session().export()
        self.assertEqual(self.mirrored(), before)
        self.assertFalse((self.run_dir / name).exists())

    def test_a_second_pod_that_claims_the_export_job_writes_nothing(self):
        name = self.late_attempt()
        before = self.mirrored()
        controller = self.controllers[0]
        listed = controller.client.list

        def with_forgery(kind: Kind, **options: Any) -> Listing:
            # A pod made under the Job's name and labels, with its controller reference, as
            # the chart's admission policy refuses; this store has no admission policy.
            found = listed(kind, **options)
            if kind.kind != "Pod" or not found.items:
                return found
            forged = copy.deepcopy(found.items[0])
            forged["metadata"]["name"] += "-forged"
            forged["metadata"]["uid"] = "00000000-0000-4000-8000-0000000000f0"
            return Listing([forged, *found.items], found.resource_version)

        with (
            patch.object(controller.client, "list", with_forgery),
            self.assertRaisesRegex(kubernetes_cli.ExportError, "more than one pod claims"),
        ):
            self.session().export()
        self.assertEqual(self.mirrored(), before)
        self.assertFalse((self.run_dir / name).exists())

    def test_a_tampered_log_writes_nothing(self):
        attempt = "stages/access/attempt-1"
        (self.directory() / attempt / "late.json").write_text('{"a": 1}')
        self.forget(attempt)
        before = self.mirrored()
        name = f"{attempt}/late.json"

        def altered(log: str) -> str:
            line = next(
                row for row in log.splitlines() if row.startswith(kubernetes_gate.FILE + name)
            )
            forged = kubernetes_gate.FILE + name + " " + base64.b64encode(b'{"a": 2}').decode()
            return log.replace(line, forged)

        self.tamper = altered
        with self.assertRaisesRegex(kubernetes_cli.ExportError, "other than its manifest"):
            self.session().export()
        self.assertEqual(self.mirrored(), before)
        # status reports the refusal and the mirror as it was.
        _, result, _ = self.deploy("status")
        self.assertIn("other than its manifest", result["data"]["evidence"]["error"])
        self.assertFalse((self.run_dir / name).exists())

    def forge(self, extra: dict[str, bytes]) -> None:
        """Have the next export pods return `extra` beside what they read, with a manifest
        that matches."""

        def forged(log: str) -> str:
            files = {**kubernetes_target.gate_files(log), **extra}
            lines = [row for row in log.splitlines() if row.startswith(kubernetes_export.SUMMARY)]
            body = "".join(
                f"{kubernetes_gate.FILE}{n} {base64.b64encode(d).decode()}\n"
                for n, d in files.items()
            )
            manifest = {
                n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
                for n, d in files.items()
            }
            return body + lines[-1] + "\n" + kubernetes_gate.MANIFEST + json.dumps(manifest) + "\n"

        self.tamper = forged

    def test_a_log_that_returns_a_workstation_file_is_refused_whatever_its_case(self):
        for name in (kubernetes_cli.FLEET_FILE, "Kubernetes-Fleet.json", "KUBECTL/x.log"):
            with self.subTest(name=name):
                self.forge({name: b'{"uid": "x"}'})
                with self.assertRaisesRegex(kubernetes_cli.ExportError, "which no export writes"):
                    self.session().export()
                self.assertFalse(
                    (self.run_dir / name).exists() and name != kubernetes_cli.FLEET_FILE
                )
        bound = json.loads((self.run_dir / kubernetes_cli.FLEET_FILE).read_text())
        self.assertNotEqual(bound["uid"], "x")

    def test_a_name_with_an_empty_or_dot_part_is_refused(self):
        for name in (".", "stages//x", "stages/access/attempt-1/./x", "stages/access/attempt-1/"):
            with self.subTest(name=name):
                self.assertFalse(kubernetes_cli.exportable(name))
                with self.assertRaisesRegex(kubernetes_cli.ExportError, "no file an export writes"):
                    self.session()._write({name: b"x"})
        self.assertTrue(kubernetes_cli.exportable("stages/access/attempt-1/.env.engine-1"))

    def test_an_export_never_replaces_evidence_the_run_directory_holds(self):
        name = next(n for n in self.mirrored() if n.startswith("stages/") and n.endswith(".json"))
        held = (self.run_dir / name).read_bytes()
        self.forge({name: b'{"forged": true}'})
        with self.assertRaisesRegex(kubernetes_cli.ExportError, "never replaces it"):
            self.session().export()
        self.assertEqual((self.run_dir / name).read_bytes(), held)
        # The same bytes again write nothing and refuse nothing.
        self.session()._write({name: held})

    def test_the_controller_answers_a_refused_request_with_its_reason(self):
        for value in (
            json.dumps({"nonce": "a" * 32, "held": {"/etc": [1]}}),
            json.dumps({"nonce": "b" * 32, "after": "../run.salt"}),
        ):
            with self.subTest(value=value):
                self.store.patch(
                    self.fleet_kind,
                    NAMESPACE,
                    fleet_resource.NAME,
                    {"metadata": {"annotations": {kubernetes_export.EXPORT_REQUEST: value}}},
                    patch_type="merge",
                )
                jobs = len(self.export_jobs())
                self.work(self.controllers[0])
                nonce = json.loads(value)["nonce"]
                [answer] = [a for a in self.status()["exports"] if a["nonce"] == nonce]
                self.assertIn(kubernetes_export.EXPORT_REQUEST, answer["error"])
                self.assertEqual(len(self.export_jobs()), jobs)

    def test_no_export_job_mounts_a_run_directory_the_controller_has_not_made(self):
        self.store.remove(self.fleet_kind, NAMESPACE, fleet_resource.NAME)
        fresh = self.root / "fresh"
        self.run_dir = fresh
        self.on_wait = lambda: None
        before = len(self.exports)
        code, result, _ = self.deploy("run", "--timeout", "10")
        self.assertEqual(code, 4, result)
        self.assertEqual(result["data"]["evidence"], kubernetes_cli.Exported().data())
        self.assertEqual(len(self.exports), before)


class StatusTests(WorkstationTest):
    def test_status_reports_the_fleet_the_mirrored_stages_and_the_live_checks(self):
        self.assertEqual(self.deploy("run")[0], 0)
        code, result, err = self.deploy("status")
        self.assertEqual(code, 0, (result, err))
        data = result["data"]
        self.assertEqual(
            data["fleet"]["conditions"][0],
            {
                "type": "Ready",
                "status": "True",
                "reason": "Passed",
                "message": data["fleet"]["conditions"][0]["message"],
            },
        )
        self.assertTrue(all(row["status"] == "passed" for row in data["stages"]))
        self.assertTrue(data["live"])
        self.assertTrue(all(check["state"] in ("holds", "not observed") for check in data["live"]))
        # A replaced engine pod shows in the live checks.
        pod = self.store.get(POD, NAMESPACE, "engine-1")
        self.store.put({**pod, "metadata": {**pod["metadata"], "uid": "replaced"}})
        code, result, _ = self.deploy("status")
        self.assertEqual((code, result["status"]), (3, "degraded"), result)

    def test_status_of_a_stalled_fleet_is_a_failed_gate(self):
        self.rig.fail_kind = "engine-check"
        self.assertEqual(self.deploy("run")[0], 1)
        code, result, _ = self.deploy("status")
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)

    def test_status_of_a_fleet_stalled_on_a_refused_input_is_invalid_input_as_for_run(self):
        self.env[kubernetes_target.MODEL_VOLUME] = json.dumps(
            {"hostPath": {"path": "/srv/models", "type": "Directory"}}
        )
        with patch.dict(os.environ, self.env):
            self.assertEqual(self.deploy("run")[0], 2)
            code, result, _ = self.deploy("status")
        self.assertEqual((code, result["status"]), (2, "invalid_input"), result)
        self.assertIn("InputRefused", result["errors"][0]["message"])


class DownTests(WorkstationTest):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.deploy("run")[0], 0)

    def test_down_stops_the_fleet_and_keeps_it(self):
        code, result, err = self.deploy("down")
        self.assertEqual(code, 0, (result, err))
        live = self.live()
        self.assertTrue(live["spec"]["stopped"])
        self.assertEqual(kubernetes_cli.spec_managers(live), [])
        self.assertEqual(self.condition(fleet_resource.STOPPED)["status"], "True")
        data = result["data"]
        self.assertEqual((data["stopped"], data["deleted"]), (True, False))
        self.assertTrue(any(p["stopped"] for p in data["processes"]))
        self.assertFalse(self.derived())
        # run starts it again from the record.
        code, result, _ = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertFalse(self.live()["spec"]["stopped"])

    def test_down_with_delete_deletes_the_fleet_in_the_background(self):
        uid = self.live()["metadata"]["uid"]
        code, result, err = self.deploy("down", "--delete")
        self.assertEqual(code, 0, (result, err))
        self.assertTrue(result["data"]["deleted"])
        self.assertIsNone(self.live())
        [deletion] = [d for d in self.store.deletes if d.kind == fleet_resource.KIND]
        self.assertEqual(deletion.propagation, "Background")
        self.assertIn((fleet_resource.NAME, uid), self.rig.preconditions)
        # The run directory keeps the mirror, and down again finds nothing to stop.
        self.assertTrue((self.run_dir / "run.json").is_file())
        code, result, _ = self.deploy("down")
        self.assertEqual(code, 0, result)

    def test_down_with_delete_keeps_a_fleet_that_did_not_stop_in_time(self):
        self.on_wait = lambda: None
        code, result, err = self.deploy("down", "--delete", "--timeout", "30")
        self.assertEqual((code, result["status"]), (4, "error"), (result, err))
        self.assertEqual(result["errors"][0]["code"], "follow_timeout")
        self.assertIn("did not delete it", result["errors"][0]["message"])
        self.assertFalse(result["data"]["deleted"])
        self.assertIsNotNone(self.live())
        self.assertFalse([d for d in self.store.deletes if d.kind == fleet_resource.KIND])


class RbacTests(WorkstationTest):
    def granted(self) -> set[tuple[str, str, str]]:
        return {
            (verb, group, resource)
            for rule in (*kubernetes_cli.FLEET_RULES, *kubernetes_cli.WORKSTATION_RULES)
            for verb in rule["verbs"]
            for group in rule["apiGroups"]
            for resource in rule["resources"]
        }

    def test_the_workstation_uses_only_the_verbs_its_rules_grant(self):
        self.assertEqual(self.deploy("plan")[0], 0)
        self.assertEqual(self.deploy("run")[0], 0)
        self.env["NARWHAL_TTFT_S"] = "3"
        with patch.dict(os.environ, self.env):
            self.assertEqual(self.deploy("plan")[0], 0)
        self.assertEqual(self.deploy("status")[0], 0)
        self.assertEqual(self.deploy("down", "--delete")[0], 0)
        used = set()
        for verb, group, resource in self.requests:
            for needed in kubernetes_target.VERBS[verb]:
                used.add((needed, group, resource))
        # A server-side apply of an existing fleet needs only patch; of a new one, create.
        self.assertLessEqual(used, self.granted())
        self.assertEqual(self.granted() - used, set())
        resources = {resource for _, _, resource in self.requests}
        self.assertFalse(
            {"jobs", "secrets", "roles", "rolebindings", "pods/exec", "configmaps"} & resources
        )
        # The documented role grants nothing that starts a pod.
        self.assertFalse(
            {(v, r) for v, _, r in self.granted() if v in ("create", "patch", "update")}
            - {(v, fleet_resource.PLURAL) for v in ("create", "patch")}
        )
        self.assertNotIn(f"{fleet_resource.PLURAL}/status", resources)

    @unittest.skipUnless(HELM, "helm is not installed")
    def test_the_fleet_rules_lie_within_the_charts_fleet_editor_role(self):
        values = self.root / "values.json"
        values.write_text(json.dumps(chart_fixtures.values()))
        rendered = subprocess.run(
            [HELM, "template", "fleet", str(CHART), "--namespace", NAMESPACE, "-f", str(values)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        [role] = [
            d
            for d in yaml.safe_load_all(rendered)
            if d and d["kind"] == "Role" and d["metadata"]["name"] == "fleet-fleet-editor"
        ]
        editor = rules(role)
        wanted = {
            (group, resource, verb, "")
            for rule in kubernetes_cli.FLEET_RULES
            for group in rule["apiGroups"]
            for resource in rule["resources"]
            for verb in rule["verbs"]
        }
        self.assertLessEqual(wanted, editor)


class KubectlWorkstationTests(unittest.TestCase):
    """The workstation's verbs as kubectl takes them, and kubectl's refusals as the API's."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.calls = self.root / "calls"
        self.kubectl = self.root / "kubectl"
        self.answer("{}")

    def answer(self, stdout: str, stderr: str = "", status: int = 0) -> None:
        (self.root / "stdout").write_text(stdout)
        (self.root / "stderr").write_text(stderr)
        script = (
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            f"cat > /dev/null\ncat {self.root / 'stdout'}\n"
            f"cat {self.root / 'stderr'} >&2\nexit {status}\n"
        )
        self.kubectl.write_text(script)
        self.kubectl.chmod(stat.S_IRWXU)

    def workstation(self) -> kubernetes_cli.KubectlWorkstation:
        env = {
            kubernetes_target.NAMESPACE: NAMESPACE,
            kubernetes_target.KUBECTL: str(self.kubectl),
            kubernetes_target.CONTEXT: "admin@cluster",
        }
        return kubernetes_cli.KubectlWorkstation(env, self.root / "logs")

    def last(self) -> str:
        return self.calls.read_text().splitlines()[-1]

    def test_each_command_logs_its_kubectl_calls_apart_from_the_last_ones(self):
        env = {
            kubernetes_target.NAMESPACE: NAMESPACE,
            kubernetes_target.KUBECTL: str(self.kubectl),
        }
        run = self.root / "run"
        for _ in range(2):
            session = kubernetes_cli.Session(run, env)
            session.live()
        logs = sorted((run / kubernetes_cli.KUBECTL_LOGS).glob("*/*.log"))
        self.assertEqual(len(logs), 2)
        self.assertEqual(len({log.parent for log in logs}), 2)

    def test_the_fleet_is_applied_as_the_cli_never_forced_and_read_with_its_managers(self):
        ws = self.workstation()
        ws.apply_fleet({"kind": "NarwhalFleet"}, dry_run=False)
        self.assertIn(
            f"apply --context admin@cluster --namespace {NAMESPACE} --server-side "
            f"--field-manager={CLI_MANAGER} --filename -",
            self.last(),
        )
        self.assertNotIn("--force-conflicts", self.last())
        ws.apply_fleet({"kind": "NarwhalFleet"}, dry_run=True)
        self.assertIn("--dry-run=server", self.last())
        ws.get(FLEET, "fleet")
        self.assertIn("--show-managed-fields", self.last())
        ws.annotate(FLEET, "fleet", {fleet_resource.REQUESTED_AT: "now"})
        self.assertIn(f"--type merge --field-manager={CLI_MANAGER}", self.last())

    def test_a_conflict_names_its_managers_and_fields_and_a_refusal_is_invalid(self):
        ws = self.workstation()
        self.answer(
            "",
            'error: Apply failed with 2 conflicts: conflict with "helm" using '
            f'{fleet_resource.API_VERSION}: .spec.slo.ttftS\nconflict with "argocd-controller" '
            f"using {fleet_resource.API_VERSION}: .spec.routers.size\nPlease review the fields "
            "above--they currently have other managers.\n",
            1,
        )
        session = kubernetes_cli.Session(self.root / "run", {}, announce=lambda line: None)
        session.ws = ws
        with self.assertRaises(kubernetes_cli.FleetConflict) as caught:
            session.apply({"kind": "NarwhalFleet"})
        self.assertEqual(
            caught.exception.fields,
            [("helm", ".spec.slo.ttftS"), ("argocd-controller", ".spec.routers.size")],
        )
        self.answer("", 'The NarwhalFleet "fleet" is invalid: spec.until: Unsupported value', 1)
        with self.assertRaises(kubernetes_target.ClusterError) as refused:
            ws.apply_fleet({"kind": "NarwhalFleet"}, dry_run=True)
        self.assertEqual(refused.exception.code, 422)


if __name__ == "__main__":
    unittest.main()
