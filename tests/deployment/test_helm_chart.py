"""The Helm chart: its engine StatefulSet runs the pod each plan rendered, and its values and
mounts hold a fleet to the rules of the Kubernetes mapping.

The tests that render the chart need helm: HELM naming it, `helm` on PATH, or the one
`make chart-tools` installs. They skip without it.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from narwhal.backends import load as load_backend
from narwhal.deployment import direct_engine_set
from narwhal.deployment.direct_start import DirectEngine
from narwhal.deployment.launch_engine import engine_set
from narwhal.deployment.launch_engine.kubernetes import MANIFESTS
from tests.deployment import chart_fixtures
from tests.deployment.pod_fixtures import NODE, POD_IP, POD_UID, SECRET_VALUE, expand
from tools.observability.artifacts import DEFAULT_CONSOLE_URL, stage_artifacts
from tools.observability.chart_dashboard import chart_dashboard
from tools.observability.make_targets import load_contract

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "narwhal"
# `make chart-tools` installs the pinned helm in local/bin.
LOCAL_HELM = ROOT / "local" / "bin" / "helm"
HELM = (
    os.environ.get("HELM")
    or shutil.which("helm")
    or (str(LOCAL_HELM) if LOCAL_HELM.is_file() else None)
)
# What the engine image's own environment holds before the pod's variables.
IMAGE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root"}
STEPS = ("checkpoint", "runtime-check", "engine")
OBSERVABILITY = ROOT / "tools" / "observability"
SERVICES = f"{chart_fixtures.NAMESPACE}.svc.cluster.local"
ROUTERS = [f"router-{n}.routers.{SERVICES}:8000" for n in (0, 1)]
LOAD_BALANCER = f"haproxy.{SERVICES}:8000"
SIDECAR = "attestation"


def field(path: str, role: str) -> str:
    return {
        "status.podIP": POD_IP,
        "metadata.uid": POD_UID,
        "spec.nodeName": NODE,
        "metadata.name": role,
    }[path]


def defined(container: dict, role: str) -> dict[str, str]:
    """A container's variables as the kubelet sets them, in their order."""
    values: dict[str, str] = {}
    for item in container.get("env", []):
        if "valueFrom" in item:
            source = item["valueFrom"]
            value = (
                field(source["fieldRef"]["fieldPath"], role)
                if "fieldRef" in source
                else SECRET_VALUE
            )
        else:
            value = expand(item["value"], values)
        values[item["name"]] = value
    return values


def started(container: dict, role: str, specs: Path | None) -> tuple[list[str], dict]:
    """The argv and environment of the process a container ends up running.

    A step container of the chart runs the launcher, which replaces itself with its role's
    command; `specs` holds the role's spec files there.
    """
    variables = defined(container, role)
    argv = [expand(part, variables) for part in container["command"]]
    environment = {**IMAGE_ENV, **variables}
    if argv[4:5] == [engine_set.LAUNCHER]:
        assert specs is not None
        launched: list[tuple[list[str], dict]] = []
        namespace: dict = {}
        exec(engine_set.LAUNCHER_DEFINITIONS, namespace)
        namespace["launch"](
            str(specs),
            json.loads(argv[6]),
            argv[7],
            dict(environment),
            lambda path, args, env: launched.append((args, env)),
        )
        [(argv, environment)] = launched
    return argv, environment


def mounts(spec: dict, container: dict, maps: dict[str, dict], role: str) -> dict:
    """Each mount path's source, subpath and access, with the role's ConfigMap files resolved."""
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    found = {}
    for mount in container["volumeMounts"]:
        if mount["name"] == engine_set.SPEC_VOLUME:
            continue
        volume = {k: v for k, v in volumes[mount["name"]].items() if k != "name"}
        subpath = expand(mount.get("subPathExpr", ""), {engine_set.ROLE_ENV: role})
        if "configMap" in volume:
            source: object = maps[volume["configMap"]["name"]]["data"]
        elif "projected" in volume and all(
            "configMap" in item for item in volume["projected"]["sources"]
        ):
            files = {}
            for item in volume["projected"]["sources"]:
                data = maps[item["configMap"]["name"]]["data"]
                for entry in item["configMap"]["items"]:
                    directory, _, name = entry["path"].partition("/")
                    if directory == subpath:
                        files[name] = data[entry["key"]]
            source, subpath = files, ""
        else:
            source = volume
        found[mount["mountPath"]] = (source, subpath, bool(mount.get("readOnly")))
    return found


def containers(spec: dict) -> dict[str, dict]:
    return {c["name"]: c for c in [*spec["initContainers"], *spec["containers"]]}


