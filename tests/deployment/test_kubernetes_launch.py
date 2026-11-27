"""A Kubernetes launch plan, its engine pod's steps and the check of the pod's records."""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import load as load_backend
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine import cli as engine_cli
from narwhal.deployment.launch_engine import kubernetes, pod
from narwhal.deployment.launch_engine.check import read_checked
from narwhal.deployment.launch_engine.plan import build
from tests.backends.contract import fixtures, launch_record, prepared
from tests.deployment.fixtures import launch_document, launcher_inputs
from tests.deployment.pod_fixtures import (
    ENGINE_PROCESS,
    ENGINE_TICKS,
    POD_IP,
    RDMA_TRANSFER,
    SNAPSHOT,
    EnginePod,
    kubernetes_env,
    printing,
)


class KubernetesLaunchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.fixtures = fixtures("vllm")
        self.output = None

    def prepared(self, name: str = "plan", **environment: str) -> tuple[Path, dict]:
        root = self.root / name
        root.mkdir()
        run, plan, _ = prepared(
            root,
            load_backend("vllm").launcher(),
            self.fixtures,
            backend="kubernetes",
            **environment,
        )
        return run, plan

    def pod(self, run: Path, name: str = "pod", **allocation) -> EnginePod:
        return EnginePod(self.root / name, run, **allocation)

    def check(self, run: Path, plan: dict, engine: EnginePod) -> str:
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            kubernetes.check(run, plan, engine.state)
        return printed.getvalue()

    def started(self) -> tuple[Path, dict, EnginePod]:
        run, plan = self.prepared()
        engine = self.pod(run)
        engine.start(self.fixtures.check_output(plan))
        return run, plan, engine

    def test_the_plan_takes_its_devices_and_address_from_the_pod(self):
        run, plan = self.prepared()
        values = (run / "container.env").read_text().splitlines()
        self.assertNotIn("CUDA_VISIBLE_DEVICES", "".join(values))
        self.assertNotIn("UCX_NET_DEVICES", "".join(values))
        self.assertIn("NIXL_HOST_IP=$(NARWHAL_POD_IP)", values)
        self.assertEqual(plan["ucx_tls"], "rc,sm,self,cuda_copy")
        self.assertIn("UCX_SHM_DEVICES=none", values)
        self.assertNotIn("model_status_sha256", plan)
        self.assertFalse((run / "cache").exists())
        engine = self.pod(run)
        environment = engine.environment("engine")
        self.assertEqual(environment["NIXL_HOST_IP"], POD_IP)
        self.assertEqual(environment["VLLM_API_KEY"], "engine-only-secret")
        spec = engine.manifests["podSpec"]
        self.assertTrue(spec["hostNetwork"])
        self.assertFalse(spec["enableServiceLinks"])
        container = engine.containers["engine"]
        self.assertEqual(
            container["resources"],
            {"limits": {"gpu.example.com/gpu": "2", "rdma.example.com/hca": "1"}},
        )
        self.assertEqual(
            {port["name"]: port["containerPort"] for port in container["ports"]},
            {"http": 8000, "kv-side-channel": 5600},
        )
        self.assertEqual(engine.manifests["plan_sha256"], plan_digest(run))

    def test_prepare_refuses_inputs_a_pod_cannot_run(self):
        image = "sha256:" + "e" * 64
        cases = {
            "registry digest": {"NARWHAL_ENGINE_IMAGE": image},
            "extended resource name": {"NARWHAL_KUBERNETES_GPU_RESOURCE": "nvidia"},
            "two resources": {"NARWHAL_KUBERNETES_RDMA_RESOURCE": "gpu.example.com/gpu"},
            "JSON volume source": {"NARWHAL_KUBERNETES_MODEL_VOLUME": '{"emptyDir": {}}'},
            "PersistentVolumeClaim's name": {"NARWHAL_KUBERNETES_STATE_CLAIM": "State_Claim"},
            "<secret name>/<key>": {"NARWHAL_KUBERNETES_API_KEY_SECRET": "credential"},
            "the Secret that holds": {"NARWHAL_ENGINE_API_KEY": ""},
            "service account the attestation sidecar": {"NARWHAL_KUBERNETES_SERVICE_ACCOUNT": ""},
            "the Narwhal image's registry digest": {
                "NARWHAL_KUBERNETES_SIDECAR_IMAGE": "registry.example/narwhal:latest"
            },
            "must name the sidecar route": {
                "NARWHAL_NODE_1_ATTESTATION_URL": "http://engine-1.engines.example.svc:9999/v1/attestation"
            },
            "a RuntimeClass name": {"NARWHAL_KUBERNETES_RUNTIME_CLASS": "Nvidia Runtime"},
        }
        for index, (message, environment) in enumerate(cases.items()):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.prepared(f"case-{index}", **environment)
        resource = {"NARWHAL_KUBERNETES_GPU_RESOURCE": "devices.kubernetes.io/gpu"}
        with self.assertRaisesRegex(ValueError, "extended resource name"):
            self.prepared("reserved", **resource)

    def test_the_attestation_sidecar_reads_its_pod_and_nothing_else(self):
        run, plan = self.prepared()
        manifests = json.loads((run / "kubernetes.json").read_text())
        spec = manifests["podSpec"]
        self.assertTrue(spec["shareProcessNamespace"])
        self.assertEqual(spec["serviceAccountName"], "narwhal-attestation")
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertNotIn("runtimeClassName", spec)
        engine, sidecar = spec["containers"]
        self.assertEqual(sidecar["name"], kubernetes.SIDECAR)
        self.assertEqual(sidecar["image"], plan["kubernetes"]["sidecar_image"])
        self.assertEqual(
            sidecar["command"][-4:],
            ["--run", str(kubernetes.LAUNCH_MOUNT), "--pod-evidence", str(pod.STATE)],
        )
        self.assertEqual(
            sidecar["securityContext"],
            {
                "runAsUser": 0,
                "runAsGroup": 0,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        mounts = {mount["mountPath"]: mount for mount in sidecar["volumeMounts"]}
        launch = mounts[str(kubernetes.LAUNCH_MOUNT)]
        self.assertEqual(
            (launch["subPathExpr"], launch["readOnly"]),
            (f"engines/{plan['role']}/launches/$(NARWHAL_POD_UID)", True),
        )
        self.assertNotIn("readOnly", mounts[str(pod.STATE)])
        self.assertEqual(mounts[str(pod.ENGINE)]["name"], "narwhal-engine")
        # Only the sidecar holds the service account's token.
        self.assertTrue(mounts["/var/run/secrets/kubernetes.io/serviceaccount"]["readOnly"])
        engine_mounts = {mount["name"] for mount in engine["volumeMounts"]}
        self.assertIn("narwhal-engine", engine_mounts)
        self.assertNotIn("narwhal-api", engine_mounts)
        names = {entry["name"] for entry in sidecar["env"]}
        self.assertTrue({"NARWHAL_POD_UID", "NARWHAL_POD_NAME", "NARWHAL_POD_IP"} <= names)
        self.assertEqual(manifests["role"]["rules"], [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}
        ])  # fmt: skip
        self.assertEqual(
            manifests["roleBinding"]["subjects"],
            [{"kind": "ServiceAccount", "name": "narwhal-attestation"}],
        )
        self.assertEqual(
            manifests["roleBinding"]["roleRef"]["name"], manifests["role"]["metadata"]["name"]
        )
        self.assertFalse(manifests["serviceAccount"]["automountServiceAccountToken"])
        run, _ = self.prepared("runtime-class", NARWHAL_KUBERNETES_RUNTIME_CLASS="nvidia")
        spec = json.loads((run / "kubernetes.json").read_text())["podSpec"]
        self.assertEqual(spec["runtimeClassName"], "nvidia")

    def test_the_engine_step_records_its_process_for_the_attestation_sidecar(self):
        run, plan, engine = self.started()
        directory = engine.path(pod.ENGINE) / ENGINE_PROCESS
        self.assertEqual(engine.logs, [directory / pod.STARTUP_LOG])
        self.assertEqual(
            engine.cache_outputs, [str(pod.ENGINE / ENGINE_PROCESS / pod.CACHE_LAYOUT)]
        )
        records = json.loads((directory / pod.INSPECTIONS_RECORD).read_text())
        expected = (
            load_backend("vllm").launcher().pod_inspections.inspections(plan, plan_digest(run))
        )
        self.assertEqual({name: record["argv"] for name, record in records.items()}, expected)
        # Outside the engine image the inspections cannot import the engine; each records why.
        self.assertTrue(all(record["returncode"] != 0 for record in records.values()))
        # A later engine process in the same pod replaces the earlier processes' directories.
        (engine.path(pod.ENGINE) / "stray").write_text("")
        stat = engine.path(pod.PROC) / "self" / "stat"
        stat.write_text(
            stat.read_text().replace(f"{ENGINE_TICKS}", "200000").replace("4242", "4300")
        )
        self.assertEqual(engine.run("engine"), 0)
        self.assertEqual([path.name for path in engine.path(pod.ENGINE).iterdir()], ["4300-200000"])

    def test_a_pod_takes_whole_gpus_and_an_rdma_transfer(self):
        _, env = launcher_inputs(self.root)
        env = kubernetes_env(env)
        runtime = self.fixtures.launch_runtime
        record = launch_record(copy.deepcopy(runtime), None)
        with self.assertRaisesRegex(ValueError, "transfers KV over ucx_rdma"):
            build(record, env, self.root / "tcp", backend="kubernetes")
        shared = {
            "group": "node:GPU-0",
            "gpu_uuid": "GPU-0",
            "device_allowance": 0.9,
            "gpu_memory_utilization": 0.4,
        }
        runtime = {**runtime, "extra_args": ["--gpu-memory-utilization", "0.4"]}
        record = launch_record(runtime, shared, dict(RDMA_TRANSFER))
        with self.assertRaisesRegex(ValueError, "without shared_device"):
            build(record, env, self.root / "shared", backend="kubernetes")

    def test_values_keep_their_dollar_signs_through_kubernetes_expansion(self):
        runtime = {**self.fixtures.launch_runtime, "environment": {"VLLM_FIXTURE": "a$(HOME)$$b"}}
        fixture = type(self.fixtures)(**{**vars(self.fixtures), "launch_runtime": runtime})
        self.fixtures = fixture
        run, _ = self.prepared()
        engine = self.pod(run)
        rendered = {
            entry["name"]: entry.get("value") for entry in engine.containers["engine"]["env"]
        }
        self.assertEqual(rendered["VLLM_FIXTURE"], "a$$(HOME)$$$$b")
        self.assertEqual(engine.environment("engine")["VLLM_FIXTURE"], "a$(HOME)$$b")

    def test_the_bootstrap_checks_every_delivered_file_before_the_step(self):
        hooks, tools = self.root / "hooks", self.root / "tools"
        hooks.mkdir()
        tools.mkdir()
        (hooks / "launch.json").write_text("{}")
        (tools / "pod.py").write_text("import sys; print(sys.argv[1:])")
        bootstrap = kubernetes.BOOTSTRAP.replace(
            repr(str(pod.TOOLS / "pod.py")), repr(str(tools / "pod.py"))
        )

        def pinned() -> dict:
            return {
                str(root): {path.name: pod.file_digest(path) for path in root.iterdir()}
                for root in (hooks, tools)
            }

        def run(pins: dict) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-I", "-c", bootstrap, json.dumps(pins), "serve", "--flag"],
                # The engine's PYTHONPATH names the hook directory.
                env={**os.environ, "PYTHONPATH": str(hooks)},
                capture_output=True,
                text=True,
                timeout=30,
            )

        pins = pinned()
        passed = run(pins)
        self.assertEqual((passed.returncode, passed.stdout), (0, "['serve', '--flag']\n"))
        # Isolated mode keeps a pinned module in the hook directory off the import path.
        (hooks / "json.py").write_text("raise SystemExit('the hook directory was imported')")
        passed = run(pinned())
        self.assertEqual((passed.returncode, passed.stdout), (0, "['serve', '--flag']\n"))
        refused = run(pins)
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        self.assertIn(f"{hooks} holds ['json.py', 'launch.json']", refused.stderr)
        (hooks / "json.py").unlink()
        (hooks / "launch.json").write_text('{"changed": true}')
        refused = run(pins)
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        self.assertIn("launch.json differs from its pinned SHA-256", refused.stderr)

    def test_the_rendered_commands_run_from_kubelet_config_map_volumes(self):
        run, plan = self.prepared()
        engine = self.pod(run)
        tools = engine.path(pod.TOOLS)
        delivered = sorted(path.name for path in tools.iterdir())
        self.assertEqual(
            delivered,
            sorted(
                [
                    "..data",
                    SNAPSHOT,
                    "checkpoint.py",
                    "inspections.py",
                    "pod.py",
                    "probe_reader.py",
                    "verdict.py",
                ]
            ),
        )
        checkpoint = engine.execute("checkpoint")
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        record = json.loads((engine.state / pod.CHECKPOINT_RECORD).read_text())
        self.assertEqual(record["model_tree_sha256"], plan["model_tree_sha256"])
        self.assertEqual(record["files"], kubernetes.delivered_files(plan, plan_digest(run)))
        output = self.fixtures.check_output(plan)
        runtime = engine.execute("runtime-check", probe=printing(output))
        self.assertEqual(runtime.returncode, 0, runtime.stderr)
        record = json.loads((engine.state / pod.RUNTIME_RECORD).read_text())
        self.assertEqual(record["transfer_device"]["ucx_net_devices"], "hca0:1")
        self.assertIn("evidence", record["verdict"])
        # The engine step replaces itself with the planned engine command, which finds no engine
        # outside the image. Its output reaches the container's output and the startup log of
        # the process's directory in the engine volume.
        served = engine.execute("engine")
        self.assertEqual(served.returncode, 1)
        self.assertEqual(plan["args"][0], "-m")
        missing = f"module specification for {plan['args'][1]!r}"
        self.assertIn(missing, served.stdout)
        log = engine.path(pod.ENGINE) / ENGINE_PROCESS / pod.STARTUP_LOG
        self.assertIn(missing, log.read_text())
        self.assertEqual(sorted(path.name for path in tools.iterdir()), delivered)
        self.check(run, plan, engine)
        self.assertEqual(read_checked(run)["transfer_device"], record["transfer_device"])
        (tools / SNAPSHOT / "verdict.py").write_text("def verdict(*arguments): return {}\n")
        refused = engine.execute("engine")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("verdict.py differs from its pinned SHA-256", refused.stderr)

    def test_the_runtime_and_engine_containers_may_lock_memory(self):
        run, plan = self.prepared()
        engine = self.pod(run)
        locked = {"capabilities": {"add": ["IPC_LOCK"]}}
        self.assertNotIn("securityContext", engine.containers["checkpoint"])
        for name in ("runtime-check", "engine"):
            self.assertEqual(engine.containers[name]["securityContext"], locked)
        # A ROCm engine also runs without a seccomp filter, as its Docker launch does.
        record = {**launch_record(self.fixtures.launch_runtime, None, dict(RDMA_TRANSFER))}
        rocm = {**record, "gpu_visibility_env": "ROCR_VISIBLE_DEVICES"}
        env = kubernetes_env(launcher_inputs(self.root)[1])
        self.assertTrue(kubernetes.pod_settings(env, rocm, plan)["seccomp_unconfined"])
        self.assertFalse(kubernetes.pod_settings(env, record, plan)["seccomp_unconfined"])
        plan["kubernetes"]["seccomp_unconfined"] = True
        manifests = json.loads((run / kubernetes.MANIFESTS).read_text())
        maps = tuple(item["metadata"]["name"] for item in manifests["configMaps"])
        spec = kubernetes.pod_spec(run, plan, [], maps)
        for container in (spec["initContainers"][1], spec["containers"][0]):
            self.assertEqual(
                container["securityContext"],
                {**locked, "seccompProfile": {"type": "Unconfined"}},
            )

    def test_the_pod_uses_its_allocated_hca_at_the_planned_port(self):
        run, plan = self.prepared()
        env = kubernetes_env(launcher_inputs(self.root)[1])
        for net, port in (("hca0:2", 2), ("hca0:1,hca1:1", None), ("hca0:0", None)):
            with self.subTest(net=net):
                transfer = {**RDMA_TRANSFER, "net_devices": net}
                record = launch_record(self.fixtures.launch_runtime, None, transfer)
                if port is None:
                    with self.assertRaisesRegex(ValueError, "names one HCA and its port"):
                        kubernetes.pod_settings(env, record, plan)
                else:
                    self.assertEqual(kubernetes.pod_settings(env, record, plan)["hca_port"], port)
        engine = self.pod(run, hcas=("mlx5_3",))
        with patch.object(pod, "ROOT", engine.root):
            device = pod.transfer_device({"kubernetes": {"hca_port": 2}})
        self.assertEqual(
            device,
            {"verbs_device": "uverbs0", "hca": "mlx5_3", "port": 2, "ucx_net_devices": "mlx5_3:2"},
        )

    def test_the_runtime_check_judges_the_probe_output_in_the_pod(self):
        run, plan = self.prepared()
        engine = self.pod(run)
        self.assertEqual(engine.run("checkpoint"), 0)
        output = self.fixtures.check_output(plan)
        failing = output.replace("NARWHAL_TOKENIZER_READY=1\n", "")
        self.assertNotEqual(failing, output)
        self.assertEqual(engine.run("runtime-check", probe=printing(failing)), 1)
        self.assertIn("one tokenizer confirmation", engine.stderr.getvalue())
        record = json.loads((engine.state / pod.RUNTIME_RECORD).read_text())
        self.assertEqual((record["returncode"], "verdict" in record), (0, False))
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("records a failure", engine.stderr.getvalue())
        with self.assertRaisesRegex(ValueError, "runtime-check: .*one tokenizer confirmation"):
            self.check(run, plan, engine)
        self.assertEqual(engine.run("runtime-check", probe=printing(output)), 0)
        record = json.loads((engine.state / pod.RUNTIME_RECORD).read_text())
        changed = copy.deepcopy(record)
        changed["verdict"]["evidence"]["prefix_caching"] = False
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "pod's verdict differs from this check"):
            self.check(run, plan, engine)
        # A restarted engine container judges the recorded output again, so a record written
        # after the init containers ran cannot start the engine.
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("recorded verdict differs", engine.stderr.getvalue())
        forged = {**changed, "verdict": {}}
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(forged))
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("recorded verdict differs", engine.stderr.getvalue())
        forged["stdout"] = failing
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(forged))
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn(
            "recorded runtime check output fails: runtime check requires one tokenizer",
            engine.stderr.getvalue(),
        )
        del changed["verdict"]
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(changed))
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("the runtime check did not pass", engine.stderr.getvalue())
        self.assertEqual(engine.executed, [])
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(record))
        self.assertEqual(engine.run("engine"), 0)
        self.check(run, plan, engine)

    def test_the_check_records_the_pod_and_its_allocated_hca(self):
        run, plan, engine = self.started()
        printed = self.check(run, plan, engine)
        self.assertIn("cache events publish under /narwhal-kv-events", printed)
        checked = read_checked(run)
        self.assertEqual(
            checked["transfer_device"],
            {"verbs_device": "uverbs0", "hca": "hca0", "port": 1, "ucx_net_devices": "hca0:1"},
        )
        self.assertEqual(checked["plan_sha256"], plan_digest(run))
        evidence = (run / "checked.json").read_text()
        self.check(run, plan, engine)
        self.assertEqual((run / "checked.json").read_text(), evidence)

    def test_the_runtime_check_needs_exactly_one_hca(self):
        for hcas in ((), ("hca0", "hca1")):
            with self.subTest(hcas=hcas):
                run, plan = self.prepared(f"hcas-{len(hcas)}")
                engine = self.pod(run, f"pod-{len(hcas)}", hcas=hcas)
                self.assertEqual(engine.run("checkpoint"), 0)
                output = self.fixtures.check_output(plan)
                self.assertEqual(engine.run("runtime-check", probe=printing(output)), 1)
                self.assertIn(f"holds {len(hcas)} RDMA verbs devices", engine.stderr.getvalue())
                self.assertEqual(engine.run("engine"), 1)
                self.assertEqual(engine.executed, [])
                with self.assertRaisesRegex(ValueError, f"holds {len(hcas)} RDMA verbs devices"):
                    self.check(run, plan, engine)
                self.assertFalse((run / "checked.json").exists())

    def test_the_engine_starts_only_on_the_checked_hca_plan_and_arguments(self):
        _, _, engine = self.started()
        engine.executed.clear()
        engine.allocate(("hca1",))
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("allocated HCA differs", engine.stderr.getvalue())
        engine.allocate(("hca0",))
        command = engine.containers["engine"]["command"]
        command.append("--changed")
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("engine arguments differ", engine.stderr.getvalue())
        command.pop()
        (engine.state / pod.RUNTIME_RECORD).unlink()
        self.assertEqual(engine.run("engine"), 1)
        self.assertIn("runtime-check.json is unreadable", engine.stderr.getvalue())
        self.assertEqual(engine.executed, [])

    def test_a_changed_model_stops_the_checkpoint_init_container(self):
        run, plan = self.prepared()
        engine = self.pod(run)
        (engine.path(pod.MODEL) / "config.json").write_text('{"changed": true}')
        self.assertEqual(engine.run("checkpoint"), 1)
        self.assertIn("differ from the launch plan's tree digest", engine.stderr.getvalue())
        output = self.fixtures.check_output(plan)
        self.assertEqual(engine.run("runtime-check", probe=printing(output)), 0)
        self.assertEqual(engine.run("engine"), 1)
        with self.assertRaisesRegex(ValueError, "pod's model files differ from the launch plan"):
            self.check(run, plan, engine)

    def test_the_check_records_the_status_of_the_hashed_model_files(self):
        run, plan, engine = self.started()
        record = json.loads((engine.state / pod.CHECKPOINT_RECORD).read_text())
        self.check(run, plan, engine)
        self.assertEqual(read_checked(run)["model_status_sha256"], record["status_sha256"])
        # A pod whose checkpoint step records no status gives the sidecar nothing to compare.
        run, plan = self.prepared("earlier")
        engine = self.pod(run, "earlier-pod")
        engine.start(self.fixtures.check_output(plan))
        path = engine.state / pod.CHECKPOINT_RECORD
        record = json.loads(path.read_text())
        del record["status_sha256"]
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "checkpoint record holds no model file status"):
            self.check(run, plan, engine)

    def test_a_failed_or_stalled_probe_fails_its_init_container_and_the_check(self):
        run, plan = self.prepared()
        engine = self.pod(run)
        self.assertEqual(engine.run("checkpoint"), 0)
        failing = ["-c", "import sys; sys.stderr.write('loading\\nboom\\n'); sys.exit(3)"]
        self.assertEqual(engine.run("runtime-check", probe=failing), 1)
        with self.assertRaisesRegex(
            ValueError, "^engine pod runtime check exited 3: boom; inspect"
        ):
            self.check(run, plan, engine)
        engine.containers["runtime-check"]["command"][7] = "0.2"
        stalled = ["-c", "import time; time.sleep(30)"]
        self.assertEqual(engine.run("runtime-check", probe=stalled), 1)
        with self.assertRaisesRegex(ValueError, "exceeded 0.2 seconds"):
            self.check(run, plan, engine)

    def test_the_check_refuses_records_of_another_plan_pod_or_delivery(self):
        run, plan, engine = self.started()
        other, other_plan = self.prepared("other")
        with self.assertRaisesRegex(ValueError, "^the engine pod ran another launch plan"):
            self.check(other, other_plan, engine)
        # The check keeps no record of another plan.
        self.assertFalse((other / "pods").exists())
        record = json.loads((engine.state / pod.RUNTIME_RECORD).read_text())
        record["files"][str(pod.TOOLS)]["pod.py"] = "0" * 64
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "delivered files differ from the plan"):
            self.check(run, plan, engine)
        record["pod_uid"] = "00000000-0000-4000-8000-000000000000"
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "come from more than one pod"):
            self.check(run, plan, engine)
        for uid in (None, 7, ["uid"], "not-a-uid"):
            with self.subTest(uid=uid):
                record["pod_uid"] = uid
                (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "runtime-check.json names no pod UID"):
                    self.check(run, plan, engine)
        del record["pod_uid"]
        (engine.state / pod.RUNTIME_RECORD).write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "runtime-check.json names no pod UID"):
            self.check(run, plan, engine)
        self.assertFalse((run / "checked.json").exists())

    def test_the_records_of_a_replaced_pod_need_a_fresh_launch_directory(self):
        run, plan, engine = self.started()
        self.check(run, plan, engine)
        for name in (pod.CHECKPOINT_RECORD, pod.RUNTIME_RECORD):
            record = json.loads((engine.state / name).read_text())
            record["pod_uid"] = "00000000-0000-4000-8000-000000000000"
            (engine.state / name).write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "^runtime identity changed"):
            self.check(run, plan, engine)
        self.assertEqual(
            sorted(path.name for path in (run / "pods").iterdir()),
            sorted([engine_uid(run), "00000000-0000-4000-8000-000000000000"]),
        )

    def test_the_cli_checks_a_pod_plan_only_from_its_records(self):
        run, _, engine = self.started()
        code, error = run_cli("check", "--run", str(run))
        self.assertEqual(code, 1)
        self.assertIn("--pod-evidence", error)
        code, error = run_cli("start", "--run", str(run))
        self.assertEqual(code, 1)
        self.assertIn("start does not apply to a Kubernetes plan", error)
        code, error = run_cli("check", "--run", str(run), "--pod-evidence", str(engine.state))
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(read_checked(run)["backend"], "kubernetes")
        (self.root / "container").mkdir()
        container, _, _ = prepared(
            self.root / "container", load_backend("vllm").launcher(), self.fixtures
        )
        code, error = run_cli("check", "--run", str(container), "--pod-evidence", str(engine.state))
        self.assertEqual(code, 1)
        self.assertIn("this plan runs its check on this host", error)


