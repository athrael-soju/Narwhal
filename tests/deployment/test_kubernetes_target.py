"""`narwhal deploy --target kubernetes` over the whole stage graph, against the stub object
store: the fleet's objects, Jobs adopted across a restart, failures that cannot wait, and the
engine API key kept in its Secret."""

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.deployment import fabric, fleet_resource, kubernetes_gate, kubernetes_target
from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.fleet_resource import VolumePolicy
from narwhal.deployment.hosts import Host
from narwhal.deployment.kubernetes_graph import KUBERNETES_GRAPH
from narwhal.deployment.runner import Stop
from narwhal.dev import cli
from narwhal.serving.control_token import ROUTER_TOKEN_ENV
from tests.deployment.kubernetes_cluster import (
    CONFIG_MAP,
    CONTROL_SECRET,
    DEPLOYMENT,
    JOB,
    NAMESPACE,
    NODES,
    POD,
    SECRET,
    SERVICE,
    STATEFUL_SET,
    Reject,
    Rig,
    Wait,
    emitted,
    instance_of,
    unpack,
)
from tests.deployment.test_kubernetes_objects import policy_refusals

ENGINES = tuple(NODES)
EDGES = ("gpu-a-to-gpu-b", "gpu-b-to-gpu-a")
# The gate Jobs one run starts, in order: two engines on two nodes, one cache group.
JOBS = [
    # Each run first reads the HMAC of the engine API key.
    "key-check",
    *("discovery", "fabric-probe") * 2,
    *("prepare",) * 2,
    *("engine-check",) * 2,
    *("state",) * 2,
    *("state",) * 2,
    *("sidecar-verify",) * 2,
    "finalize-fleet",
    "profile",
    "calibrate",
    "state",
    "preflight",
    "router-check",
]
# A router check's call: the action and its arguments, at the router that controls the fleet.
CALL = re.compile(r'exec python -m narwhal\.deployment\.host_http (\S+) "\$base" ?(.*)')
ROUTER_URLS = [f"http://router-{n}.routers.{NAMESPACE}.svc.cluster.local:8000" for n in (0, 1)]


def call(entry: dict) -> tuple[str, str]:
    """The action and arguments a router-check Job calls."""
    found = CALL.search(entry["command"][-1])
    assert found is not None, entry["command"]
    return found[1], found[2]


# The kinds the fleet's objects take, and those of the stub's own.
DERIVED = (JOB, CONFIG_MAP, SERVICE, STATEFUL_SET, DEPLOYMENT)


def every_stage() -> set[str]:
    keys = {"access", "discover", "install", "cache-groups", "fabric-budget[engine-1]"}
    keys |= {"finalize-fleet", "profile", "calibrate", "fleet-limits", "preflight"}
    keys |= {"router-start"} | {f"fabric-edge[{edge}]" for edge in EDGES}
    for stage in (
        "engine-prepare",
        "engine-start",
        "engine-check",
        "cache-capture",
        "attest-capture",
        "sidecar-start",
        "sidecar-verify",
    ):
        keys |= {f"{stage}[{engine}]" for engine in ENGINES}
    return keys


class Crash(BaseException):
    """The controller's process ends: what a lost Lease's `os._exit` leaves behind."""


class KubernetesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.rig = Rig(self.root)
        self.run = self.root / "run"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.rig.patched())
        stack.enter_context(patch.dict(os.environ, self.rig.env, clear=True))

    def deploy(self, action: str, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        argv = ["deploy", action, "--run", str(self.run), "--target", "kubernetes", *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main([*argv, "--format", "json"])
        return code, json.loads(out.getvalue())

    def record(self) -> dict:
        return json.loads((self.run / "run.json").read_text())

    def entries(self, kind: str) -> list[dict]:
        return [job["entry"] for job in self.rig.jobs.values() if job["entry"]["kind"] == kind]

    def derived(self) -> list[dict]:
        """Every object in the namespace that the fleet controls, pods included."""
        owner = objects.Owner.of(self.rig.fleet)
        found = [obj for kind in DERIVED for obj in self.rig.store.live(kind, NAMESPACE)]
        return [obj for obj in found if objects.controlled(owner, obj)]

    def deployment(self) -> kubernetes_target.KubernetesDeployment:
        deployment = kubernetes_target.KubernetesDeployment(self.run, self.rig.env)
        deployment.announce = lambda line: None
        return deployment

    def crash_when(self, kind: str) -> None:
        """End the process once a Job of `kind` runs, leaving the run record as a killed
        controller leaves it: the attempt still running and its Jobs named."""
        saved = {}

        def crash() -> None:
            running = [
                job
                for job in self.rig.live_jobs()
                if job["metadata"]["labels"][fleet_resource.GATE_LABEL] == kind
                and (job.get("status") or {}).get("active")
            ]
            if running and not saved:
                saved["record"] = (self.run / "run.json").read_bytes()
                raise Crash

        self.rig.on_tick = crash
        self.addCleanup(self._restore, saved)
        self._saved = saved

    def _restore(self, saved: dict) -> None:
        self.rig.on_tick = None

    def crashed(self) -> None:
        """Run until the crash, then put back the record the killed process left."""
        with self.assertRaises(Crash):
            self.deploy("run")
        self.left_behind(self._saved["record"])
        self.rig.on_tick = None

    def left_behind(self, data: bytes) -> None:
        """Write the record a killed runner left, whose lock names a process that ended."""
        record = json.loads(data)
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        record["lock"]["owner"]["pid"] = ended.pid
        (self.run / "run.json").write_text(json.dumps(record))


class KubernetesDeployTests(KubernetesTest):
    def test_every_workload_a_run_applies_passes_the_charts_admission_policy(self):
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        workloads = self.rig.workloads
        gates = {
            w["spec"]["template"]["metadata"]["labels"].get(fleet_resource.GATE_LABEL)
            for w in workloads
            if w["kind"] == "Job"
        }
        self.assertEqual(gates, set(JOBS))
        names = {(w["kind"], w["metadata"]["name"]) for w in workloads if w["kind"] != "Job"}
        expected = {("StatefulSet", "engine"), ("StatefulSet", "router"), ("Deployment", "haproxy")}
        self.assertEqual(names, expected)
        for workload in workloads:
            with self.subTest(f"{workload['kind']} {workload['metadata']['name']}"):
                self.assertEqual(policy_refusals(workload), [])

    def test_run_reaches_the_router_pair_and_a_rerun_starts_no_job(self):
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["data"]["ran"]), every_stage())
        self.assertEqual(self.rig.kinds, JOBS)
        record = self.record()
        self.assertEqual(record["target"], "kubernetes")
        # Each discovery Job runs the engine image on its node, with the node's GPUs and model.
        discovery = self.entries("discovery")
        self.assertEqual({entry["node"] for entry in discovery}, set(NODES.values()))
        for entry in discovery:
            self.assertEqual(entry["image"], self.rig.env["NARWHAL_ENGINE_IMAGE"])
            self.assertEqual(entry["resources"], {"gpu.example.com/gpu": "2"})
            self.assertEqual(
                entry["modelVolume"],
                {"persistentVolumeClaim": {"claimName": "model", "readOnly": True}},
            )
        # The engines start after their launches are prepared, and the routers and HAProxy
        # wait for router-start, after preflight.
        timeline = self.rig.timeline
        self.assertLess(
            timeline.index(("Job", "prepare")), timeline.index(("StatefulSet", "engine"))
        )
        self.assertGreater(
            timeline.index(("StatefulSet", "router")), timeline.index(("Job", "preflight"))
        )
        self.assertIn(("Deployment", "haproxy"), timeline)
        router = next(obj for obj in self.derived() if obj["metadata"]["name"] == "router")
        command = router["spec"]["template"]["spec"]["containers"][0]["command"]
        fleet = command[command.index("--fleet") + 1].removeprefix("/narwhal-state/")
        self.assertRegex(fleet, r"^deploy/[^/]+/.+/fleet\.json$")
        limits = json.loads(
            (
                self.run
                / Path(fleet).parent.relative_to(f"deploy/{record['run_id']}")
                / "fleet.json"
            ).read_text()
        )
        self.assertEqual(limits["recovery"]["state_path"], "/narwhal-router/router-state.json")
        self.assertTrue(limits["profiles"]["path"].startswith("/narwhal-state/deploy/"))
        # Evidence each Job returned lands in its stage's attempt directory, with its log.
        check = self.run / record["stages"]["engine-check[engine-1]"]["directory"]
        self.assertTrue((check / "checked.json").is_file())
        self.assertTrue(any((check / "logs").glob("engine-check-*.log")))
        # The engine check writes the launch directory where the pod's sidecar waits for it.
        uid = self.rig.pods["engine-1"]["metadata"]["uid"]
        [entry] = [e for e in self.entries("engine-check") if instance_of(e) == "engine-1"]
        self.assertIn(f"/narwhal-state/engines/engine-1/launches/{uid}", entry["command"][-1])
        # The sidecar runs as another user and reads the launch, which holds no key.
        self.assertIn('chmod -R u=rwX,go=rX "$stage"', entry["command"][-1])
        self.assertTrue(all(entry["serviceAccount"] for entry in self.entries("sidecar-verify")))
        # Every Job keeps its output in its attempt's directory on the state volume; the key
        # check, which runs before any attempt, keeps its own.
        for job in self.rig.jobs.values():
            entry = job["entry"]
            if entry["kind"] == "key-check":
                self.assertEqual(entry["keep"], f"deploy/key-check/{entry['name']}")
                continue
            self.assertEqual(entry["command"][:2], ["sh", "-c"])
            self.assertRegex(
                entry["keep"], rf"^deploy/{record['run_id']}/stages/.+/jobs/{entry['name']}$"
            )
        # The inputs ConfigMaps go once their Job finished, and each passed attempt's Jobs
        # with it; deleting a Job takes its pods.
        self.assertEqual(self.rig.config_maps, {})
        self.assertEqual(self.rig.live_jobs(), [])
        self.rig.settle()
        self.assertEqual(self.rig.store.live(POD, NAMESPACE), list(self.rig.pods.values()))

        created = len(self.rig.created)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["ran"], [])
        # Only the key check ran, under the fleet's one key-check name, and it found the key
        # unchanged.
        self.assertEqual([entry["kind"] for entry in self.rig.created[created:]], ["key-check"])

    def test_every_object_names_the_fleet_as_its_controller(self):
        self.assertEqual(self.deploy("run")[0], 0)
        owner = objects.Owner.of(self.rig.fleet)
        for kind in DERIVED:
            for obj in self.rig.store.live(kind, NAMESPACE):
                if kind == CONFIG_MAP and obj["metadata"]["name"] == "kube-root-ca.crt":
                    continue
                with self.subTest(kind=kind.kind, name=obj["metadata"]["name"]):
                    self.assertTrue(objects.controlled(owner, obj))
        for job in self.rig.jobs.values():
            self.assertTrue(objects.controlled(owner, job["job"]))
        # Deleting the fleet leaves the rest to the garbage collector.
        self.rig.store.delete(
            fleet_store_kind(self.rig), NAMESPACE, "fleet", propagation_policy="Background"
        )
        self.rig.settle()
        self.assertEqual(self.derived(), [])
        self.assertEqual(self.rig.pods, {})

    def test_no_derived_object_carries_the_fleets_tracking_metadata(self):
        tracked = copy.deepcopy(self.rig.fleet)
        keys = {
            "app.kubernetes.io/instance": "fleet",
            "app.kubernetes.io/managed-by": "Helm",
            "helm.sh/chart": "narwhal-0.13.0",
            "argocd.argoproj.io/instance": "fleet",
            "kustomize.toolkit.fluxcd.io/name": "fleet",
            "helm.toolkit.fluxcd.io/name": "fleet",
        }
        tracked["metadata"]["labels"] = dict(keys)
        tracked["metadata"]["annotations"] = {
            "meta.helm.sh/release-name": "narwhal",
            "argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true",
        }
        self.rig.store.put(tracked)
        self.assertEqual(self.deploy("run")[0], 0)
        found = [*self.derived(), *(job["job"] for job in self.rig.jobs.values())]
        found += self.rig.store.live(POD, NAMESPACE)
        for obj in found:
            for _, block in objects._metadata_blocks(obj):
                for field in ("labels", "annotations"):
                    for name, value in (block.get(field) or {}).items():
                        with self.subTest(obj=obj["metadata"]["name"], key=name):
                            self.assertFalse(objects.denied(name, value))

    def test_a_replaced_engine_pod_is_checked_again_and_readmitted(self):
        self.assertEqual(self.deploy("run")[0], 0)
        before = len(self.rig.jobs)
        old = self.rig.pods["engine-2"]
        self.rig.pods["engine-2"] = {**old, "metadata": {**old["metadata"], "uid": "new-uid"}}
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        ran = set(result["data"]["ran"])
        self.assertEqual(
            ran,
            {
                "engine-start[engine-2]",
                "engine-check[engine-2]",
                "cache-capture[engine-2]",
                "attest-capture[engine-2]",
                "sidecar-start[engine-2]",
                "sidecar-verify[engine-2]",
                "finalize-fleet",
                "preflight",
                "router-start",
            },
        )
        checks = [
            job["entry"]
            for job in list(self.rig.jobs.values())[before:]
            if job["entry"]["kind"] == "router-check"
        ]
        self.assertEqual(
            [call(entry) for entry in checks], [("readmit", "120 n2"), ("responses", "")]
        )
        # Each check reaches the routers at their own addresses, never through HAProxy, which
        # forwards only the serving routes.
        for entry in checks:
            script = entry["command"][-1]
            self.assertIn(f"for base in {' '.join(ROUTER_URLS)}; do", script)
            self.assertIn('status "$base/narwhal/targets"', script)
            self.assertNotIn("haproxy", script)

    def test_a_router_check_calls_the_router_that_controls_the_fleet(self):
        deployment = self.deployment()
        sent = []
        result = kubernetes_target.GateResult("check", self.root / "log", "", {}, {})
        with patch.object(deployment, "gate", lambda context, gate: sent.append(gate) or result):
            context = SimpleNamespace(stage="router-start", directory=self.root / "attempt")
            deployment._host_http(context, "readmit", "readmit", "120", "n2")  # type: ignore[arg-type]
        [gate] = sent
        tools = self.root / "bin"
        tools.mkdir()
        python = tools / "python"
        # The second router controls the fleet; the first answers 404 to /narwhal/targets.
        python.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = status ]; then case "$4" in *router-1*) echo 200;; *) echo 404;; esac\n'
            'else echo "called $3 $4 $5 $6"; fi\n'
        )
        python.chmod(stat.S_IRWXU)
        env = {"PATH": f"{tools}:/usr/bin:/bin"}
        ran = subprocess.run(gate.command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(ran.stdout, f"called readmit {ROUTER_URLS[1]} 120 n2\n")
        python.write_text("#!/bin/sh\necho 0\n")
        ran = subprocess.run(gate.command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(ran.returncode, 1)
        self.assertIn("narwhal-host: no router controls the fleet", ran.stderr)

    def test_each_engine_pod_is_pinned_to_the_node_discovery_measured(self):
        self.rig.misplace = {"engine-2": "gpu-c"}
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        [engines] = [obj for obj in self.derived() if obj["metadata"]["name"] == "engine"]
        template = engines["spec"]["template"]["spec"]
        self.assertEqual(template["schedulingGates"], [{"name": kubernetes_target.PLACEMENT_GATE}])
        for role, node in NODES.items():
            pod = self.rig.pods[role]
            self.assertEqual(pod["spec"]["nodeName"], node)
            self.assertNotIn("schedulingGates", pod["spec"])
            terms = pod["spec"]["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"]
            self.assertEqual(terms[0]["matchFields"][0]["values"], [node])
        # The pod the scheduler put on another node was replaced, then pinned.
        deleted = [d for d in self.rig.store.deletes if d.kind == "Pod"]
        self.assertEqual([(d.name, d.propagation) for d in deleted], [("engine-2", "Background")])

    def test_a_group_member_that_resolved_another_layout_than_its_representative_fails(self):
        self.rig.layouts["engine-2"] = "NHD"
        code, result = self.deploy("run")
        self.assertEqual(code, 1, result)
        [error] = result["errors"]
        self.assertEqual(error["stage"], "attest-capture[engine-2]")
        self.assertIn("Resolved layout NHD differs from the representative's HND", error["message"])

    def test_handoffs_are_measured_by_a_job_on_the_engines_node(self):
        engines = {
            role: fabric.Engine(
                role,
                Host(node, "", None, (role,)),
                fabric.Endpoint(
                    Host(node, "", None, (role,)), f"10.0.0.{role[-1]}", "f0", "ucx_tcp", None
                ),
                "f0",
                1,
                Path("launch.json"),
                {f"NARWHAL_NODE_{role[-1]}_URL": f"http://{role}.engines:8100"},
                {},
                False,
                True,
            )
            for role, node in NODES.items()
        }
        peers = {"engine-1": "engine-2", "engine-2": "engine-1"}
        target = kubernetes_target
        with (
            patch.object(target, "cache_capture", lambda backend: None),
            patch.object(target, "transfer_measurement", lambda backend: object()),
            patch.object(target.fabric, "measure_handoffs", lambda *a: (engines, peers, 4096)),
        ):
            code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        ran = set(result["data"]["ran"])
        self.assertTrue({f"transfer-measure[{role}]" for role in NODES} <= ran)
        self.assertFalse(any(key.startswith("cache-capture") for key in ran))
        measured = self.entries("transfer-measure")
        self.assertEqual({entry["node"] for entry in measured}, set(NODES.values()))
        for entry in measured:
            self.assertTrue(entry["hostNetwork"])
            self.assertIn("cd /tmp/narwhal-in", entry["command"][-1])
            files = unpack(self.rig.inputs[entry["name"]])
            role = next(role for role, node in NODES.items() if node == entry["node"])
            self.assertIn(f"runs/deployment/.env.{role}", files)
            self.assertIn(f"config/engine-launch.{role}.json", files)
            # The measurement calls the engines with the key from the Job's own variable.
            role_file = files[f"runs/deployment/.env.{role}"]
            self.assertNotIn(SECRET.encode(), role_file)
            self.assertIn(b'NARWHAL_ENGINE_API_KEY="${NARWHAL_ENGINE_KEY:?', role_file)
        self.assertEqual(self.rig.kinds.count("transfer-measure"), 2)
        [profile] = self.entries("profile")
        self.assertIn("--no-router", profile["command"][-1])
        transfer = self.run / self.record()["stages"]["transfer-measure[engine-1]"]["directory"]
        self.assertTrue((transfer / "transfer" / "transfer.json").is_file())

    def test_a_sample_runs_its_server_job_then_its_client_job(self):
        self.assertEqual(self.deploy("run")[0], 0)
        hosts = {role: Host(node, "", None, (role,)) for role, node in NODES.items()}
        endpoints = {
            role: fabric.Endpoint(hosts[role], f"10.0.0.{role[-1]}", "f0", "ucx_tcp", None)
            for role in NODES
        }
        engines = {
            role: fabric.Engine(
                role, hosts[role], endpoints[role], "f0", 2, Path("l"), {}, {}, True, False
            )
            for role in NODES
        }
        plan = fabric.Fabric(engines, (), None, 5201, "/tmp", {}, {})  # type: ignore[arg-type]
        sample = fabric.Sample(
            endpoints["engine-1"], endpoints["engine-2"], ("engine-1",), ("engine-2",)
        )
        deployment = self.deployment()
        context = kubernetes_target._LiveContext(self.run)
        shell = kubernetes_target.JobShell(deployment, context)  # type: ignore[arg-type]
        before = len(self.rig.jobs)
        self.rig.hold = {"fabric-server"}
        report = shell.sample(plan, "gpu-a-to-gpu-b", sample)
        self.assertEqual(report, b'{"end": {}}\n')
        entries = [job["entry"] for job in list(self.rig.jobs.values())[before:]]
        kinds = [(entry["kind"], entry["node"]) for entry in entries]
        self.assertEqual(
            kinds,
            [
                ("fabric-probe", "gpu-a"),
                ("fabric-probe", "gpu-b"),
                ("fabric-server", "gpu-b"),
                ("fabric-client", "gpu-a"),
            ],
        )
        self.assertIn("iperf3 --server --one-off --bind 10.0.0.2", entries[2]["command"][-1])
        self.assertIn("iperf3 --client 10.0.0.2 --bind 10.0.0.1", entries[3]["command"][-1])
        # The client starts while the server still runs.
        self.assertEqual(self.rig.calls["client while its server runs"], 1)

    def test_a_changed_route_measures_its_edge_again(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.rig.routes["gpu-a-to-gpu-b"] = "route-2"
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(
            set(result["data"]["ran"]),
            {
                "fabric-edge[gpu-a-to-gpu-b]",
                "calibrate",
                "fleet-limits",
                "preflight",
                "router-start",
            },
        )

    def test_plan_starts_no_job_and_says_it_did_not_observe_the_links(self):
        code, result = self.deploy("plan")
        self.assertEqual(code, 0, result)
        self.assertEqual((self.rig.jobs, self.derived()), ({}, []))
        self.assertTrue(all(row["runs"] for row in result["data"]["stages"]))
        self.assertEqual(self.deploy("run")[0], 0)
        jobs = len(self.rig.jobs)
        self.rig.routes["gpu-a-to-gpu-b"] = "route-2"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["deploy", "plan", "--run", str(self.run), "--target", "kubernetes"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.rig.jobs), jobs)
        self.assertEqual(self.rig.calls["observe[gpu-a-to-gpu-b]"], 0)
        # Observing a route takes a Job on each node, so plan reports the link unobserved
        # rather than holding; run observes it.
        lines = out.getvalue().splitlines()
        for edge in EDGES:
            self.assertIn(
                f"fabric-edge[{edge}]: skipped unless run finds a change; "
                f"link[{edge}] not observed",
                lines,
            )
        self.assertIn("router-start: skipped; its recorded result holds", lines)
        code, result = self.deploy("plan")
        rows = {row["stage"]: row for row in result["data"]["stages"]}
        self.assertEqual(
            rows["fabric-edge[gpu-a-to-gpu-b]"]["unobserved"], ["link[gpu-a-to-gpu-b]"]
        )
        self.assertNotIn("unobserved", rows["router-start"])
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertIn("fabric-edge[gpu-a-to-gpu-b]", result["data"]["ran"])

    def test_a_pin_whose_reply_was_lost_reads_the_pod_and_finishes(self):
        # The affinity patch lands and its reply is lost. The pod is read back and the pin
        # goes on from what it shows.
        self.rig.store.fail("patch", POD, name="engine-2", code=503, commit=True)
        code, result = self.deploy("run", "--until", "engine-start")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.rig.store.faults, [])
        pod = self.rig.pods["engine-2"]
        self.assertEqual(pod["spec"]["nodeName"], NODES["engine-2"])
        self.assertNotIn("schedulingGates", pod["spec"])
        self.assertEqual([d for d in self.rig.store.deletes if d.kind == "Pod"], [])

    def test_a_lost_ungate_reply_is_read_back_and_the_patch_made_again(self):
        # The affinity patch goes through; the gate removal is refused before it lands.
        def lose_ungate() -> None:
            pod = self.rig.store._objects[POD].get((NAMESPACE, "engine-1"))
            pinned = pod is not None and (pod["spec"].get("affinity") or {}).get("nodeAffinity")
            if pinned and not self.rig.calls["lost"]:
                self.rig.calls["lost"] += 1
                self.rig.store.fail("patch", POD, name="engine-1", code=503)

        self.rig.store.validators.setdefault(POD, []).append(lambda new, old: lose_ungate())
        code, result = self.deploy("run", "--until", "engine-start")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.rig.calls["lost"], 1)
        self.assertEqual(self.rig.pods["engine-1"]["spec"]["nodeName"], NODES["engine-1"])

    def test_a_gate_another_controller_added_ahead_of_the_placement_gate_stays(self):
        self.rig.gate_ahead = {"engine-1"}
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assertEqual(result["data"]["failed_stage"], "engine-start[engine-1]")
        # The patch tests the gate at the index it removes, so it removed neither gate.
        self.assertEqual(
            self.rig.pods["engine-1"]["spec"]["schedulingGates"],
            [{"name": "example.com/other"}, {"name": kubernetes_target.PLACEMENT_GATE}],
        )
        # Its controller then releases its own gate.
        pod = copy.deepcopy(self.rig.pods["engine-1"])
        pod["spec"]["schedulingGates"].pop(0)
        self.rig.pods["engine-1"] = pod
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.rig.pods["engine-1"]["spec"]["nodeName"], NODES["engine-1"])

    def test_access_names_a_node_without_its_resources_and_a_fleet_made_again(self):
        node = self.rig.store.get(kubernetes_node_kind(), None, "gpu-b")
        node["status"]["allocatable"] = {"gpu.example.com/gpu": "0"}
        self.rig.store.put(node)
        code, result = self.deploy("run")
        self.assertEqual(code, 1, result)
        [error] = result["errors"]
        self.assertEqual(error["stage"], "access")
        self.assertIn("node gpu-b of engine-2 offers no gpu.example.com/gpu", error["message"])
        self.assertIn("offers no rdma.example.com/hca", error["message"])
        self.assertEqual(self.rig.kinds, ["key-check"])
        # The fleet was deleted and made again under its name: the run's uid no longer names it.
        fleet = copy.deepcopy(self.rig.fleet)
        fleet["metadata"]["uid"] = "11111111-2222-4333-8444-555555555555"
        self.rig.store.put(fleet)
        code, result = self.deploy("run")
        self.assertIn("the fleet was deleted and made again", result["errors"][0]["message"])

    def test_status_and_down_refuse_a_run_directory_other_users_may_write(self):
        self.assertEqual(self.deploy("run")[0], 0)
        deletes = len(self.rig.store.deletes)
        self.run.chmod(0o770)
        for action in ("status", "down"):
            code, result = self.deploy(action)
            self.assertEqual(code, 2, result)
            self.assertIn("lets other users in", result["errors"][0]["message"])
        self.assertEqual(len(self.rig.store.deletes), deletes)
        self.run.chmod(0o700)
        record = self.run / "run.json"
        moved = self.run / "elsewhere.json"
        record.rename(moved)
        record.symlink_to(moved)
        code, result = self.deploy("status")
        self.assertEqual(code, 2, result)
        self.assertIn("must be a regular file", result["errors"][0]["message"])
        record.unlink()
        moved.rename(record)
        # The SSH target applies the same check to the same directory.
        self.run.chmod(0o750)
        for action in ("status", "down"):
            out = io.StringIO()
            argv = ["deploy", action, "--run", str(self.run), "--target", "ssh"]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main([*argv, "--format", "json"])
            result = json.loads(out.getvalue())
            self.assertEqual(code, 2, result)
            self.assertIn("lets other users in", result["errors"][0]["message"])

    def test_a_rejected_gate_names_its_recovery_and_the_next_run_resumes_there(self):
        self.rig.fail_kind = "engine-check"
        code, result = self.deploy("run")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["status"], "failed_gate")
        [error] = result["errors"]
        self.assertEqual(error["stage"], "engine-check[engine-1]")
        self.assertIn("the check rejected the engine", error["message"])
        self.assertTrue(
            error["context"]["recovery"].endswith("deploy/Kubernetes/#checking-the-engine-pods")
        )
        # The failed attempt keeps its Job for inspection; the next attempt deletes it first.
        [failed] = [job for job in self.rig.live_jobs() if job["status"].get("failed")]
        self.rig.fail_kind = None
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertNotIn("discover", result["data"]["ran"])
        self.assertIn("engine-check[engine-1]", result["data"]["ran"])
        name = failed["metadata"]["name"]
        self.assertIn(("Job", name, "Background"), deletions(self.rig))
        self.assertEqual(self.record()["stages"]["engine-check[engine-1]"]["stale_jobs"], [])

    def test_a_truncated_job_log_fails_its_stage(self):
        self.rig.truncate_kind = "prepare"
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assertIn("truncated", result["errors"][0]["message"])

    def test_status_reports_a_replaced_pod_and_down_removes_the_workloads(self):
        self.assertEqual(self.deploy("run")[0], 0)
        code, result = self.deploy("status")
        self.assertEqual(code, 0, result)
        del self.rig.pods["router-1"]
        code, result = self.deploy("status")
        self.assertEqual(code, 3, result)
        states = {check["key"]: check["state"] for check in result["data"]["live"]}
        self.assertNotEqual(states["router_identity"], "holds")
        # An object of the chart's, which the fleet does not control, stays.
        self.rig.store.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "chart-owned", "namespace": NAMESPACE},
            }
        )
        before = len(self.rig.store.deletes)
        code, result = self.deploy("down")
        self.assertEqual(code, 0, result)
        stopped = {process["name"]: process["stopped"] for process in result["data"]["processes"]}
        self.assertEqual(stopped["router-0"], True)
        self.assertEqual(stopped["router-1"], False)
        self.assertTrue(stopped["engine-1"])
        self.rig.settle()
        self.assertEqual(self.derived(), [])
        self.assertEqual(sorted(self.rig.pods), [])
        self.assertIsNotNone(self.rig.store.get(CONFIG_MAP, NAMESPACE, "chart-owned"))
        # Down stops the routers, then HAProxy, then the engines, each before what it mounts.
        stopping = deletions(self.rig)[before:]
        order = [(kind, name) for kind, name, _ in stopping]
        self.assertEqual(
            order[:3],
            [("StatefulSet", "router"), ("Service", "routers"), ("Deployment", "haproxy")],
        )
        self.assertLess(
            order.index(("Deployment", "haproxy")), order.index(("StatefulSet", "engine"))
        )
        self.assertTrue(all(policy == "Background" for _, _, policy in stopping))

    def test_a_run_after_down_applies_and_starts_the_workloads_again(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.assertEqual(self.deploy("down")[0], 0)
        self.rig.settle()
        record = self.record()
        for stage in kubernetes_target.STOP_INVALIDATES:
            entries = [e for e in record["stages"].values() if e["stage"] == stage]
            self.assertTrue(entries, stage)
            self.assertEqual({e["status"] for e in entries}, {"invalidated"}, stage)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertIn("install", result["data"]["ran"])
        self.assertIn("router-start", result["data"]["ran"])
        names = {(o["kind"], o["metadata"]["name"]) for o in self.derived()}
        self.assertLessEqual(
            {("StatefulSet", "engine"), ("StatefulSet", "router"), ("Deployment", "haproxy")},
            names,
        )

    def test_down_returns_only_once_the_stopped_pods_are_gone(self):
        # A deleted engine pod holds its node's GPUs until the kubelet ends it, so a fleet
        # started again at once would have its pods refused at admission.
        self.assertEqual(self.deploy("run")[0], 0)
        self.rig.store.patch(
            POD, NAMESPACE, "engine-1", {"metadata": {"finalizers": ["example.com/kubelet"]}}
        )
        waits = []

        def kubelet() -> None:
            names = {item["metadata"]["name"] for item in self.rig.store.live(POD, NAMESPACE)}
            if "engine-1" in names:
                waits.append(1)
                if len(waits) == 3:
                    self.rig.store.patch(
                        POD, NAMESPACE, "engine-1", {"metadata": {"finalizers": None}}
                    )

        self.rig.on_tick = kubelet
        code, result = self.deploy("down")
        self.assertEqual(code, 0, result)
        self.assertEqual(len(waits), 3)
        names = {item["metadata"]["name"] for item in self.rig.store.live(POD, NAMESPACE)}
        self.assertNotIn("engine-1", names)

    def test_down_changes_nothing_for_another_fleet(self):
        self.assertEqual(self.deploy("run")[0], 0)
        deletes = len(self.rig.store.deletes)
        other = {kubernetes_target.FLEET_UID: "11111111-2222-4333-8444-555555555555"}
        with patch.dict(os.environ, other):
            code, result = self.deploy("down")
        self.assertEqual(code, 2, result)
        self.assertIn("load the environment the run used", result["errors"][0]["message"])
        record = self.record()
        record["target"] = "ssh"
        (self.run / "run.json").write_text(json.dumps(record))
        code, result = self.deploy("down")
        self.assertEqual(code, 2, result)
        self.assertIn("holds a ssh run", result["errors"][0]["message"])
        self.assertEqual(len(self.rig.store.deletes), deletes)
        self.assertIn("router-0", self.rig.pods)

    def test_plan_names_every_missing_input_before_the_cluster(self):
        env = {
            name: value
            for name, value in self.rig.env.items()
            if name not in ("NARWHAL_KUBERNETES_NAMESPACE", "NARWHAL_NODE_2_KUBERNETES_NODE")
        }
        with patch.dict(os.environ, env, clear=True):
            code, result = self.deploy("plan")
        self.assertEqual(code, 2, result)
        messages = [error["message"] for error in result["errors"]]
        self.assertTrue(any("NARWHAL_KUBERNETES_NAMESPACE" in m for m in messages), messages)
        self.assertTrue(any("KUBERNETES_NODE" in m for m in messages), messages)
        self.assertEqual(self.rig.requests, [])


class AdoptionTests(KubernetesTest):
    """A runner that ends mid-Job and starts again adopts the Job, and never runs it twice."""

    def test_a_restart_mid_job_adopts_the_running_job_and_does_not_stall(self):
        self.rig.hold = {"profile"}
        self.crash_when("profile")
        self.crashed()
        entry = self.record()["stages"]["profile"]
        self.assertEqual(entry["status"], "running")
        [name] = entry["jobs"]
        self.assertIsNotNone(self.rig.store.get(JOB, NAMESPACE, name))
        self.rig.hold = set()
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        entry = self.record()["stages"]["profile"]
        self.assertEqual((entry["status"], entry["attempt"], entry["jobs"]), ("passed", 1, [name]))
        self.assertEqual(entry["reasons"], ["no attempt yet"])
        self.assertEqual(self.rig.kinds.count("profile"), 1)
        self.assertNotIn("discover", result["data"]["ran"])
        # The adopted Job's evidence is the attempt's, and it went once the attempt passed.
        directory = self.run / entry["directory"]
        self.assertTrue((directory / "host" / "profiles.json").is_file())
        self.assertIsNone(self.rig.store._objects[JOB].get((NAMESPACE, name)))

    def test_an_interrupted_attempt_whose_jobs_are_gone_runs_again_and_removes_them(self):
        self.rig.hold = {"profile"}
        self.crash_when("profile")
        self.crashed()
        [name] = self.record()["stages"]["profile"]["jobs"]
        # Another client deleted the Job, leaving its inputs, while no runner ran.
        self.rig.store.delete(JOB, NAMESPACE, name, propagation_policy="Background")
        self.rig.settle()
        self.rig.hold = set()
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        entry = self.record()["stages"]["profile"]
        self.assertEqual(entry["attempt"], 2)
        self.assertEqual(entry["stale_jobs"], [])
        self.assertEqual(self.rig.kinds.count("profile"), 2)
        self.assertIsNone(self.rig.store._objects[CONFIG_MAP].get((NAMESPACE, name)))

    def test_every_multi_job_action_replays_safely(self):
        # Discovery starts four Jobs together; the runner ends while they run.
        self.rig.hold = {"fabric-probe"}
        self.crash_when("fabric-probe")
        self.crashed()
        names = self.record()["stages"]["discover"]["jobs"]
        self.assertEqual(len(names), 4)
        self.rig.hold = set()
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.rig.kinds[:5], ["key-check", *["discovery", "fabric-probe"] * 2])
        self.assertEqual(self.rig.kinds.count("discovery"), 2)
        self.assertEqual(self.record()["stages"]["discover"]["jobs"], names)
        # A readmission runs two Jobs in turn; the runner ends once the second exists.
        old = self.rig.pods["engine-2"]
        self.rig.pods["engine-2"] = {**old, "metadata": {**old["metadata"], "uid": "new-uid"}}

        def made() -> list[dict]:
            return [entry for entry in self.rig.created if entry["kind"] == "router-check"]

        checks = len(made())
        self.rig.hold = {"router-check"}
        saved: dict[str, bytes] = {}

        def second_check() -> None:
            started = len(made()) - checks
            if started == 1:
                # The readmission's Job finishes; the responses Job comes next.
                self.rig.released.add(made()[-1]["name"])
            elif started == 2 and not saved:
                saved["record"] = (self.run / "run.json").read_bytes()
                raise Crash

        self.rig.on_tick = second_check
        with self.assertRaises(Crash):
            self.deploy("run")
        self.left_behind(saved["record"])
        self.rig.on_tick = None
        self.rig.hold = set()
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        # The interrupted attempt recorded no readmission, so the replay starts the routers
        # in full. Its first check meets the readmission's Job under its name, which ran
        # another command: it is replaced, not adopted. The responses Job the replay did not
        # meet again goes with the attempt.
        replayed = made()[checks:]
        actions = [call(e)[0] for e in replayed]
        self.assertEqual(actions, ["readmit", "responses", "responses"])
        self.assertEqual(replayed[0]["name"], replayed[2]["name"])
        self.assertIn("--completion", call(replayed[2])[1])
        self.assertEqual(self.rig.live_jobs(), [])

    def test_a_signal_leaves_the_running_job_alone(self):
        self.rig.hold = {"profile"}

        def interrupt() -> None:
            if any(
                job["metadata"]["labels"][fleet_resource.GATE_LABEL] == "profile"
                for job in self.rig.live_jobs()
            ):
                raise KeyboardInterrupt

        self.rig.on_tick = interrupt
        code, result = self.deploy("run")
        self.assertEqual(code, 130, result)
        entry = self.record()["stages"]["profile"]
        self.assertEqual(entry["status"], "cancelled")
        [name] = entry["jobs"]
        self.assertIsNotNone(self.rig.store.get(JOB, NAMESPACE, name))
        self.assertNotIn(("Job", name), [(kind, job) for kind, job, _ in deletions(self.rig)])

    def test_a_cancelled_stage_deletes_its_jobs_and_the_pass_stops(self):
        self.rig.hold = {"profile"}
        deployment = self.deployment()
        runner = deployment.runner()
        stop = Stop()

        def cancel() -> None:
            if any(
                job["metadata"]["labels"][fleet_resource.GATE_LABEL] == "profile"
                for job in self.rig.live_jobs()
            ):
                stop.cancel("profile")

        self.rig.on_tick = cancel
        result = runner.run(kubernetes_target.inputs(self.rig.env), stop=stop)
        self.assertTrue(result.stopped)
        entry = self.record()["stages"]["profile"]
        self.assertEqual(entry["status"], "cancelled")
        [name] = entry["jobs"]
        self.assertIn(("Job", name, "Background"), deletions(self.rig))
        self.rig.settle()
        self.assertEqual(self.rig.live_jobs(), [])

    def test_a_cancelled_stage_ends_only_once_its_jobs_pods_are_gone(self):
        # The pod of a deleted Job holds its node's resources until the kubelet ends it, so the
        # next attempt's Job on that node would be refused at admission.
        self.rig.hold = {"profile"}
        deployment = self.deployment()
        runner = deployment.runner()
        stop = Stop()
        held: dict[str, str] = {}
        waits = []

        def pods(job: str) -> list[dict]:
            return [
                pod
                for pod in self.rig.store.live(POD, NAMESPACE)
                if pod["metadata"]["labels"].get(kubernetes_target.JOB_NAME_LABEL) == job
            ]

        def cancel() -> None:
            if not held:
                for job in active(self.rig, "profile"):
                    for pod in pods(job["metadata"]["name"]):
                        held[job["metadata"]["name"]] = pod["metadata"]["name"]
                        self.rig.store.patch(
                            POD,
                            NAMESPACE,
                            pod["metadata"]["name"],
                            {"metadata": {"finalizers": ["example.com/kubelet"]}},
                        )
                if held:
                    stop.cancel("profile")
                return
            [(job, pod)] = held.items()
            jobs = {item["metadata"]["name"] for item in self.rig.store.live(JOB, NAMESPACE)}
            if job not in jobs and pods(job):
                # The kubelet ends the pod a few waits after its Job is gone.
                waits.append(1)
                if len(waits) == 3:
                    self.rig.store.patch(POD, NAMESPACE, pod, {"metadata": {"finalizers": None}})

        self.rig.on_tick = cancel
        result = runner.run(kubernetes_target.inputs(self.rig.env), stop=stop)
        self.assertTrue(result.stopped)
        self.assertEqual(self.record()["stages"]["profile"]["status"], "cancelled")
        [job] = held
        self.assertEqual(len(waits), 3)
        self.assertEqual(pods(job), [])

    def test_a_job_of_the_name_another_owner_controls_is_refused(self):
        uid = self.rig.env[kubernetes_target.FLEET_UID]
        name = objects.job_name("discovery", uid, "discover", "", 1, 0)
        self.rig.store.create(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{"name": "gate", "image": "busybox"}],
                        }
                    }
                },
            }
        )
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assertEqual(result["data"]["failed_stage"], "discover")
        self.assertIn("another owner controls it", result["errors"][0]["message"])
        # A restart does not adopt it either.
        deployment = self.deployment()
        with self.assertRaisesRegex(ValueError, "another owner controls it"):
            deployment.adopt({"stage": "discover", "instance": "", "jobs": [name]})