class Comparison:
    """Compare the pod a plan rendered with the chart's pod for the same role."""

    def __init__(self, case: unittest.TestCase, folder: Path) -> None:
        self.case = case
        self.folder = folder

    def specs(self, maps: dict[str, dict], role: str) -> Path:
        directory = self.folder / f"spec-{role}-{len(list(self.folder.iterdir()))}"
        directory.mkdir()
        [spec] = [m for name, m in maps.items() if name.startswith(f"narwhal-spec-{role}-")]
        for name, text in spec["data"].items():
            (directory / name).write_text(text)
        return directory

    def same(self, run: Path, template: dict, chart_maps: dict[str, dict]) -> None:
        manifests = json.loads((run / MANIFESTS).read_text())
        role = json.loads((run / "launch.json").read_text())["role"]
        rendered = manifests["podSpec"]
        rendered_maps = {item["metadata"]["name"]: item for item in manifests["configMaps"]}
        specs = self.specs(chart_maps, role)
        ignored = {"initContainers", "containers", "volumes"}
        self.case.assertEqual(
            {k: v for k, v in template.items() if k not in ignored},
            {k: v for k, v in rendered.items() if k not in ignored},
        )
        ours, theirs = containers(template), containers(rendered)
        self.case.assertEqual(list(ours), list(theirs))
        for name, container in theirs.items():
            with self.case.subTest(role=role, container=name):
                chart = ours[name]
                argv, environment = started(chart, role, specs)
                expected_argv, expected_environment = started(container, role, None)
                if name == SIDECAR:
                    environment.pop(engine_set.ROLE_ENV)
                self.case.assertEqual(argv, expected_argv)
                self.case.assertEqual(environment, expected_environment)
                self.case.assertEqual(
                    mounts(template, chart, chart_maps, role),
                    mounts(rendered, container, rendered_maps, role),
                )
                skip = {"command", "env", "volumeMounts", "ports"}
                self.case.assertEqual(
                    {k: v for k, v in chart.items() if k not in skip},
                    {k: v for k, v in container.items() if k not in skip},
                )
        names = {volume["name"] for volume in template["volumes"]}
        self.case.assertEqual(
            names - {engine_set.SPEC_VOLUME}, {volume["name"] for volume in rendered["volumes"]}
        )


def filled(engines: dict) -> dict:
    """The generator's pod template with the image, limits and model volume the chart sets."""
    spec = copy.deepcopy(engines["podTemplate"])
    limits = {engines["gpuResource"]: str(engines["gpus"]), engines["rdmaResource"]: "1"}
    for container in [*spec["initContainers"], *spec["containers"]]:
        if container.get("image", "") is None:
            container["image"] = engines["image"]
        if "resources" in container and container["resources"] is None:
            container["resources"] = {"limits": limits}
    for volume in spec["volumes"]:
        if volume["name"] == engine_set.MODEL_VOLUME:
            volume.update(engines["modelVolume"])
    return spec


class EngineSetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def test_each_role_runs_the_pod_its_plan_rendered(self):
        for backend in ("vllm", "sglang", "trtllm"):
            with self.subTest(backend=backend):
                runs, document = chart_fixtures.example(self.root / backend, 3, backend)
                engines = document["engines"]
                self.assertEqual(engines["count"], 3)
                maps = {item["metadata"]["name"]: item for item in engines["configMaps"]}
                for run in runs:
                    Comparison(self, self.scratch).same(run, filled(engines), maps)

    def test_the_launcher_refuses_a_changed_spec_file(self):
        _, document = chart_fixtures.example(self.root, 1)
        engines = document["engines"]
        maps = {item["metadata"]["name"]: item for item in engines["configMaps"]}
        specs = Comparison(self, self.scratch).specs(maps, "engine-1")
        engine = containers(filled(engines))["engine"]
        spec = json.loads((specs / "engine.json").read_text())
        spec["command"].append("--enforce-eager")
        (specs / "engine.json").write_text(json.dumps(spec))
        with self.assertRaisesRegex(SystemExit, "differs from the SHA-256 pinned for engine-1"):
            started(engine, "engine-1", specs)

    def test_plans_must_fit_one_statefulset(self):
        first = chart_fixtures.prepare_role(self.root / "a", 1)
        image = "registry.example/other@sha256:" + "f" * 64
        cases = {
            "engine image": [
                first,
                chart_fixtures.prepare_role(self.root / "b", 2, NARWHAL_ENGINE_IMAGE=image),
            ],
            "engine-1 onwards": [chart_fixtures.prepare_role(self.root / "c", 2)],
            "headless Service": [
                chart_fixtures.prepare_role(
                    self.root / "d",
                    1,
                    NARWHAL_NODE_1_URL="http://192.0.2.11:8000",
                )
            ],
            "beyond its role": [
                first,
                chart_fixtures.prepare_role(
                    self.root / "e", 2, NARWHAL_KUBERNETES_RUNTIME_CLASS="other-runtime"
                ),
            ],
        }
        for message, runs in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                engine_set.chart_values(runs)

    def test_the_chart_follows_the_release_version(self):
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(chart["version"], project["version"])
        self.assertEqual(chart["appVersion"], project["version"])


