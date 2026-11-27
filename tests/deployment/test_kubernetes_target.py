"""`narwhal deploy --target kubernetes` over the whole stage graph, against a stubbed cluster."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import fabric, kubernetes_gate, kubernetes_target
from narwhal.deployment.hosts import Host
from narwhal.deployment.kubernetes_graph import KUBERNETES_GRAPH
from narwhal.dev import cli
from tests.deployment.kubernetes_cluster import NODES, SECRET, Rig, emitted, unpack

ENGINES = tuple(NODES)
EDGES = ("gpu-a-to-gpu-b", "gpu-b-to-gpu-a")
# The gate Jobs one run starts, in order: two engines on two nodes, one cache group.
JOBS = [
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


class KubernetesDeployTests(unittest.TestCase):
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

    def test_run_reaches_the_router_pair_and_a_rerun_starts_no_job(self):
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["data"]["ran"]), every_stage())
        self.assertEqual(self.rig.kinds, JOBS)
        record = json.loads((self.run / "run.json").read_text())
        self.assertEqual(record["target"], "kubernetes")
        # Each discovery Job runs the engine image on its node, with the node's GPUs and model.
        discovery = [
            job["entry"] for job in self.rig.jobs.values() if job["entry"]["kind"] == "discovery"
        ]
        self.assertEqual({entry["node"] for entry in discovery}, set(NODES.values()))
        for entry in discovery:
            self.assertEqual(entry["image"], self.rig.env["NARWHAL_ENGINE_IMAGE"])
            self.assertEqual(entry["resources"], {"gpu.example.com/gpu": "2"})
            self.assertEqual(
                entry["modelVolume"], {"persistentVolumeClaim": {"claimName": "model"}}
            )
        # The routers wait for router-start, and every later upgrade keeps them.
        enabled = [release["routers"]["enabled"] for release in self.rig.releases]
        first = enabled.index(True)
        self.assertFalse(any(enabled[:first]))
        self.assertTrue(all(enabled[first:]))
        self.assertEqual(self.rig.releases[first]["gates"], [])
        final = self.rig.releases[-1]
        self.assertEqual(final["state"]["claimName"], "narwhal-state")
        fleet = final["routers"]["fleet"]
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
        [entry] = [
            job["entry"]
            for job in self.rig.jobs.values()
            if job["entry"]["kind"] == "engine-check" and "engine-1" in job["entry"]["name"]
        ]
        self.assertIn(f"/narwhal-state/engines/engine-1/launches/{uid}", entry["command"][-1])
        verify = [
            job["entry"]
            for job in self.rig.jobs.values()
            if job["entry"]["kind"] == "sidecar-verify"
        ]
        self.assertTrue(all(entry["serviceAccount"] for entry in verify))
        # A Job's inputs hold no secret: the key comes from the Job's Secret reference.
        for name, job in self.rig.jobs.items():
            if job["entry"]["kind"] != "prepare":
                continue
            files = unpack(self.rig.inputs[name])
            self.assertNotIn(SECRET.encode(), files[".env"])
            self.assertIn(b'NARWHAL_ENGINE_API_KEY="${NARWHAL_ENGINE_KEY:?', files[".env"])
            self.assertTrue(any(path.startswith("config/engine-launch.") for path in files))
        # Every Job keeps its output in its attempt's directory on the state volume.
        for job in self.rig.jobs.values():
            entry = job["entry"]
            self.assertEqual(entry["command"][:2], ["sh", "-c"])
            keep = entry["env"][kubernetes_target.KEEP_ENV]
            self.assertRegex(
                keep, rf"^/narwhal-state/deploy/{record['run_id']}/stages/.+/jobs/{entry['name']}$"
            )
        # The inputs ConfigMaps go once their Job finished.
        self.assertEqual(self.rig.config_maps, {})

        jobs = len(self.rig.jobs)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["ran"], [])
        self.assertEqual(len(self.rig.jobs), jobs)

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
            job["entry"]["command"]
            for job in list(self.rig.jobs.values())[before:]
            if job["entry"]["kind"] == "router-check"
        ]
        actions = [command[command.index("narwhal.deployment.host_http") + 1] for command in checks]
        self.assertEqual(actions, ["readmit", "responses"])
        self.assertEqual(checks[0][-1], "n2")

    def test_each_engine_pod_is_pinned_to_the_node_discovery_measured(self):
        self.rig.misplace = {"engine-2": "gpu-c"}
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        install = next(release for release in self.rig.releases if release["engines"])
        self.assertEqual(install["placement"], NODES)
        for role, node in NODES.items():
            pod = self.rig.pods[role]
            self.assertEqual(pod["spec"]["nodeName"], node)
            self.assertEqual(pod["spec"]["schedulingGates"], [])
            terms = pod["spec"]["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"]
            self.assertEqual(terms[0]["matchFields"][0]["values"], [node])
        # The pod the scheduler put on another node was replaced, then pinned.
        self.assertEqual(self.rig.calls["delete pod"], 1)

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
        jobs = [job["entry"] for job in self.rig.jobs.values()]
        measured = [entry for entry in jobs if entry["kind"] == "transfer-measure"]
        self.assertEqual({entry["node"] for entry in measured}, set(NODES.values()))
        for entry in measured:
            self.assertTrue(entry["hostNetwork"])
            self.assertIn("cd /tmp/narwhal-in", entry["command"][-1])
            files = unpack(self.rig.inputs[entry["name"]])
            role = next(
                role for role in NODES if role in entry["name"] or NODES[role] in entry["name"]
            )
            self.assertIn(f"runs/deployment/.env.{role}", files)
            self.assertIn(f"config/engine-launch.{role}.json", files)
        [profile] = [entry for entry in jobs if entry["kind"] == "profile"]
        self.assertIn("--no-router", profile["command"][-1])
        record = json.loads((self.run / "run.json").read_text())
        transfer = self.run / record["stages"]["transfer-measure[engine-1]"]["directory"]
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
        deployment = kubernetes_target.KubernetesDeployment(self.run, self.rig.env)
        context = kubernetes_target._LiveContext(self.run)
        shell = kubernetes_target.JobShell(deployment, context)  # type: ignore[arg-type]
        before = len(self.rig.jobs)
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
        client_release = next(
            r for r in self.rig.releases if any(g["kind"] == "fabric-client" for g in r["gates"])
        )
        self.assertEqual(
            [g["kind"] for g in client_release["gates"]], ["fabric-server", "fabric-client"]
        )

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

    def test_plan_starts_no_job_and_takes_each_link_as_recorded(self):
        code, result = self.deploy("plan")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.rig.releases, [])
        self.assertTrue(all(row["runs"] for row in result["data"]["stages"]))
        self.assertEqual(self.deploy("run")[0], 0)
        releases = len(self.rig.releases)
        self.rig.routes["gpu-a-to-gpu-b"] = "route-2"
        code, result = self.deploy("plan")
        self.assertEqual(code, 0, result)
        self.assertFalse(any(row["runs"] for row in result["data"]["stages"]))
        self.assertEqual(len(self.rig.releases), releases)

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
        self.rig.fail_kind = None
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertNotIn("discover", result["data"]["ran"])
        self.assertIn("engine-check[engine-1]", result["data"]["ran"])

    def test_a_truncated_job_log_fails_its_stage(self):
        self.rig.truncate_kind = "prepare"
        code, result = self.deploy("run")
        self.assertEqual(code, 4, result)
        self.assertIn("truncated", result["errors"][0]["message"])

    def test_status_reports_a_replaced_pod_and_down_removes_the_workloads(self):
        self.assertEqual(self.deploy("run")[0], 0)
        code, result = self.deploy("status")
        self.assertEqual(code, 0, result)
        self.rig.pods.pop("router-1")
        code, result = self.deploy("status")
        self.assertEqual(code, 3, result)
        states = {check["key"]: check["state"] for check in result["data"]["live"]}
        self.assertNotEqual(states["router_identity"], "holds")
        code, result = self.deploy("down")
        self.assertEqual(code, 0, result)
        stopped = {process["name"]: process["stopped"] for process in result["data"]["processes"]}
        self.assertEqual(stopped["router-0"], True)
        self.assertEqual(stopped["router-1"], False)
        self.assertTrue(stopped["engine-1"])
        final = self.rig.releases[-1]
        self.assertEqual(
            (final["engines"], final["gates"], final["routers"]["enabled"]), ({}, [], False)
        )
        self.assertEqual(final["state"]["claimName"], "narwhal-state")
        self.assertEqual(sorted(self.rig.pods), [])

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
        self.assertEqual(self.rig.releases, [])


class GateTests(unittest.TestCase):
    def test_a_gate_returns_its_files_and_the_runner_checks_them(self):
        log = "noise\n" + emitted({"a.json": b"{}", "dir/b": b"x"})
        self.assertEqual(kubernetes_target.gate_files(log), {"a.json": b"{}", "dir/b": b"x"})
        with self.assertRaisesRegex(ValueError, "truncated"):
            kubernetes_target.gate_files(log.rsplit("narwhal-manifest", 1)[0])
        dropped = "\n".join(line for line in log.splitlines() if " dir/b " not in line)
        with self.assertRaisesRegex(ValueError, "missing"):
            kubernetes_gate_files(dropped)

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


def kubernetes_gate_files(log: str) -> dict:
    return kubernetes_target.gate_files(log)


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


if __name__ == "__main__":
    unittest.main()