def active(rig: Rig, kind: str) -> list[dict]:
    """The running gate Jobs of `kind`."""
    return [
        job
        for job in rig.live_jobs()
        if job["metadata"]["labels"][fleet_resource.GATE_LABEL] == kind
        and (job.get("status") or {}).get("active")
    ]


class ReviewTests(KubernetesTest):
    """The review of the first Cluster target: each test fails on that target."""

    def test_a_new_run_directory_replaces_another_runs_job_of_its_name(self):
        # A run ends while its discovery Jobs run; a new run directory for the same fleet
        # names its own discovery Jobs alike.
        self.rig.hold = {"discovery"}
        self.crash_when("discovery")
        self.crashed()
        first = self.record()["run_id"]
        [held, *_] = self.record()["stages"]["discover"]["jobs"]
        earlier = self.rig.store.get(JOB, NAMESPACE, held)["metadata"]["uid"]
        self.rig.hold = set()
        self.run = self.root / "run-b"
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        second = self.record()["run_id"]
        # The earlier run's Job had another spec digest, so it was deleted by its uid and the
        # new run's Job made in its place, keeping the new run's evidence.
        made = [entry for entry in self.rig.created if entry["name"] == held]
        self.assertEqual(len(made), 2)
        self.assertIn(f"deploy/{first}/", made[0]["keep"])
        self.assertIn(f"deploy/{second}/", made[1]["keep"])
        self.assertIn((held, earlier), self.rig.preconditions)
        job = self.rig.jobs[held]["job"]
        self.assertIn(kubernetes_target.SPEC_ANNOTATION, job["metadata"]["annotations"])

    def test_every_delete_names_the_uid_it_read(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.assertTrue(self.rig.preconditions)
        self.assertTrue(all(uid for _, uid in self.rig.preconditions))

    def test_a_brief_api_outage_mid_stage_is_waited_out(self):
        self.rig.hold = {"profile"}
        outage = {}

        def go_down() -> None:
            if not outage and active(self.rig, "profile"):
                outage["at"] = self.rig.store.clock()
                self.rig.outage_until = outage["at"] + 30
                # The Job finishes while the API is down.
                self.rig.hold = set()

        self.rig.on_tick = go_down
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertGreater(self.rig.calls["refused in the outage"], 0)
        self.assertGreaterEqual(self.rig.store.clock(), outage["at"] + 30)
        self.assertEqual(self.rig.kinds.count("profile"), 1)

    def test_an_unreadable_log_interrupts_the_attempt_and_the_next_pass_adopts_the_job(self):
        self.rig.hold = {"profile"}

        def refuse_logs() -> None:
            if active(self.rig, "profile") and not self.rig.calls["logs refused"]:
                self.rig.calls["logs refused"] += 1
                self.rig.hold = set()
                self.rig.store.fail(
                    "logs", POD, code=403, message="forbidden", times=kubernetes_target.LOG_TRIES
                )

        self.rig.on_tick = refuse_logs
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        entry = self.record()["stages"]["profile"]
        self.assertEqual(entry["status"], "interrupted")
        self.assertIn("its log could not be read", entry["failure"]["message"])
        [name] = entry["jobs"]
        self.assertIsNotNone(self.rig.store.get(JOB, NAMESPACE, name))
        self.rig.on_tick = None
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        entry = self.record()["stages"]["profile"]
        self.assertEqual((entry["status"], entry["attempt"]), ("passed", 1))
        self.assertEqual(self.rig.kinds.count("profile"), 1)

    def test_a_router_pod_that_waits_for_a_node_a_while_still_starts(self):
        label = {"example.com/router": "yes"}
        self.rig.router_selector = label
        waited = {}

        def add_node() -> None:
            pending = self.rig.pods.get("router-0")
            if pending is None:
                return
            waited.setdefault("since", self.rig.store.clock())
            if self.rig.store.clock() - waited["since"] > 60 and "done" not in waited:
                waited["done"] = True
                node = copy.deepcopy(self.rig.store.get(kubernetes_node_kind(), None, "cpu-a"))
                node["metadata"]["labels"].update(label)
                self.rig.store.put(node)

        self.rig.on_tick = add_node
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertTrue(waited.get("done"))

    def test_a_router_pod_no_node_takes_for_minutes_fails_router_start(self):
        self.rig.router_selector = {"example.com/router": "yes"}
        self.assertEqual(self.deploy("run", "--until", "preflight")[0], 0)
        start = self.rig.store.clock()
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assertEqual(result["data"]["failed_stage"], "router-start")
        self.assertIn("no node can schedule the pod", result["errors"][0]["message"])
        self.assertGreater(
            self.rig.store.clock() - start, kubernetes_target.ROUTER_UNSCHEDULABLE_SECONDS
        )

    def test_a_key_edited_in_place_reruns_the_engines_and_the_routers(self):
        self.assertEqual(self.deploy("run")[0], 0)
        kept = json.loads((self.run / kubernetes_target.CREDENTIAL_FILE).read_text())
        self.assertEqual(set(kept), {"salt", "hmac", "control_hmac"})
        self.rig.set_key("rotated-in-place")
        code, result = self.deploy("plan")
        # Plan reads no key, so it cannot see the edit; run can.
        self.assertNotIn(
            "engine-prepare[engine-1]", [r["stage"] for r in result["data"]["stages"] if r["runs"]]
        )
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        ran = set(result["data"]["ran"])
        self.assertTrue({f"engine-prepare[{role}]" for role in ENGINES} <= ran)
        self.assertIn("router-start", ran)
        self.assertFalse({"access", "discover"} & ran)
        # The run directory keeps the salt and the new HMAC, and neither the key nor its plain
        # hash.
        now = json.loads((self.run / kubernetes_target.CREDENTIAL_FILE).read_text())
        self.assertEqual(now["salt"], kept["salt"])
        self.assertNotEqual(now["hmac"], kept["hmac"])
        stored = (self.run / kubernetes_target.CREDENTIAL_FILE).read_text()
        for key in (SECRET, "rotated-in-place"):
            self.assertNotIn(key, stored)
            self.assertNotIn(hashlib.sha256(key.encode()).hexdigest(), stored)
        self.assertEqual(stat.S_IMODE((self.run / "credential.json").stat().st_mode), 0o600)
        code, result = self.deploy("run")
        self.assertEqual(result["data"]["ran"], [])

    def test_a_resumed_sample_whose_server_window_passed_is_interrupted(self):
        self.assertEqual(self.deploy("run")[0], 0)
        hosts = {role: Host(node, "", None, (role,)) for role, node in NODES.items()}
        endpoints = {
            role: fabric.Endpoint(hosts[role], f"10.0.0.{role[-1]}", "f0", "ucx_tcp", None)
            for role in NODES
        }
        engines = {
            role: fabric.Engine(
                role, hosts[role], endpoints[role], "f0", 2, Path("l"), {}, {}, True, False
            )
            for role in NODES
        }
        plan = fabric.Fabric(engines, (), None, 5201, "/tmp", {}, {})  # type: ignore[arg-type]
        sample = fabric.Sample(
            endpoints["engine-1"], endpoints["engine-2"], ("engine-1",), ("engine-2",)
        )
        context = kubernetes_target._LiveContext(self.run)
        first = self.deployment()
        kubernetes_target.JobShell(first, context).sample(plan, "e", sample)  # type: ignore[arg-type]
        server, client = [e["name"] for e in self.rig.created if e["kind"].startswith("fabric-")][
            -2:
        ]
        # The runner ended after the server's window and before its client: a resumed attempt
        # finds the finished server and no client.
        if self.rig.store._objects[JOB].get((NAMESPACE, client)) is not None:
            self.rig.store.remove(JOB, NAMESPACE, client)
        self.rig.store.put(self.finished(server))
        context.resumed = True  # type: ignore[attr-defined]
        # A resumed attempt starts from an empty directory.
        shutil.rmtree(context.directory)
        again = self.deployment()
        with self.assertRaisesRegex(kubernetes_target.Interrupted, "window passed"):
            kubernetes_target.JobShell(again, context).sample(plan, "e", sample)  # type: ignore[arg-type]
        self.rig.settle()
        self.assertIsNone(self.rig.store._objects[JOB].get((NAMESPACE, server)))

    def finished(self, name: str) -> dict:
        job = copy.deepcopy(self.rig.store.get(JOB, NAMESPACE, name))
        job["status"] = {
            "succeeded": 1,
            "conditions": [{"type": "Complete", "status": "True"}],
        }
        return job

    def test_a_pod_waiting_on_its_claim_or_a_passing_runtime_error_is_not_failed(self):
        unbound = {
            "status": {
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/3 nodes are available: pod has unbound immediate "
                        "PersistentVolumeClaims.",
                    }
                ]
            }
        }
        self.assertIsNone(kubernetes_target.never_runs(unbound, scheduled=True))
        created = {
            "status": {
                "containerStatuses": [
                    {"name": "gate", "state": {"waiting": {"reason": "CreateContainerError"}}}
                ]
            }
        }
        self.assertIsNone(kubernetes_target.never_runs(created, scheduled=False))