class Rendering:
    """Render the chart with helm, from values written to the class's scratch directory."""

    root: Path
    documents: list[dict]

    @classmethod
    def helm(cls, values: dict, *, namespace: str = chart_fixtures.NAMESPACE):
        path = cls.root / f"values-{len(list(cls.root.iterdir()))}.json"
        path.write_text(json.dumps(values))
        assert HELM is not None
        return subprocess.run(
            [HELM, "template", "fleet", str(CHART), "--namespace", namespace, "-f", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    @classmethod
    def render(cls, values: dict) -> list[dict]:
        result = cls.helm(values)
        if result.returncode:
            raise AssertionError(result.stderr)
        return [document for document in yaml.safe_load_all(result.stdout) if document]

    def find(self, kind: str, name: str) -> dict:
        [document] = [
            d for d in self.documents if d["kind"] == kind and d["metadata"]["name"] == name
        ]
        return document


@unittest.skipUnless(HELM, "helm is not installed")
class ChartTests(Rendering, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        cls.runs, cls.values = chart_fixtures.example(cls.root / "fleet")
        cls.documents = cls.render(cls.values)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def pods(self) -> list[tuple[str, dict]]:
        return [
            (document["kind"], document["spec"]["template"]["spec"])
            for document in self.documents
            if document["kind"] in ("StatefulSet", "Deployment", "Job")
        ]

    def test_the_engine_statefulset_runs_each_plans_pod(self):
        engines = self.find("StatefulSet", "engine")
        self.assertEqual(engines["spec"]["replicas"], 2)
        self.assertEqual(engines["spec"]["ordinals"], {"start": 1})
        self.assertEqual(engines["spec"]["updateStrategy"], {"type": "OnDelete"})
        self.assertEqual(engines["spec"]["serviceName"], "engines")
        maps = {d["metadata"]["name"]: d for d in self.documents if d["kind"] == "ConfigMap"}
        scratch = self.root / "scratch"
        scratch.mkdir()
        for run in self.runs:
            Comparison(self, scratch).same(run, engines["spec"]["template"]["spec"], maps)
        service = self.find("Service", "engines")
        self.assertEqual(service["spec"]["clusterIP"], "None")
        self.assertTrue(self.find("ServiceAccount", "narwhal-attestation"))

    def test_only_gate_jobs_write_the_state_volume(self):
        claim = self.values["engines"]["stateClaim"]
        own = f"engines/$({engine_set.ROLE_ENV})/$(NARWHAL_POD_UID)"
        kinds = set()
        for kind, spec in self.pods():
            state = {
                volume["name"]
                for volume in spec["volumes"]
                if volume.get("persistentVolumeClaim", {}).get("claimName") == claim
            }
            for container in [*spec.get("initContainers", []), *spec["containers"]]:
                for mount in container.get("volumeMounts", []):
                    if mount["name"] not in state or mount.get("readOnly"):
                        continue
                    kinds.add(kind)
                    with self.subTest(kind=kind, container=container["name"]):
                        if kind == "StatefulSet" and container["name"] != "router":
                            self.assertEqual(mount["subPathExpr"], own)
                        else:
                            self.assertEqual(kind, "Job")
        self.assertEqual(kinds, {"StatefulSet", "Job"})
        router = self.find("StatefulSet", "router")["spec"]["template"]["spec"]
        [volume] = [v for v in router["volumes"] if v["name"] == "narwhal-state"]
        self.assertTrue(volume["persistentVolumeClaim"]["readOnly"])

    def test_haproxy_finds_each_router_pod_by_its_dns_name(self):
        config = self.find("ConfigMap", "haproxy")["data"]["haproxy.cfg"]
        shipped = (ROOT / "deploy" / "ha" / "haproxy.cfg").read_text().splitlines()
        servers = re.findall(r"(?m)^\s*server (router-[ab]) (\S+) (.*)$", config)
        domain = "routers.example.svc.cluster.local"
        self.assertEqual(
            servers,
            [
                ("router-a", f"router-0.{domain}:8000", "resolvers kubernetes init-addr none"),
                ("router-b", f"router-1.{domain}:8000", "resolvers kubernetes init-addr none"),
            ],
        )
        lines = config.splitlines()
        kept = [line for line in shipped if line.strip() and "_ADDRESS:8000" not in line]
        self.assertEqual([line for line in lines if line in kept], kept)
        self.assertIn("resolvers kubernetes", lines)
        self.assertIn("    parse-resolv-conf", lines)

    def test_router_0_starts_primary_until_it_holds_a_handoff(self):
        router = self.find("StatefulSet", "router")
        self.assertEqual(router["spec"]["replicas"], 2)
        spec = router["spec"]["template"]["spec"]
        [container] = spec["containers"]
        self.assertEqual(container["workingDir"], "/narwhal-router")
        variables = defined(container, "router-0")
        variables["NARWHAL_POD_IP"] = POD_IP
        argv = [expand(part, variables) for part in container["command"]]
        self.assertEqual(argv[argv.index("--fleet") + 1], "/narwhal-state/fleet.json")
        folder = Path(tempfile.mkdtemp(dir=self.root))
        handoff = folder / "router-state.json"
        fleet = folder / "fleet.json"
        fleet.write_text(json.dumps({"recovery": {"state_path": str(handoff)}}))
        argv[argv.index("--fleet") + 1] = str(fleet)
        lease = folder / "router.lease"
        argv[argv.index("--lease-path") + 1] = str(lease)
        domain = "routers.example.svc.cluster.local:8000"
        first, second = f"http://router-1.{domain}", f"http://router-0.{domain}"
        # Each case: the router, the files that exist, and the standby options it starts with.
        cases = (
            ("router-0", (), []),
            ("router-1", (), ["--standby-of", second]),
            ("router-0", (lease,), ["--standby-of", first]),
            ("router-0", (handoff,), ["--standby-of", first]),
            ("router-1", (handoff, lease), ["--standby-of", second]),
        )
        for name, present, standby in cases:
            with self.subTest(name=name, present=[path.name for path in present]):
                for path in (handoff, lease):
                    path.unlink(missing_ok=True)
                for path in present:
                    path.write_text("{}")
                executed: list[list[str]] = []
                environment = {**variables, "NARWHAL_POD_NAME": name}
                with (
                    patch.dict(os.environ, environment),
                    patch.object(sys, "argv", ["-c", *argv[4:]]),
                    patch.object(sys, "executable", "python3"),
                    patch("os.execv", lambda path, args, found=executed: found.append(args)),
                ):
                    exec(argv[3], {"__name__": "__main__"})
                [args] = executed
                self.assertEqual(args[:5], ["python3", "-I", "-m", "narwhal.cli", "--fleet"])
                tail = ["--router-id", name, *standby]
                self.assertEqual(args[-len(tail) :], tail)
                self.assertEqual(args.count("--standby-of"), len(standby) // 2)
                self.assertIn("--lease-path", args)
                self.assertEqual(args[args.index("--host") + 1], POD_IP)

    def test_gate_jobs_run_once_on_their_node(self):
        job = self.find("Job", "fabric-node-a-node-b-server")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 300)
        spec = job["spec"]["template"]["spec"]
        self.assertEqual(spec["nodeName"], "node-b")
        self.assertTrue(spec["hostNetwork"])
        [container] = spec["containers"]
        self.assertEqual(container["image"], chart_fixtures.FABRIC_IMAGE)
        self.assertEqual(container["resources"]["limits"], {"rdma.example.com/hca": "1"})

    def test_the_runners_gates_get_their_inputs_node_and_identity(self):
        values = copy.deepcopy(self.values)
        values["gates"] = [
            {
                "name": "prepare-engine-1",
                "keep": "deploy/run-1/jobs/prepare-engine-1",
                "kind": "prepare",
                "command": ["narwhal-engine", "prepare"],
                "deadlineSeconds": 600,
                "node": "node-a",
                "inputs": "prepare-engine-1",
                "modelVolume": {"persistentVolumeClaim": {"claimName": "models"}},
            },
            {
                "name": "probe-node-a",
                "keep": "deploy/run-1/jobs/probe-node-a",
                "kind": "fabric-probe",
                "command": ["ip", "route"],
                "deadlineSeconds": 60,
                "node": "node-a",
            },
            {
                "name": "verify-engine-1",
                "keep": "deploy/run-1/jobs/verify-engine-1",
                "kind": "sidecar-verify",
                "command": ["python", "-m", "narwhal.deployment.attestation_contract.cli"],
                "deadlineSeconds": 300,
                "serviceAccount": True,
            },
        ]
        jobs = {
            d["metadata"]["name"]: d["spec"]["template"]["spec"]
            for d in self.render(values)
            if d["kind"] == "Job"
        }
        prepare = jobs["prepare-engine-1"]
        mounts = {m["mountPath"]: m for m in prepare["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["/narwhal-input"]["readOnly"])
        self.assertTrue(mounts["/model"]["readOnly"])
        volumes = {v["name"]: v for v in prepare["volumes"]}
        self.assertEqual(volumes["narwhal-input"]["configMap"]["name"], "prepare-engine-1")
        self.assertFalse(prepare["automountServiceAccountToken"])
        probe = jobs["probe-node-a"]
        self.assertTrue(probe["hostNetwork"])
        self.assertNotIn("resources", probe["containers"][0])
        self.assertEqual(
            probe["containers"][0]["securityContext"],
            {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        )
        verify = jobs["verify-engine-1"]
        self.assertEqual(verify["serviceAccountName"], "narwhal-attestation")
        self.assertTrue(verify["automountServiceAccountToken"])

    def test_only_the_gates_that_call_engines_get_the_key_and_fabric_jobs_are_confined(self):
        values = copy.deepcopy(self.values)
        values["gates"].append({**values["gates"][2], "name": "prepare", "kind": "prepare"})
        jobs = {
            d["metadata"]["name"]: d["spec"]["template"]["spec"]
            for d in self.render(values)
            if d["kind"] == "Job"
        }

        def env(job: str) -> dict:
            return {e["name"]: e for e in jobs[job]["containers"][0]["env"]}

        for job in ("preflight", "prepare"):
            self.assertEqual(
                env(job)["NARWHAL_ENGINE_API_KEY"]["valueFrom"]["secretKeyRef"],
                {"name": "engine-credential", "key": "api-key"},
            )
        for job in ("discover-node-a", "fabric-node-a-node-b-server"):
            self.assertNotIn("NARWHAL_ENGINE_API_KEY", env(job))
            self.assertEqual(env(job)["NARWHAL_GATE_KEEP"]["value"], "/narwhal-keep")
        server = jobs["fabric-node-a-node-b-server"]["containers"][0]
        # The fabric image writes only its own keep directory and sees no other state.
        self.assertEqual(
            server["volumeMounts"],
            [
                {
                    "name": "narwhal-state",
                    "mountPath": "/narwhal-keep",
                    "subPath": values["gates"][1]["keep"],
                }
            ],
        )
        self.assertEqual(
            server["securityContext"],
            {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"], "add": ["IPC_LOCK"]},
            },
        )
        preflight = {m["mountPath"]: m for m in jobs["preflight"]["containers"][0]["volumeMounts"]}
        self.assertEqual(set(preflight), {"/narwhal-keep", "/narwhal-state"})
        self.assertNotIn("securityContext", jobs["preflight"]["containers"][0])
        for keep in ("deploy/../fleet.json", "fleet.json", "deploy/run/.hidden"):
            values["gates"][1]["keep"] = keep
            self.assertIn("keep", self.helm(values).stderr, keep)
        del values["gates"][1]["keep"]
        self.assertIn("keep", self.helm(values).stderr)

    def test_placement_holds_each_engine_pod_until_the_run_pins_it(self):
        values = copy.deepcopy(self.values)
        values["placement"] = {"engine-1": "node-a", "engine-2": "node-b"}
        [engines] = [
            d
            for d in self.render(values)
            if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "engine"
        ]
        self.assertEqual(
            engines["spec"]["template"]["spec"]["schedulingGates"],
            [{"name": "narwhal.io/placement"}],
        )
        default = self.find("StatefulSet", "engine")["spec"]["template"]["spec"]
        self.assertNotIn("schedulingGates", default)
        values["placement"] = {"engine-1": "node-a"}
        result = self.helm(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("placement names no node for engine-2", result.stderr)

    def test_a_release_without_engines_holds_its_volumes_and_gates(self):
        values = copy.deepcopy(self.values)
        values["engines"] = {}
        values["routers"]["enabled"] = False
        values["state"]["claimName"] = "narwhal-state"
        values["gates"] = [values["gates"][0], values["gates"][2]]
        values["gates"][0]["modelVolume"] = {"persistentVolumeClaim": {"claimName": "models"}}
        documents = self.render(values)
        kinds = sorted({d["kind"] for d in documents})
        self.assertEqual(kinds, ["Job", "PersistentVolumeClaim"])
        claims = {d["metadata"]["name"] for d in documents if d["kind"] == "PersistentVolumeClaim"}
        self.assertEqual(claims, {"narwhal-state", "narwhal-lease"})
        values["routers"]["enabled"] = True
        self.assertNotEqual(self.helm(values).returncode, 0)
        values["routers"]["enabled"] = False
        del values["state"]["claimName"]
        result = self.helm(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state.claimName", result.stderr)

    def test_the_state_claim_must_be_the_one_the_plans_name(self):
        values = copy.deepcopy(self.values)
        values["state"]["claimName"] = "another-claim"
        result = self.helm(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NARWHAL_KUBERNETES_STATE_CLAIM", result.stderr)

    def test_incomplete_values_are_refused(self):
        def without(path: str) -> dict:
            values = copy.deepcopy(self.values)
            *parents, last = path.split(".")
            target = values
            for part in parents:
                target = target[part]
            del target[last]
            return values

        cases = {
            "engines": without("engines"),
            "gpuResource": without("engines.gpuResource"),
            "rdmaResource": without("engines.rdmaResource"),
            "modelVolume": without("engines.modelVolume"),
            "count": without("engines.count"),
            "image": without("engines.image"),
            "narwhal": without("images.narwhal"),
            "size": without("state.size"),
            "fleet": without("routers.fleet"),
        }
        tag = {**self.values, "images": {**self.values["images"], "haproxy": "haproxy:3.2"}}
        cases["haproxy"] = tag
        nodeless = copy.deepcopy(self.values)
        del nodeless["gates"][0]["node"]
        cases["node"] = nodeless
        for message, values in cases.items():
            with self.subTest(message):
                result = self.helm(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        defaults = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertNotEqual(self.helm(defaults).returncode, 0)

    def test_the_fleet_must_fit_its_release(self):
        result = self.helm(self.values, namespace="other")
        self.assertIn("install the release there", result.stderr)
        other = {
            **self.values,
            "images": {**self.values["images"], "narwhal": "x@sha256:" + "d" * 64},
        }
        self.assertIn("another Narwhal image", self.helm(other).stderr)
        fabric = {**self.values, "images": {**self.values["images"], "fabric": ""}}
        self.assertIn("needs images.fabric", self.helm(fabric).stderr)
        credentials = copy.deepcopy(self.values)
        del credentials["credentials"]
        self.assertIn("set credentials.engineApiKey", self.helm(credentials).stderr)
        credentials["credentials"] = {
            "engineApiKey": {"env": "KEY", "secretName": "other", "key": "api-key"}
        }
        self.assertIn("set credentials.engineApiKey", self.helm(credentials).stderr)

    def mounted(self, deployment: str, volume: str) -> dict[str, str]:
        """The files a Deployment's ConfigMap volume holds, by their path in the volume."""
        spec = self.find("Deployment", deployment)["spec"]["template"]["spec"]
        [source] = [v["configMap"] for v in spec["volumes"] if v["name"] == volume]
        data = self.find("ConfigMap", source["name"])["data"]
        items = source.get("items") or [{"key": key, "path": key} for key in data]
        return {item["path"]: data[item["key"]] for item in items}

    def test_monitoring_reads_the_files_make_observe_stages(self):
        staged = self.root / "observe"
        contract = load_contract(
            [f"http://{router}" for router in ROUTERS], f"http://{LOAD_BALANCER}"
        )
        stage_artifacts(contract, DEFAULT_CONSOLE_URL, root=staged)
        prometheus = staged / "prometheus"
        expected = {
            str(path.relative_to(prometheus)): path.read_text()
            for path in prometheus.rglob("*")
            if path.is_file()
        }
        self.assertEqual(self.mounted("prometheus", "config"), expected)
        provisioning = staged / "grafana-provisioning"
        expected = {
            str(path.relative_to(provisioning)): path.read_text()
            for path in provisioning.rglob("*")
            if path.is_file()
        }
        self.assertEqual(self.mounted("grafana", "provisioning"), expected)
        # make observe adds the fleet control link and markers that the chart's dashboard takes
        # with --console-url.
        dashboard = json.loads((staged / "grafana-dashboards" / "narwhal.json").read_text())
        self.assertEqual(json.loads(chart_dashboard(DEFAULT_CONSOLE_URL)), dashboard)
        [served] = self.mounted("grafana", "dashboards").values()
        self.assertEqual(served, self.values["monitoring"]["grafana"]["dashboard"])

    def test_prometheus_scrapes_both_routers_and_each_engine_once(self):
        files = self.mounted("prometheus", "config")
        config = yaml.safe_load(files["prometheus.yml"])
        jobs = {job["job_name"]: job for job in config["scrape_configs"]}
        [routers] = jobs["narwhal-router"]["file_sd_configs"]
        [path] = routers["files"]
        self.assertEqual(
            json.loads(files[path.removeprefix("/etc/prometheus/")]), [{"targets": ROUTERS}]
        )
        # The active router serves the engine targets through the load balancer, so a pair
        # lists each engine once.
        [engines] = jobs["engines"]["http_sd_configs"]
        self.assertEqual(engines["url"], f"http://{LOAD_BALANCER}/narwhal/targets")
        [rules] = config["rule_files"]
        alerts = (OBSERVABILITY / "prometheus-alerts.yml").read_text()
        self.assertEqual(files[rules.removeprefix("/etc/prometheus/")], alerts)

    def test_monitoring_runs_as_make_observe_configures_it(self):
        compose = yaml.safe_load((OBSERVABILITY / "compose.yml").read_text())["services"]
        [prometheus] = self.find("Deployment", "prometheus")["spec"]["template"]["spec"][
            "containers"
        ]
        listen = "--web.listen-address="
        self.assertEqual(
            [flag for flag in prometheus["args"] if not flag.startswith(listen)],
            [flag for flag in compose["prometheus"]["command"] if not flag.startswith(listen)],
        )
        [grafana] = self.find("Deployment", "grafana")["spec"]["template"]["spec"]["containers"]
        env = {item["name"]: item.get("value") for item in grafana["env"]}
        for name in (
            "GF_AUTH_ANONYMOUS_ENABLED",
            "GF_AUTH_ANONYMOUS_ORG_ROLE",
            "GF_SECURITY_ALLOW_EMBEDDING",
        ):
            self.assertEqual(env[name], compose["grafana"]["environment"][name], name)
        self.assertEqual(env["NARWHAL_PROMETHEUS_URL"], f"http://prometheus.{SERVICES}:9090")
        admin = {
            item["name"]: item["valueFrom"]["secretKeyRef"]
            for item in grafana["env"]
            if "valueFrom" in item
        }
        self.assertEqual(
            admin,
            {
                "GF_SECURITY_ADMIN_USER": {"name": "grafana-admin", "key": "user"},
                "GF_SECURITY_ADMIN_PASSWORD": {"name": "grafana-admin", "key": "password"},
            },
        )

    def test_monitoring_values(self):
        disabled = copy.deepcopy(self.values)
        disabled["monitoring"] = {"enabled": False}
        kinds = {(d["kind"], d["metadata"]["name"]) for d in self.render(disabled)}
        self.assertNotIn(("Deployment", "prometheus"), kinds)
        self.assertNotIn(("Deployment", "grafana"), kinds)
        claims = {
            d["metadata"]["name"] for d in self.documents if d["kind"] == "PersistentVolumeClaim"
        }
        self.assertIn("prometheus", claims)
        self.assertNotIn("grafana", claims)
        source = (OBSERVABILITY / "grafana-narwhal.json").read_text()
        cases = {
            "image": ("prometheus", "image", None),
            "dashboard": ("grafana", "dashboard", None),
            "admin": ("grafana", "admin", None),
            "unrendered placeholders": ("grafana", "dashboard", source),
            "is not JSON": ("grafana", "dashboard", "{"),
            "@sha256": ("grafana", "image", "grafana/grafana:13.2.1"),
        }
        for message, (part, field, value) in cases.items():
            with self.subTest(message):
                values = copy.deepcopy(self.values)
                if value is None:
                    del values["monitoring"][part][field]
                else:
                    values["monitoring"][part][field] = value
                result = self.helm(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_gate_keys_and_the_fleet_path_must_be_names(self):
        for field, key in (("env", "A: b"), ("resources", "cpu x")):
            with self.subTest(field):
                values = copy.deepcopy(self.values)
                values["gates"][2][field] = {key: "1"}
                result = self.helm(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"invalid propertyName '{key}'", result.stderr)
        for fleet in ("../fleet.json", "/fleet.json", "a/../../fleet.json", "fleet.json\nx"):
            with self.subTest(fleet):
                values = copy.deepcopy(self.values)
                values["routers"]["fleet"] = fleet
                self.assertNotEqual(self.helm(values).returncode, 0)


class DirectEngineSetTests(unittest.TestCase):
    def test_the_command_is_the_backends_direct_start_in_the_pod(self):
        engines = chart_fixtures.direct_engines(
            resources={
                "requests": {"cpu": "3", "memory": "1Gi"},
                "limits": {"cpu": "3", "memory": "1Gi"},
            }
        )
        expected = (
            load_backend("llamacpp")
            .direct_start()
            .command(
                Path("/app/llama-server"),
                DirectEngine(
                    model=Path(direct_engine_set.MODEL_MOUNT) / "model.gguf",
                    served_name="example-model",
                    host="$(NARWHAL_POD_IP)",
                    port=direct_engine_set.PORT,
                    runtime=chart_fixtures.LLAMACPP_RUNTIME,
                    shared_dir=Path(direct_engine_set.SHARED_MOUNT),
                    accelerated=False,
                    threads=3,
                ),
            )
        )
        self.assertEqual(engines["command"], expected)
        # The library path is the image's own, never the caller's.
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/workstation/lib"}):
            again = chart_fixtures.direct_engines()
        self.assertEqual(again["env"], [{"name": "LD_LIBRARY_PATH", "value": "/app"}])

    def test_values_the_kubelet_would_expand_are_escaped(self):
        engines = chart_fixtures.direct_engines(served_name="price-$(HOME)")
        self.assertIn("price-$$(HOME)", engines["command"])
        self.assertIn("$(NARWHAL_POD_IP)", engines["command"])

    def test_direct_values_refuse_what_cannot_serve(self):
        for overrides, message in (
            ({"count": 1}, "at least two engines"),
            ({"backend": "vllm"}, "no direct engine start"),
            (
                {"resources": {"requests": {"cpu": "500m"}, "limits": {"cpu": "500m"}}},
                "whole CPUs",
            ),
        ):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                chart_fixtures.direct_engines(**overrides)

    def test_the_fleet_names_each_pod_by_its_service(self):
        engines = chart_fixtures.direct_engines(count=4)
        fleet = direct_engine_set.fleet_engines(engines)
        domain = f"engines.{chart_fixtures.NAMESPACE}.svc.cluster.local"
        self.assertEqual(
            [engine["url"] for engine in fleet],
            [f"http://engine-{n}.{domain}:{direct_engine_set.PORT}" for n in range(1, 5)],
        )
        self.assertEqual([e["role"] for e in fleet], ["prefill", "prefill", "decode", "decode"])


@unittest.skipUnless(HELM, "helm is not installed")
class DirectChartTests(Rendering, unittest.TestCase):
    """The chart with engines that start directly from their image."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        cls.values = chart_fixtures.direct_example()
        cls.documents = cls.render(cls.values)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_each_engine_pod_runs_the_rendered_command_on_its_own_ip(self):
        engines = self.find("StatefulSet", "engine")
        self.assertEqual(engines["spec"]["replicas"], 2)
        self.assertEqual(engines["spec"]["ordinals"], {"start": 1})
        self.assertEqual(engines["spec"]["updateStrategy"], {"type": "OnDelete"})
        spec = engines["spec"]["template"]["spec"]
        [container] = spec["containers"]
        direct = self.values["directEngines"]
        self.assertEqual(container["command"], direct["command"])
        self.assertEqual(container["image"], direct["image"])
        self.assertEqual(container["resources"], direct["resources"])
        self.assertEqual(
            container["env"][0],
            {"name": "NARWHAL_POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
        )
        self.assertNotIn("hostNetwork", spec)
        self.assertNotIn("shareProcessNamespace", spec)
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        self.assertEqual(mounts["model"]["mountPath"], direct["modelMount"])
        self.assertTrue(mounts["model"]["readOnly"])
        self.assertEqual(mounts["shared"]["mountPath"], direct["sharedMount"])
        volumes = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual(
            volumes["shared"]["persistentVolumeClaim"]["claimName"], "narwhal-engine-shared"
        )
        claim = self.find("PersistentVolumeClaim", "narwhal-engine-shared")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteMany"])
        service = self.find("Service", "engines")
        self.assertEqual(service["spec"]["ports"][0]["port"], direct["port"])
        self.assertEqual(
            service["spec"]["selector"], engines["spec"]["template"]["metadata"]["labels"]
        )

    def test_nothing_writes_the_state_volume_and_no_engine_attests(self):
        kinds = {d["kind"] for d in self.documents}
        self.assertNotIn("Job", kinds)
        self.assertNotIn("ServiceAccount", kinds)
        for document in self.documents:
            if document["kind"] not in ("StatefulSet", "Deployment"):
                continue
            spec = document["spec"]["template"]["spec"]
            for volume in spec.get("volumes", []):
                source = volume.get("persistentVolumeClaim", {})
                if source.get("claimName") == self.values["directEngines"]["stateClaim"]:
                    self.assertTrue(source.get("readOnly"), document["metadata"]["name"])
        engine = self.find("StatefulSet", "engine")["spec"]["template"]["spec"]
        claims = {v.get("persistentVolumeClaim", {}).get("claimName") for v in engine["volumes"]}
        self.assertNotIn(self.values["directEngines"]["stateClaim"], claims)

    def test_direct_values_are_refused_where_they_cannot_make_one_fleet(self):
        attested = chart_fixtures.values({})
        both = copy.deepcopy(self.values)
        both["engines"] = {"count": 2}
        gates = copy.deepcopy(self.values)
        gates["gates"] = attested["gates"]
        incomplete = copy.deepcopy(self.values)
        del incomplete["directEngines"]["command"]
        tagged = copy.deepcopy(self.values)
        tagged["directEngines"]["image"] = "llama.cpp:server"
        single = copy.deepcopy(self.values)
        single["directEngines"]["count"] = 1
        crossed = copy.deepcopy(self.values)
        crossed["directEngines"]["sharedMount"] = crossed["directEngines"]["modelMount"]
        cases = {
            "at '/engines': maxProperties": both,
            "at '/gates': maxItems": gates,
            "missing property 'command'": incomplete,
            "/directEngines/image": tagged,
            "/directEngines/count": single,
            "give each its own": crossed,
        }
        for message, values in cases.items():
            with self.subTest(message):
                result = self.helm(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        result = self.helm(self.values, namespace="other")
        self.assertIn("install the release there", result.stderr)


if __name__ == "__main__":
    unittest.main()