class RdmaTransportTests(unittest.TestCase):
    def test_rdma_plans_move_same_host_kv_over_their_hcas(self):
        with tempfile.TemporaryDirectory() as folder:
            record, env = launcher_inputs(Path(folder))
            document = launch_document()
            entry = document["engines"]["engine-1"]
            entry.update(
                gpu_ids=["0"],
                tensor_parallel_size=1,
                gpu_visibility_env="CUDA_VISIBLE_DEVICES",
                accelerator_devices=["/dev/nvidiactl", "/dev/nvidia0"],
                transfer=dict(RDMA_TRANSFER),
            )
            selected = selected_launch(document, "engine-1", {})
            self.assertEqual(selected["transfer"]["gpu_tls"], "cuda_copy")
            selected["runtime"] = record["runtime"]
            plan, values = build(selected, env, Path(folder) / "launch")
            self.assertEqual(plan["ucx_tls"], "rc,sm,self,cuda_copy")
            self.assertEqual(values["UCX_SHM_DEVICES"], "none")
            self.assertEqual(values["UCX_NET_DEVICES"], "hca0:1")
            entry["transfer"]["gpu_tls"] = "cuda"
            with self.assertRaisesRegex(ValueError, "gpu_tls is incompatible.*use cuda_copy"):
                selected_launch(document, "engine-1", {})
            selected["runtime"]["environment"] = {"UCX_SHM_DEVICES": "posix"}
            with self.assertRaisesRegex(ValueError, "UCX_SHM_DEVICES"):
                build(selected, env, Path(folder) / "managed")

    def test_rocm_rdma_plans_take_the_rocm_copy_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            record, env = launcher_inputs(Path(folder))
            document = launch_document()
            entry = document["engines"]["engine-1"]
            entry["transfer"] = dict(RDMA_TRANSFER)
            selected = selected_launch(document, "engine-1", {})
            self.assertEqual(selected["transfer"]["gpu_tls"], "rocm_copy")
            selected["runtime"] = record["runtime"]
            plan, values = build(selected, env, Path(folder) / "launch")
            self.assertEqual(plan["ucx_tls"], "rc,sm,self,rocm_copy")
            self.assertEqual(values["UCX_SHM_DEVICES"], "none")
            entry["transfer"]["gpu_tls"] = "rocm"
            with self.assertRaisesRegex(ValueError, "gpu_tls is incompatible.*use rocm_copy"):
                selected_launch(document, "engine-1", {})

    def test_tcp_plans_keep_shared_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            record, env = launcher_inputs(Path(folder))
            _, values = build(record, env, Path(folder) / "launch")
            self.assertNotIn("UCX_SHM_DEVICES", values)
            self.assertEqual(values["UCX_TLS"], "tcp,sm,self,rocm")


def plan_digest(run: Path) -> str:
    return pod.file_digest(run / "launch.json")


def engine_uid(run: Path) -> str:
    return read_checked(run)["pod_uid"]


def run_cli(*arguments: str) -> tuple[int, str]:
    errors = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
        try:
            code = engine_cli.main(list(arguments))
        except SystemExit as exit_status:
            code = int(exit_status.code or 0)
    return code, errors.getvalue()


if __name__ == "__main__":
    unittest.main()