class ApiFailureTests(KubernetesTest):
    """Every write whose reply is lost is read back before the run decides, and every
    injected failure below is met."""

    def assert_met(self) -> None:
        self.assertEqual(self.rig.store.faults, [])

    def test_a_job_create_whose_reply_was_lost_is_read_back_not_made_twice(self):
        # The first create lands and its reply is lost; the second is refused before it lands.
        self.rig.store.fail("create", JOB, code=503, commit=True)
        self.rig.store.fail("create", JOB, code=503, commit=False)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assert_met()
        self.assertEqual(self.rig.kinds, JOBS)
        self.assertEqual(len(self.rig.created), len(JOBS))

    def test_an_apply_or_delete_whose_reply_was_lost_is_made_again(self):
        self.rig.store.fail("apply", CONFIG_MAP, code=503, commit=True)
        self.rig.store.fail("apply", STATEFUL_SET, code=504)
        self.rig.store.fail("delete", JOB, code=503, commit=False)
        self.rig.store.fail("delete", CONFIG_MAP, code=500, commit=True)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assert_met()
        self.assertEqual(self.rig.live_jobs(), [])
        self.assertEqual(self.rig.config_maps, {})

    def test_a_list_whose_resource_version_expired_is_read_again(self):
        self.rig.store.fail("list", POD, code=410, times=2)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assert_met()

    def test_a_job_whose_name_is_taken_but_missing_fails_its_stage(self):
        # A 409 for a Job no read then finds: the name is held but no object answers to it.
        uid = self.rig.env[kubernetes_target.FLEET_UID]
        name = objects.job_name("discovery", uid, "discover", "", 1, 0)
        self.rig.store.fail("create", JOB, name=name, code=409, message="already exists")
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assert_met()
        self.assertEqual(result["data"]["failed_stage"], "discover")

    def test_a_job_being_deleted_under_its_name_is_waited_out(self):
        # A Job of the name still terminates when the attempt reaches it: the run waits until
        # it is gone and creates its own.
        uid = self.rig.env[kubernetes_target.FLEET_UID]
        name = objects.job_name("discovery", uid, "discover", "", 1, 0)
        deployment = self.deployment()
        gate = objects.GateJob(
            name=name,
            kind="discovery",
            command=["true"],
            deadline_seconds=60,
            keep="deploy/earlier/jobs/x",
            node="gpu-a",
        )
        job = objects.job(deployment.fleet(), gate)
        job["metadata"]["finalizers"] = ["example.com/hold"]
        self.rig.store.create(job)
        self.rig.store.delete(JOB, NAMESPACE, name, propagation_policy="Background")

        waits = []

        def release() -> None:
            # Its finalizer's controller lets it go once the run has waited for it.
            waits.append(1)
            held = self.rig.store._objects[JOB].get((NAMESPACE, name))
            if held is not None and len(waits) == 3:
                self.rig.store.remove(JOB, NAMESPACE, name)

        self.rig.on_tick = release
        code, result = self.deploy("run", "--until", "discover")
        self.assertEqual(code, 0, result)
        self.assertGreaterEqual(len(waits), 3)
        earlier, made = [e for e in self.rig.created if e["name"] == name]
        self.assertEqual(earlier["command"][-1], "true")
        self.assertNotEqual(made["command"][-1], "true")


class FailFastTests(KubernetesTest):
    """A pod that can never run fails its stage at once, not at its time limit."""

    def assert_failed_fast(self, stage: str, cause: str, result: dict) -> None:
        self.assertEqual(result["data"]["failed_stage"], stage, result)
        self.assertIn(cause, result["errors"][0]["message"])

    def test_a_job_whose_container_configuration_cannot_be_built_fails_at_once(self):
        self.rig.outcomes["discovery"] = Wait("CreateContainerConfigError", 'secret "x" not found')
        start = self.rig.store.clock()
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assert_failed_fast("discover", "CreateContainerConfigError", result)
        self.assertLess(self.rig.store.clock() - start, 60)
        # The Job is deleted, so its pod holds nothing on its node. The attempt's other Jobs
        # stay for inspection, as a failed attempt's do.
        self.rig.settle()
        [name] = re.findall(r"Job (discovery-[0-9a-f]+)", result["errors"][0]["message"])
        live = [job["metadata"]["name"] for job in self.rig.live_jobs()]
        self.assertNotIn(name, live)
        self.assertEqual(len(live), 3)

    def test_a_pinned_job_the_kubelet_refuses_fails_with_the_reason(self):
        self.rig.outcomes["discovery"] = Reject(
            "OutOfgpu.example.com/gpu", "Node didn't have enough resource"
        )
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assert_failed_fast("discover", "refused the pod: OutOfgpu.example.com/gpu", result)

    def test_an_engine_pod_no_node_can_schedule_fails_its_start(self):
        node = self.rig.store.get(kubernetes_node_kind(), None, "gpu-b")
        self.assertEqual(self.deploy("run", "--until", "install")[0], 0)
        node["spec"] = {"unschedulable": True}
        self.rig.store.put(node)
        start = self.rig.store.clock()
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assert_failed_fast("engine-start[engine-2]", "no node can schedule the pod", result)
        self.assertLess(self.rig.store.clock() - start, 60)

    def test_an_engine_container_whose_configuration_cannot_be_built_fails_its_start(self):
        self.rig.pod_outcomes["engine-1"] = Wait("CreateContainerConfigError", "no Secret")
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assert_failed_fast("engine-start[engine-1]", "CreateContainerConfigError", result)

    def test_a_router_pod_no_node_can_schedule_fails_router_start(self):
        self.rig.pod_outcomes["router-0"] = Reject("OutOfcpu", "Node didn't have enough cpu")
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assert_failed_fast("router-start", "refused the pod: OutOfcpu", result)


class CredentialTests(KubernetesTest):
    """The engine API key stays in its Secret: the runner never holds it."""

    def test_no_object_log_or_run_file_holds_the_key(self):
        self.assertEqual(self.deploy("run")[0], 0)
        secret = SECRET.encode()
        for path in self.run.rglob("*"):
            if path.is_file():
                with self.subTest(path=str(path.relative_to(self.run))):
                    self.assertNotIn(secret, path.read_bytes())
        for kind in DERIVED:
            for obj in self.rig.store.live(kind, NAMESPACE):
                self.assertNotIn(SECRET, json.dumps(obj))
        # Every file a Job's inputs ConfigMap carried, and every Job's spec.
        for job in self.rig.jobs.values():
            for data in self.rig.files(job["entry"]).values():
                self.assertNotIn(secret, data, job["entry"]["name"])
            self.assertNotIn(SECRET, json.dumps(job["job"]))
        for logs in self.rig.store._logs.values():
            self.assertNotIn(SECRET, logs)

    def test_only_the_jobs_that_call_engines_hold_the_key(self):
        self.assertEqual(self.deploy("run")[0], 0)
        keyed = {
            entry["kind"]
            for job in self.rig.jobs.values()
            for entry in (job["entry"],)
            if "NARWHAL_ENGINE_KEY" in entry["env"]
        }
        self.assertEqual(
            keyed,
            {"key-check", "sidecar-verify", "finalize-fleet", "profile", "calibrate", "preflight"},
        )
        for job in self.rig.jobs.values():
            for item in job["entry"]["env"].values():
                self.assertNotIn("value", item if "valueFrom" in item else {})
        # The prepare Job's role file names the Secret and neither the key nor its variable.
        for entry in self.entries("prepare"):
            role = unpack(self.rig.inputs[entry["name"]])[".env"]
            self.assertIn(b"NARWHAL_KUBERNETES_API_KEY_SECRET=engine-key-v1/api-key", role)
            self.assertNotIn(b"NARWHAL_ENGINE_API_KEY", role)
            self.assertNotIn(b"NARWHAL_ENGINE_KEY", role)

    def test_an_environment_that_holds_the_key_is_refused(self):
        with patch.dict(os.environ, {"NARWHAL_ENGINE_KEY": SECRET}):
            code, result = self.deploy("plan")
        self.assertEqual(code, 2, result)
        self.assertTrue(
            any("holds the engine API key" in e["message"] for e in result["errors"]), result
        )

    def test_a_new_secret_reruns_the_engines_and_the_routers(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.rig.store.create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "engine-key-v2", "namespace": NAMESPACE},
                "data": {"api-key": "c2Vjb25k"},
            }
        )
        rotated = {"NARWHAL_KUBERNETES_API_KEY_SECRET": "engine-key-v2/api-key"}
        self.rig.secret = {"name": "engine-key-v2", "key": "api-key"}
        with patch.dict(os.environ, rotated):
            code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        ran = set(result["data"]["ran"])
        self.assertTrue({f"engine-prepare[{role}]" for role in ENGINES} <= ran)
        self.assertIn("router-start", ran)
        self.assertFalse({"access", "discover"} & ran)
        [router] = [obj for obj in self.derived() if obj["metadata"]["name"] == "router"]
        env = router["spec"]["template"]["spec"]["containers"][0]["env"]
        [key] = [item for item in env if item["name"] == "NARWHAL_ENGINE_KEY"]
        self.assertEqual(key["valueFrom"]["secretKeyRef"]["name"], "engine-key-v2")


class ControlTokenTests(KubernetesTest):
    """The routers' control token stays in its Secret, reaches the routers and the Jobs that
    call their control routes, and a change of it restarts the routers alone."""

    def test_no_object_log_or_run_file_holds_the_token(self):
        self.assertEqual(self.deploy("run")[0], 0)
        token = CONTROL_SECRET.encode()
        for path in self.run.rglob("*"):
            if path.is_file():
                with self.subTest(path=str(path.relative_to(self.run))):
                    self.assertNotIn(token, path.read_bytes())
                    self.assertNotIn(hashlib.sha256(token).hexdigest().encode(), path.read_bytes())
        for kind in DERIVED:
            for obj in self.rig.store.live(kind, NAMESPACE):
                self.assertNotIn(CONTROL_SECRET, json.dumps(obj))
        for job in self.rig.jobs.values():
            for data in self.rig.files(job["entry"]).values():
                self.assertNotIn(token, data, job["entry"]["name"])
            self.assertNotIn(CONTROL_SECRET, json.dumps(job["job"]))
        for logs in self.rig.store._logs.values():
            self.assertNotIn(CONTROL_SECRET, logs)

    def test_the_routers_and_the_router_checks_read_the_token_from_its_secret(self):
        self.assertEqual(self.deploy("run")[0], 0)
        held = {
            job["entry"]["kind"]
            for job in self.rig.jobs.values()
            if ROUTER_TOKEN_ENV in job["entry"]["env"]
        }
        self.assertEqual(held, {"key-check", "router-check"})
        [router] = [obj for obj in self.derived() if obj["metadata"]["name"] == "router"]
        env = router["spec"]["template"]["spec"]["containers"][0]["env"]
        [token] = [item for item in env if item["name"] == ROUTER_TOKEN_ENV]
        self.assertEqual(token["valueFrom"]["secretKeyRef"], self.rig.control_secret)

    def test_a_token_edited_in_place_restarts_only_the_routers(self):
        self.assertEqual(self.deploy("run")[0], 0)
        kept = json.loads((self.run / kubernetes_target.CREDENTIAL_FILE).read_text())
        self.rig.set_key("d4" * 32, self.rig.control_secret)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["data"]["ran"]), {"router-start"})
        now = json.loads((self.run / kubernetes_target.CREDENTIAL_FILE).read_text())
        self.assertEqual((now["salt"], now["hmac"]), (kept["salt"], kept["hmac"]))
        self.assertNotEqual(now["control_hmac"], kept["control_hmac"])
        self.assertEqual(self.deploy("run")[1]["data"]["ran"], [])

    def test_a_new_secret_restarts_only_the_routers(self):
        self.assertEqual(self.deploy("run")[0], 0)
        self.rig.store.create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "router-token-v2", "namespace": NAMESPACE},
                "data": {"token": base64.b64encode(b"e5" * 32).decode()},
            }
        )
        self.rig.control_secret = {"name": "router-token-v2", "key": "token"}
        rotated = {fleet_resource.CONTROL_TOKEN_SECRET_ENV: "router-token-v2/token"}
        with patch.dict(os.environ, rotated):
            code, plan = self.deploy("plan")
            self.assertEqual(code, 0, plan)
            self.assertEqual(
                {row["stage"] for row in plan["data"]["stages"] if row["runs"]}, {"router-start"}
            )
            code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["data"]["ran"]), {"router-start"})
        [router] = [obj for obj in self.derived() if obj["metadata"]["name"] == "router"]
        env = router["spec"]["template"]["spec"]["containers"][0]["env"]
        [token] = [item for item in env if item["name"] == ROUTER_TOKEN_ENV]
        self.assertEqual(token["valueFrom"]["secretKeyRef"]["name"], "router-token-v2")

    def test_a_fleet_without_the_token_secret_or_an_environment_holding_it_is_refused(self):
        for change, message in (
            ({fleet_resource.CONTROL_TOKEN_SECRET_ENV: ""}, "spec.controlTokenSecret"),
            ({ROUTER_TOKEN_ENV: CONTROL_SECRET}, "holds the router control token"),
        ):
            with self.subTest(message), patch.dict(os.environ, change):
                code, result = self.deploy("plan")
            self.assertEqual(code, 2, result)
            self.assertTrue(any(message in e["message"] for e in result["errors"]), result)


class InputTests(KubernetesTest):
    def test_each_input_edit_reruns_the_stages_its_fact_names(self):
        self.assertEqual(self.deploy("run")[0], 0)
        with patch.dict(os.environ, {"NARWHAL_TTFT_S": "5"}):
            code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["data"]["ran"]), {"fleet-limits", "preflight", "router-start"})
        self.assertEqual(
            [rerun.stage for rerun in KUBERNETES_GRAPH.reruns("slo")],
            ["fleet-limits", "preflight", "router-start"],
        )

    def test_each_variable_takes_the_fact_the_fleet_gives_it(self):
        found = kubernetes_target.inputs(self.rig.env)
        self.assertEqual(
            set(found), {n for n, f in KUBERNETES_GRAPH.facts.items() if f.kind == "input"}
        )
        # Each variable of the fleet lands in exactly the fact it names, so changing it
        # changes that fact's digest and no other.
        for name, value in self.rig.env.items():
            fact = kubernetes_target.fact_of(name)
            if fact is None or name == kubernetes_target.FLEET_UID:
                continue
            with self.subTest(name):
                changed = kubernetes_target.inputs({**self.rig.env, name: value})
                self.assertEqual(changed[fact].digest, found[fact].digest)
                moved = {
                    other
                    for other, digest in kubernetes_target.inputs(
                        {**self.rig.env, name: edit(name, value)}, policy=WIDE
                    ).items()
                    if digest.digest != found[other].digest
                }
                self.assertEqual(moved, {fact})
        self.assertEqual(
            kubernetes_target.fact_of("NARWHAL_KUBERNETES_API_KEY_SECRET"), "credential"
        )
        self.assertEqual(
            kubernetes_target.fact_of(fleet_resource.CONTROL_TOKEN_SECRET_ENV),
            "control_credential",
        )
        self.assertEqual(kubernetes_target.fact_of("NARWHAL_KUBERNETES_FLEET_UID"), "cluster")
        self.assertEqual(kubernetes_target.fact_of("NARWHAL_KUBECTL"), "cluster")
        self.assertEqual(kubernetes_target.fact_of("NARWHAL_KUBERNETES_ROUTERS"), "router")
        self.assertEqual(kubernetes_target.fact_of("NARWHAL_ROUTER_URL"), "router")
        self.assertEqual(kubernetes_target.fact_of("NARWHAL_RDMA_GID_INDEX"), "fabric_test")
        self.assertIsNone(kubernetes_target.fact_of("NARWHAL_STAGE_PROFILE_TIMEOUT_SECONDS"))
        # The Helm inputs the target read before the fleet are refused, not recorded.
        for name in kubernetes_target.RETIRED:
            with self.subTest(name), self.assertRaises(kubernetes_target.InputError) as raised:
                kubernetes_target.inputs({**self.rig.env, name: "/tmp/values.json"})
            self.assertIn(f"unset {name}", raised.exception.problems[0])

    def test_the_admins_volume_policy_refuses_a_model_volume_it_does_not_allow(self):
        host = {
            **self.rig.env,
            "NARWHAL_KUBERNETES_MODEL_VOLUME": json.dumps(
                {"hostPath": {"path": "/srv/models", "type": "Directory"}}
            ),
        }
        with self.assertRaises(kubernetes_target.InputError) as raised:
            kubernetes_target.inputs(host)
        self.assertTrue(any("modelVolumeSources" in p for p in raised.exception.problems))
        allowed = VolumePolicy(sources=("hostPath",), host_path_prefixes=("/srv/models",))
        kubernetes_target.inputs(host, policy=allowed)
        with self.assertRaises(kubernetes_target.InputError):
            kubernetes_target.inputs(
                {**host, "NARWHAL_KUBERNETES_MODEL_VOLUME": '{"hostPath": {"path": "/etc"}}'},
                policy=allowed,
            )


# A policy that allows the edited model volume below.
WIDE = VolumePolicy(sources=("persistentVolumeClaim", "hostPath"), host_path_prefixes=("/srv",))


def edit(name: str, value: str) -> str:
    """Another valid value of the fleet variable `name`."""
    if value.startswith("{"):
        if name == "NARWHAL_KUBERNETES_MODEL_VOLUME":
            return json.dumps({"hostPath": {"path": "/srv/models", "type": "Directory"}})
        return json.dumps({**json.loads(value), "edited": True})
    if "@sha256:" in value:
        return value.rsplit(":", 1)[0] + ":" + "9" * 64
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return "9" * 64
    if re.fullmatch(r"[0-9]+(,[0-9]+)*", value):
        *rest, last = value.split(",")
        return ",".join([*rest, str(int(last) + 1)])
    if value.replace(".", "", 1).isdigit():
        return str(float(value) + 1)
    if "/" in value and name.endswith("_SECRET"):
        return "other-secret/other-key"
    return value + "x" if not value.startswith("http") else value.replace(":8", ":9")


class RbacTests(KubernetesTest):
    def test_a_run_and_its_down_use_only_the_verbs_the_rules_grant(self):
        # A misplaced engine pod is deleted, so the run uses every verb it may.
        self.rig.misplace = {"engine-2": "gpu-c"}
        self.assertEqual(self.deploy("run")[0], 0)
        old = self.rig.pods["engine-2"]
        self.rig.pods["engine-2"] = {**old, "metadata": {**old["metadata"], "uid": "new-uid"}}
        self.assertEqual(self.deploy("run")[0], 0)
        self.assertEqual(self.deploy("status")[0], 0)
        self.assertEqual(self.deploy("down")[0], 0)
        granted = {
            (verb, group, resource)
            for rule in (*kubernetes_target.RULES, *kubernetes_target.CLUSTER_RULES)
            for verb in rule["verbs"]
            for group in rule["apiGroups"]
            for resource in rule["resources"]
        }
        used = set()
        for verb, group, resource in self.rig.requests:
            for needed in kubernetes_target.VERBS[verb]:
                used.add((needed, group, resource))
        self.assertLessEqual(used, granted)
        # Nothing reads a Secret, and nothing writes RBAC or runs a command in a pod.
        resources = {resource for _, _, resource in self.rig.requests}
        self.assertNotIn("secrets", resources)
        self.assertFalse({"roles", "rolebindings", "pods/exec"} & resources)
        # Every rule is used, so the chart grants nothing the runner does not need.
        self.assertEqual(granted - used, set())


class KubectlClusterTests(unittest.TestCase):
    """The workstation's `Cluster`: each verb as kubectl takes it, and kubectl's failures as
    the API's errors."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.calls = self.root / "calls"
        self.kubectl = self.root / "kubectl"
        self.answer("")

    def answer(self, stdout: str, stderr: str = "", status: int = 0) -> None:
        script = (
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            f"cat > /dev/null\nprintf %s {json.dumps(stdout)}\n"
            f"printf %s {json.dumps(stderr)} >&2\nexit {status}\n"
        )
        self.kubectl.write_text(script)
        self.kubectl.chmod(stat.S_IRWXU)

    def cluster(self) -> kubernetes_target.KubectlCluster:
        env = {
            kubernetes_target.NAMESPACE: NAMESPACE,
            kubernetes_target.KUBECTL: str(self.kubectl),
            kubernetes_target.CONTEXT: "admin@cluster",
        }
        return kubernetes_target.KubectlCluster(env, self.root / "logs")

    def last(self) -> str:
        return self.calls.read_text().splitlines()[-1]

    def test_each_verb_runs_kubectl_in_the_namespace_and_context(self):
        cluster = self.cluster()
        self.assertIsNone(cluster.get(JOB, "gate"))
        self.assertEqual(
            self.last(),
            "get --context admin@cluster --namespace fleet jobs.batch gate --ignore-not-found "
            "--output json",
        )
        self.answer(json.dumps({"items": [{"metadata": {"name": "a"}}]}))
        [item] = cluster.list(POD, {"app": "x"})
        self.assertEqual((item["kind"], item["apiVersion"]), ("Pod", "v1"))
        self.assertIn("--selector app=x", self.last())
        self.answer("{}")
        cluster.apply(CONFIG_MAP, {"kind": "ConfigMap"})
        self.assertIn("apply", self.last())
        self.assertIn(
            "--server-side --force-conflicts --field-manager=narwhal-controller", self.last()
        )
        self.answer("job.batch/gate deleted")
        self.assertTrue(cluster.delete(JOB, "gate"))
        self.assertIn("--cascade=background", self.last())
        # A delete of one uid goes to the object's path with its precondition on stdin.
        self.answer("{}")
        self.assertTrue(cluster.delete(kubernetes_target.JOB, "gate", uid="u-1"))
        self.assertIn("delete --context admin@cluster --namespace fleet --raw", self.last())
        self.assertIn("/apis/batch/v1/namespaces/fleet/jobs/gate -f -", self.last())
        self.answer("", 'Error from server (NotFound): jobs.batch "gate" not found', 1)
        self.assertFalse(cluster.delete(kubernetes_target.JOB, "gate", uid="u-1"))

    def test_kubectl_failures_become_the_apis_errors(self):
        cluster = self.cluster()
        self.answer("", 'Error from server (AlreadyExists): jobs.batch "gate" already exists', 1)
        with self.assertRaises(kubernetes_target.Conflict):
            cluster.create(JOB, {"kind": "Job"})
        self.answer("", "Unable to connect to the server: i/o timeout", 1)
        with self.assertRaises(kubernetes_target.OutcomeUnknown):
            cluster.create(JOB, {"kind": "Job"})
        with self.assertRaises(kubernetes_target.Unavailable):
            cluster.get(JOB, "gate")
        self.answer("", "error: You must be logged in", 1)
        with self.assertRaisesRegex(kubernetes_target.ClusterError, "logged in"):
            cluster.get(JOB, "gate")


class StubClusterTests(unittest.TestCase):
    """The stub refuses what the cluster refuses, so the tests above cannot pass on objects or
    patches a real cluster would reject."""

    def setUp(self):
        self.rig = Rig(Path(tempfile.mkdtemp()))
        self.cluster = self.rig.cluster(self.rig.env, self.rig.root / "logs")

    def test_a_router_check_fails_while_no_router_runs(self):
        deployment = kubernetes_target.KubernetesDeployment(self.rig.root / "run", self.rig.env)
        fleet = deployment.fleet()
        gate = objects.GateJob(
            name="check",
            kind="router-check",
            command=["true"],
            deadline_seconds=1,
            keep="deploy/run/jobs/check",
        )
        self.cluster.create(JOB, objects.job(fleet, gate))
        self.rig.settle()
        job = self.rig.store.get(JOB, NAMESPACE, "check")
        self.assertEqual(job["status"]["conditions"][0]["type"], "Failed")

    def test_only_a_gated_pod_takes_node_affinity(self):
        self.rig.store.create(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "engine-1", "namespace": NAMESPACE},
                "spec": {
                    "schedulingGates": [{"name": kubernetes_target.PLACEMENT_GATE}],
                    "containers": [{"name": "engine", "image": "x"}],
                },
            }
        )
        affinity = {"nodeAffinity": {"x": 1}}
        self.cluster.patch(
            POD, "engine-1", [{"op": "add", "path": "/spec/affinity", "value": affinity}]
        )
        self.cluster.patch(POD, "engine-1", [{"op": "remove", "path": "/spec/schedulingGates/0"}])
        with self.assertRaises(kubernetes_target.ClusterError):
            self.cluster.patch(
                POD, "engine-1", [{"op": "replace", "path": "/spec/affinity", "value": {}}]
            )


class GateTests(unittest.TestCase):
    def test_a_gate_returns_its_files_and_the_runner_checks_them(self):
        log = "noise\n" + emitted({"a.json": b"{}", "dir/b": b"x"})
        self.assertEqual(kubernetes_target.gate_files(log), {"a.json": b"{}", "dir/b": b"x"})
        with self.assertRaisesRegex(ValueError, "truncated"):
            kubernetes_target.gate_files(log.rsplit("narwhal-manifest", 1)[0])
        dropped = "\n".join(line for line in log.splitlines() if " dir/b " not in line)
        with self.assertRaisesRegex(ValueError, "missing"):
            kubernetes_target.gate_files(dropped)

    def test_the_job_wrapper_unpacks_inputs_and_returns_evidence_of_a_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "inputs.tar.gz"
            archive.write_bytes(kubernetes_gate.pack({"config/a.txt": b"hello"}))
            into = root / "in"
            script = f"cp {into}/config/a.txt {root}/out.txt; echo report; exit 1"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "narwhal.deployment.kubernetes_gate",
                    "run",
                    "--inputs",
                    str(archive),
                    "--into",
                    str(into),
                    "--return",
                    f"out.txt={root}/out.txt",
                    "--",
                    "sh",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(kubernetes_target.gate_files(result.stdout), {"out.txt": b"hello"})
            self.assertNotIn("report", result.stdout)
            self.assertIn("exited with status 1", result.stderr)

    def test_every_job_keeps_its_output_and_status_on_the_state_volume(self):
        with tempfile.TemporaryDirectory() as folder:
            keep = Path(folder) / "jobs" / "one"
            # The Job mounts its keep directory; a Job without one fails at once.
            missing = subprocess.run(
                ["sh", "-c", kubernetes_target._KEEP, "sh", "true"],
                env={**os.environ, kubernetes_target.KEEP_ENV: str(keep)},
                check=False,
            )
            self.assertEqual(missing.returncode, 4)
            keep.mkdir(parents=True)
            archive = Path(folder) / "inputs.tar.gz"
            archive.write_bytes(kubernetes_gate.pack({"a.txt": b"kept"}))
            inner = [
                sys.executable,
                "-m",
                "narwhal.deployment.kubernetes_gate",
                "run",
                "--inputs",
                str(archive),
                "--into",
                f"{folder}/in",
                "--return",
                f"a.txt={folder}/in/a.txt",
                "--",
                "sh",
                "-c",
                "echo said; exit 3",
            ]
            result = subprocess.run(
                ["sh", "-c", kubernetes_target._KEEP, "sh", *inner],
                env={**os.environ, kubernetes_target.KEEP_ENV: str(keep)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual((keep / "status").read_text(), "3\n")
            log = (keep / "output.log").read_text()
            self.assertIn("said", log)
            self.assertEqual(kubernetes_target.gate_files(log), {"a.txt": b"kept"})
            self.assertEqual(kubernetes_target.gate_files(result.stdout), {"a.txt": b"kept"})
            self.assertEqual((keep / "files" / "a.txt").read_bytes(), b"kept")

    def test_unsafe_inputs_are_refused(self):
        import io as _io
        import tarfile

        with tempfile.TemporaryDirectory() as folder:
            buffer = _io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                tar.addfile(info, _io.BytesIO(b"x"))
            archive = Path(folder) / "inputs.tar.gz"
            archive.write_bytes(buffer.getvalue())
            with self.assertRaisesRegex(ValueError, "unsafe"):
                kubernetes_gate.unpack(archive, Path(folder) / "in")

    def test_a_pod_that_can_never_run_says_why(self):
        never = kubernetes_target.never_runs
        self.assertIsNone(never({"status": {"phase": "Running"}}, scheduled=True))
        refused = {
            "spec": {"nodeName": "gpu-a"},
            "status": {"phase": "Failed", "reason": "OutOfcpu"},
        }
        self.assertIn("kubelet on gpu-a refused the pod: OutOfcpu", never(refused, scheduled=False))
        waiting = {
            "status": {
                "containerStatuses": [
                    {"name": "gate", "state": {"waiting": {"reason": "CreateContainerConfigError"}}}
                ]
            }
        }
        self.assertIn("CreateContainerConfigError", never(waiting, scheduled=False))
        pulling = {
            "status": {"containerStatuses": [{"state": {"waiting": {"reason": "ErrImagePull"}}}]}
        }
        self.assertIsNone(never(pulling, scheduled=False))
        pending = {
            "status": {
                "conditions": [
                    {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
                ]
            }
        }
        self.assertIsNone(never(pending, scheduled=False))
        self.assertIn("no node can schedule", never(pending, scheduled=True))
        gated = {
            "status": {
                "conditions": [
                    {"type": "PodScheduled", "status": "False", "reason": "SchedulingGated"}
                ]
            }
        }
        self.assertIsNone(never(gated, scheduled=True))


class GraphTests(unittest.TestCase):
    def test_every_stage_is_named_as_its_ssh_counterpart(self):
        from narwhal.deployment.deploy_graph import DEPLOY_GRAPH

        self.assertLessEqual(set(KUBERNETES_GRAPH.stages), set(DEPLOY_GRAPH.stages))

    def test_a_replaced_engine_pod_reruns_its_check_but_not_the_fabric(self):
        reruns = [rerun.stage for rerun in KUBERNETES_GRAPH.reruns("engine_identity")]
        self.assertIn("engine-check", reruns)
        self.assertNotIn("fabric-edge", reruns)
        self.assertNotIn("profile", reruns)

    def test_the_engine_check_follows_the_pod_start(self):
        order = KUBERNETES_GRAPH.order()
        self.assertLess(order.index("engine-start"), order.index("engine-check"))
        self.assertLess(order.index("engine-check"), order.index("attest-capture"))

    def test_the_measuring_engines_handoffs_size_their_groups(self):
        stages = KUBERNETES_GRAPH.stages
        self.assertEqual(stages["transfer-measure"].capability, "transfer_measurement")
        self.assertIn("transfer-measure", stages["cache-groups"].after)

    def test_a_new_secret_reruns_every_engines_launch_and_the_routers(self):
        reruns = [rerun.stage for rerun in KUBERNETES_GRAPH.reruns("credential")]
        self.assertEqual(reruns, ["engine-prepare", "router-start"])


def deletions(rig: Rig) -> list[tuple[str, str, str | None]]:
    return [(d.kind, d.name, d.propagation) for d in rig.store.deletes]


def fleet_store_kind(rig: Rig):
    return rig.store.kind_of(rig.fleet)


def kubernetes_node_kind():
    from tests.deployment.kubernetes_cluster import NODE

    return NODE


if __name__ == "__main__":
    unittest.main()
