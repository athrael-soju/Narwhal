"""The Helm chart: the fleet controller with only the access it uses, the admission policy that
bounds what a fleet can run, and what stays in the chart. The engine StatefulSet the
controller builds runs the pod each plan rendered.

The tests that render the chart need helm: HELM naming it, `helm` on PATH, or the one
`make chart-tools` installs. They skip without it.
"""

import copy
import json
import os
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
from narwhal.deployment import direct_engine_set, fleet_resource
from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.direct_start import DirectEngine
from narwhal.deployment.fleet_resource import VolumePolicy
from narwhal.deployment.launch_engine import engine_set
from narwhal.deployment.launch_engine.kubernetes import MANIFESTS
from narwhal.observability.stack.artifacts import DEFAULT_CONSOLE_URL, stage_artifacts
from narwhal.observability.stack.chart_dashboard import chart_dashboard
from narwhal.observability.stack.make_targets import load_contract
from tests.deployment import chart_fixtures, chart_live
from tests.deployment.pod_fixtures import NODE, POD_IP, POD_UID, SECRET_VALUE, expand

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
OBSERVABILITY = ROOT / "src" / "narwhal" / "observability" / "stack"
SERVICES = f"{chart_fixtures.NAMESPACE}.svc.cluster.local"
ROUTERS = [f"router-{n}.routers.{SERVICES}:8000" for n in (0, 1)]
LOAD_BALANCER = f"haproxy.{SERVICES}:8000"
SIDECAR = "attestation"
# The release the tests install, and the accounts the chart names after it.
CONTROLLER = "fleet-controller"
ENGINE_ACCOUNT = "fleet-engine"


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
    def render(cls, values: dict, *, namespace: str = chart_fixtures.NAMESPACE) -> list[dict]:
        result = cls.helm(values, namespace=namespace)
        if result.returncode:
            raise AssertionError(result.stderr)
        return [document for document in yaml.safe_load_all(result.stdout) if document]

    def render_in(self, namespace: str) -> list[dict]:
        return self.render(self.values, namespace=namespace)

    def find(self, kind: str, name: str) -> dict:
        [document] = [
            d for d in self.documents if d["kind"] == kind and d["metadata"]["name"] == name
        ]
        return document


def rules(role: dict) -> set[tuple[str, str, str, str]]:
    """A Role's grants, one (API group, resource, verb, resource name or "") each."""
    found = set()
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                for verb in rule["verbs"]:
                    for name in rule.get("resourceNames") or [""]:
                        found.add((group, resource, verb, name))
    return found


def pod_templates(documents: list[dict]) -> list[tuple[str, dict]]:
    return [
        (f"{document['kind']} {document['metadata']['name']}", document["spec"]["template"])
        for document in documents
        if document["kind"] in ("StatefulSet", "Deployment", "Job")
    ]


@unittest.skipUnless(HELM, "helm is not installed")
class ChartTests(Rendering, unittest.TestCase):
    """A release that installs a fleet, with monitoring: the controller, its RBAC and the
    admission policy, and what stays in the chart."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        cls.values = chart_fixtures.values()
        cls.documents = cls.render(cls.values)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def kinds(self, documents: list[dict] | None = None) -> set[tuple[str, str]]:
        return {(d["kind"], d["metadata"]["name"]) for d in documents or self.documents}

    def test_the_controller_owns_the_engines_gate_jobs_and_haproxy(self):
        kinds = {kind for kind, _ in self.kinds()}
        self.assertNotIn("Job", kinds)
        self.assertNotIn(("StatefulSet", "engine"), self.kinds())
        self.assertNotIn(("Deployment", "haproxy"), self.kinds())
        self.assertNotIn(("ConfigMap", "haproxy"), self.kinds())
        templates = {path.name for path in (CHART / "templates").iterdir()}
        self.assertFalse(templates & {"engines.yaml", "gates.yaml", "haproxy.yaml"})
        schema = json.loads((CHART / "values.schema.json").read_text())["properties"]
        self.assertFalse(set(schema) & {"engines", "gates", "placement"})
        for name in ("engines", "gates", "placement"):
            with self.subTest(name):
                result = self.helm({**self.values, name: {}})
                self.assertIn(f"additional properties '{name}' not allowed", result.stderr)

    def test_the_controller_runs_alone_as_a_non_root_user_with_a_read_only_root(self):
        deployment = self.find("Deployment", CONTROLLER)
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        spec = deployment["spec"]["template"]["spec"]
        self.assertEqual(
            spec["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        [container] = spec["containers"]
        self.assertNotIn("initContainers", spec)
        self.assertEqual(
            container["securityContext"],
            {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        self.assertEqual(container["image"], self.values["images"]["narwhal"])
        mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
        self.assertNotIn("readOnly", mounts["/narwhal-state"])
        self.assertTrue(mounts["/etc/narwhal"]["readOnly"])
        self.assertEqual(mounts["/tmp"]["name"], "tmp")
        values = copy.deepcopy(self.values)
        values["controller"] = {"runAsUser": 0, "runAsGroup": 0}
        self.assertIn("/controller/runAsUser", self.helm(values).stderr)
        # The engines' attestation sidecars run as a non-root user of their own.
        values = copy.deepcopy(self.values)
        values["sidecarUser"] = 0
        self.assertIn("/sidecarUser", self.helm(values).stderr)

    def test_only_the_controller_container_mounts_a_token(self):
        for kind, name in self.kinds():
            if kind == "ServiceAccount":
                with self.subTest(account=name):
                    account = self.find(kind, name)
                    self.assertIs(account["automountServiceAccountToken"], False)
        tokens = []
        for workload, template in pod_templates(self.documents):
            spec = template["spec"]
            with self.subTest(workload):
                self.assertIs(spec["automountServiceAccountToken"], False)
            for volume in spec.get("volumes", []):
                sources = (volume.get("projected") or {}).get("sources", [])
                if any("serviceAccountToken" in source for source in sources):
                    tokens.append((workload, volume["name"], sources))
            mounted = [
                (container["name"], mount["name"])
                for container in [*spec.get("initContainers", []), *spec["containers"]]
                for mount in container.get("volumeMounts", [])
                if mount["mountPath"].startswith("/var/run/secrets")
            ]
            if workload == f"Deployment {CONTROLLER}":
                self.assertEqual(mounted, [("controller", "api")])
            else:
                self.assertEqual(mounted, [], workload)
        [(workload, volume, sources)] = tokens
        self.assertEqual((workload, volume), (f"Deployment {CONTROLLER}", "api"))
        self.assertEqual(
            sources[0], {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}
        )
        self.assertEqual(
            sources[1]["configMap"],
            {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]},
        )
        spec = self.find("Deployment", CONTROLLER)["spec"]["template"]["spec"]
        self.assertEqual(spec["serviceAccountName"], CONTROLLER)

    def test_the_controller_reads_the_charts_settings(self):
        values = copy.deepcopy(self.values)
        policy = {
            "modelVolumeSources": ["persistentVolumeClaim", "hostPath", "nfs", "csi"],
            "modelHostPathPrefixes": ["/srv/models"],
            "modelNfsServers": ["nfs.example.com"],
            "modelCsiDrivers": ["csi.example.com"],
        }
        values.update(policy)
        values["runtimeClassNames"] = ["nvidia"]
        values["state"]["claimName"] = "fleet-state"
        documents = self.render(values)
        [config] = [
            d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"] == CONTROLLER
        ]
        settings = json.loads(config["data"]["controller.json"])
        self.assertEqual(
            settings,
            {
                "release": "fleet",
                "stateClaim": "fleet-state",
                "engineServiceAccount": ENGINE_ACCOUNT,
                "sidecarUser": 10002,
                **policy,
                "runtimeClassNames": ["nvidia"],
                "admissionPolicy": f"{chart_fixtures.NAMESPACE}-narwhal",
            },
        )
        self.assertEqual(
            VolumePolicy.from_values(settings),
            VolumePolicy(
                ("persistentVolumeClaim", "hostPath", "nfs", "csi"),
                ("/srv/models",),
                ("nfs.example.com",),
                ("csi.example.com",),
            ),
        )
        default = json.loads(self.find("ConfigMap", CONTROLLER)["data"]["controller.json"])
        self.assertEqual(VolumePolicy.from_values(default), VolumePolicy())
        [deployment] = [
            d
            for d in documents
            if d["kind"] == "Deployment" and d["metadata"]["name"] == CONTROLLER
        ]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"][-2:], ["--settings", "/etc/narwhal/controller.json"])
        claims = {
            v["persistentVolumeClaim"]["claimName"]
            for v in deployment["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in v
        }
        self.assertEqual(claims, {"fleet-state"})
        # A changed setting restarts the controller.
        annotation = f"{fleet_resource.GROUP}/config-sha256"

        def digest(document: dict) -> str:
            return document["spec"]["template"]["metadata"]["annotations"][annotation]

        self.assertNotEqual(digest(deployment), digest(self.find("Deployment", CONTROLLER)))

    def test_the_controllers_role_holds_only_the_verbs_it_uses(self):
        role = self.find("Role", CONTROLLER)
        self.assertEqual(rules(role), chart_live.CONTROLLER_RULES)
        granted = {(group, resource) for group, resource, _, _ in rules(role)}
        for group, resource in granted:
            self.assertNotIn("*", (group, resource))
            self.assertNotIn(resource, ("secrets", "pods/exec", "pods/attach", "serviceaccounts"))
            self.assertNotEqual(group, "rbac.authorization.k8s.io")
        # Only status is written on the fleet.
        fleet = {(r, v) for g, r, v, _ in rules(role) if g == fleet_resource.GROUP}
        self.assertEqual(
            {verb for resource, verb in fleet if resource == fleet_resource.PLURAL},
            {"get", "list", "watch"},
        )
        binding = self.find("RoleBinding", CONTROLLER)
        self.assertEqual(binding["roleRef"]["name"], CONTROLLER)
        self.assertEqual(
            binding["subjects"],
            [{"kind": "ServiceAccount", "name": CONTROLLER, "namespace": chart_fixtures.NAMESPACE}],
        )

    def test_the_cluster_role_reads_nodes_and_the_fleet_crd_only(self):
        name = f"{chart_fixtures.NAMESPACE}-fleet-narwhal-nodes"
        role = self.find("ClusterRole", name)
        self.assertEqual(rules(role), chart_live.cluster_rules(chart_fixtures.NAMESPACE))
        binding = self.find("ClusterRoleBinding", name)
        self.assertEqual(
            binding["roleRef"],
            {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": name},
        )
        self.assertEqual(
            binding["subjects"],
            [{"kind": "ServiceAccount", "name": CONTROLLER, "namespace": chart_fixtures.NAMESPACE}],
        )
        # Two releases of one name in two namespaces get two cluster roles.
        other = {(d["kind"], d["metadata"]["name"]) for d in self.render_in("other")}
        self.assertIn(("ClusterRole", "other-fleet-narwhal-nodes"), other)
        self.assertIn(("ValidatingAdmissionPolicy", "other-narwhal"), other)

    def test_the_fleet_editor_role_is_its_own_grant(self):
        role = self.find("Role", "fleet-fleet-editor")
        self.assertFalse(
            [key for key in role["metadata"].get("labels", {}) if "aggregate-to" in key]
        )
        self.assertEqual(
            {(group, resource) for group, resource, _, _ in rules(role)},
            {
                (fleet_resource.GROUP, fleet_resource.PLURAL),
                (fleet_resource.GROUP, f"{fleet_resource.PLURAL}/status"),
            },
        )
        bindings = [d for d in self.documents if d["kind"] in ("RoleBinding", "ClusterRoleBinding")]
        self.assertFalse([b for b in bindings if b["roleRef"]["name"] == "fleet-fleet-editor"])

    def test_the_engines_account_only_gets_pods(self):
        self.assertEqual(rules(self.find("Role", ENGINE_ACCOUNT)), {("", "pods", "get", "")})
        binding = self.find("RoleBinding", ENGINE_ACCOUNT)
        self.assertEqual(binding["subjects"][0]["name"], ENGINE_ACCOUNT)

    def test_the_routers_may_hold_only_their_own_lease(self):
        role = self.find("Role", "narwhal-router")
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": ["coordination.k8s.io"],
                    "resources": ["leases"],
                    "resourceNames": ["narwhal-router"],
                    "verbs": ["get", "update"],
                },
                # A create cannot name its object in RBAC, so it is a rule of its own.
                {
                    "apiGroups": ["coordination.k8s.io"],
                    "resources": ["leases"],
                    "verbs": ["create"],
                },
            ],
        )
        binding = self.find("RoleBinding", "narwhal-router")
        self.assertEqual(binding["roleRef"]["name"], "narwhal-router")
        self.assertEqual(
            binding["subjects"], [{"kind": "ServiceAccount", "name": "narwhal-router"}]
        )
        self.assertFalse(
            self.find("ServiceAccount", "narwhal-router")["automountServiceAccountToken"]
        )
        # No other Role in the release mentions Leases but the controller's own.
        for document in self.documents:
            if document["kind"] == "Role" and document["metadata"]["name"] not in (
                "narwhal-router",
                CONTROLLER,
            ):
                self.assertNotIn("leases", json.dumps(document["rules"]))

    def test_the_state_claim_outlives_the_release(self):
        claim = self.find("PersistentVolumeClaim", "narwhal-state")
        self.assertEqual(claim["metadata"]["annotations"], {"helm.sh/resource-policy": "keep"})
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteMany"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "10Gi")

    def test_the_values_install_the_fleet(self):
        fleet = self.find(fleet_resource.KIND, fleet_resource.NAME)
        self.assertEqual(fleet["apiVersion"], fleet_resource.API_VERSION)
        self.assertEqual(fleet["spec"], self.values["fleet"])
        self.assertEqual(
            fleet["metadata"]["annotations"],
            {"argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true"},
        )
        without = {k: v for k, v in self.values.items() if k != "fleet"}
        self.assertNotIn(fleet_resource.KIND, {d["kind"] for d in self.render(without)})
        cases = {
            "/fleet/engines/image": lambda s: s["engines"].update(image="engine:latest"),
            "serviceAccount": lambda s: s["engines"].update(serviceAccount="x"),
            "/fleet/engines/nodes": lambda s: s["engines"]["nodes"].pop(),
        }
        for message, change in cases.items():
            with self.subTest(message):
                values = copy.deepcopy(self.values)
                change(values["fleet"])
                result = self.helm(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_the_admission_policy_checks_the_namespaces_pods_and_workloads(self):
        name = f"{chart_fixtures.NAMESPACE}-narwhal"
        policy = self.find("ValidatingAdmissionPolicy", name)["spec"]
        self.assertEqual(policy["failurePolicy"], "Fail")
        matched = {
            (group, resource, tuple(rule["operations"]))
            for rule in policy["matchConstraints"]["resourceRules"]
            for group in rule["apiGroups"]
            for resource in rule["resources"]
        }
        operations = ("CREATE", "UPDATE")
        self.assertEqual(
            matched,
            {
                ("", "pods", operations),
                ("", "pods/ephemeralcontainers", operations),
                ("batch", "jobs", operations),
                ("apps", "statefulsets", operations),
                ("apps", "deployments", operations),
                ("apps", "replicasets", operations),
            },
        )
        # helm uninstall keeps both, so the policy outlives the controller and what it made.
        for kind in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"):
            annotations = self.find(kind, name)["metadata"]["annotations"]
            self.assertEqual(annotations, {"helm.sh/resource-policy": "keep"})
        binding = self.find("ValidatingAdmissionPolicyBinding", name)["spec"]
        self.assertEqual(binding["policyName"], name)
        self.assertEqual(binding["validationActions"], ["Deny"])
        self.assertEqual(
            binding["matchResources"],
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": chart_fixtures.NAMESPACE}
                }
            },
        )

    def test_the_admission_policy_takes_the_controllers_rules(self):
        def policy(values: dict) -> tuple[dict, str]:
            [document] = [
                d for d in self.render(values) if d["kind"] == "ValidatingAdmissionPolicy"
            ]
            variables = {v["name"]: v["expression"] for v in document["spec"]["variables"]}
            return variables, "\n".join(v["expression"] for v in document["spec"]["validations"])

        variables, validations = policy(self.values)
        self.assertEqual(json.loads(variables["sources"]), ["persistentVolumeClaim"])
        for name in ("hostPathPrefixes", "nfsServers", "csiDrivers"):
            self.assertEqual(json.loads(variables[name]), [])
        values = copy.deepcopy(self.values)
        values.update(chart_live.POLICIES[1])
        values["modelHostPathPrefixes"] = ['/srv/it\'s "here"']
        variables, _ = policy(values)
        self.assertEqual(json.loads(variables["sources"]), values["modelVolumeSources"])
        self.assertEqual(json.loads(variables["hostPathPrefixes"]), values["modelHostPathPrefixes"])
        self.assertEqual(json.loads(variables["nfsServers"]), values["modelNfsServers"])
        self.assertEqual(json.loads(variables["csiDrivers"]), values["modelCsiDrivers"])
        # The rules the controller applies, written into the CEL as raw strings.
        for pattern in (
            fleet_resource.HOST_PATH_REFUSED,
            fleet_resource._NORMALISED,
            fleet_resource._DOT_SEGMENT,
            fleet_resource._DIGEST,
        ):
            self.assertIn(f"matches(r'{pattern}')", validations)
        self.assertIn(json.dumps(list(objects.HOST_NETWORK_KINDS)), variables["hostNetworkGate"])
        self.assertIn(f"'{fleet_resource.GATE_LABEL}'", variables["hostNetworkGate"])
        self.assertIn(f"v.name != '{objects.MODEL_VOLUME}'", validations)
        self.assertEqual(json.loads(variables["runtimeClasses"]), [])
        values["runtimeClassNames"] = ["nvidia"]
        variables, _ = policy(values)
        self.assertEqual(json.loads(variables["runtimeClasses"]), ["nvidia"])
        # Every case the live check sends is one a fleet's objects or a refusal covers.
        self.assertTrue(chart_live.refused("fleet"))
        for policy_values in chart_live.POLICIES:
            cases = chart_live.volume_cases(policy_values)
            self.assertEqual(len(cases), len(chart_live.MODEL_VOLUMES))

    def test_the_policy_trusts_a_label_only_from_the_controller_or_the_pods_maker(self):
        [document] = [d for d in self.documents if d["kind"] == "ValidatingAdmissionPolicy"]
        variables = {v["name"]: v["expression"] for v in document["spec"]["variables"]}
        # A workload is the controller's when its ServiceAccount sends it.
        self.assertIn(
            f"variables.requester == 'system:serviceaccount:{chart_fixtures.NAMESPACE}:"
            f"{CONTROLLER}'",
            variables["trusted"],
        )
        # A pod is a workload's when that workload's Kubernetes controller sends it.
        self.assertIn("'system:serviceaccount:kube-system:'", variables["madeByOwner"])
        for kind, maker in chart_live.MAKERS.items():
            self.assertIn(f"'{kind}': '{maker}'", variables["makers"])
        self.assertIn("variables.madeByOwner ? variables.owner", variables["trusted"])
        self.assertEqual(variables["requester"], "request.userInfo.username")
        # The exemptions and the NetworkPolicy's peer labels need a trusted sender.
        self.assertIn("variables.controlled", variables["engine"])
        self.assertIn("variables.controlled", variables["hostNetworkGate"])
        self.assertIn(
            "{'engine': ['StatefulSet'], 'router': ['StatefulSet'], 'gate': ['Job'],\n"
            " 'haproxy': ['Deployment', 'ReplicaSet']}",
            variables["kinds"],
        )
        peers = {
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
            for peer in self.find("NetworkPolicy", "fleet-routers")["spec"]["ingress"][0]["from"]
        }
        # Every peer label but Prometheus's, which the chart's own Deployment carries.
        self.assertEqual(peers - {"prometheus"}, {"gate", "haproxy", "router"})
        kinds = variables["kinds"]
        for component in peers - {"prometheus"}:
            self.assertIn(f"'{component}':", kinds)

    def test_every_pod_the_chart_runs_passes_the_policy_s_seccomp_rule(self):
        for values in (self.values, chart_fixtures.direct_example()):
            for workload, template in pod_templates(self.render(values)):
                with self.subTest(workload):
                    self.assertEqual(
                        template["spec"]["securityContext"]["seccompProfile"],
                        {"type": "RuntimeDefault"},
                    )

    def test_the_routers_admit_only_haproxy_gates_each_other_and_monitoring(self):
        policy = self.find("NetworkPolicy", "fleet-routers")["spec"]
        self.assertEqual(
            policy["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "narwhal", "app.kubernetes.io/component": "router"},
        )
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        [rule] = policy["ingress"]
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 8000}])
        peers = [
            p["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for p in rule["from"]
        ]
        self.assertEqual(peers, ["haproxy", "gate", "router", "prometheus"])
        router = self.find("StatefulSet", "router")["spec"]["template"]["metadata"]["labels"]
        self.assertLessEqual(policy["podSelector"]["matchLabels"].items(), router.items())
        prometheus = self.find("Deployment", "prometheus")["spec"]["template"]["metadata"]
        self.assertLessEqual(
            rule["from"][3]["podSelector"]["matchLabels"].items(), prometheus["labels"].items()
        )
        values = copy.deepcopy(self.values)
        cluster = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "mon"}}}
        values["networkPolicy"] = {"enabled": True, "monitoring": [cluster]}
        [rendered] = [d for d in self.render(values) if d["kind"] == "NetworkPolicy"]
        self.assertEqual(rendered["spec"]["ingress"][0]["from"][-1], cluster)
        # Without the chart's Prometheus, no pod label it cannot reserve reaches the routers.
        values["monitoring"] = {"enabled": False}
        [rendered] = [d for d in self.render(values) if d["kind"] == "NetworkPolicy"]
        components = [
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
            for peer in rendered["spec"]["ingress"][0]["from"]
            if "podSelector" in peer
        ]
        self.assertEqual(components, ["haproxy", "gate", "router"])
        values["networkPolicy"] = {"enabled": False}
        self.assertNotIn("NetworkPolicy", {d["kind"] for d in self.render(values)})

    def test_only_the_controller_writes_the_state_volume(self):
        claim = self.values["state"]["claimName"]
        writers = []
        for workload, template in pod_templates(self.documents):
            spec = template["spec"]
            state = {
                volume["name"]
                for volume in spec["volumes"]
                if volume.get("persistentVolumeClaim", {}).get("claimName") == claim
            }
            writers += [
                workload
                for container in [*spec.get("initContainers", []), *spec["containers"]]
                for mount in container.get("volumeMounts", [])
                if mount["name"] in state and not mount.get("readOnly")
            ]
        self.assertEqual(writers, [f"Deployment {CONTROLLER}"])
        router = self.find("StatefulSet", "router")["spec"]["template"]["spec"]
        [volume] = [v for v in router["volumes"] if v["name"] == "narwhal-state"]
        self.assertTrue(volume["persistentVolumeClaim"]["readOnly"])

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
            "narwhal": without("images.narwhal"),
            "size": without("state.size"),
            "fleet": without("routers.fleet"),
            "modelVolume": without("fleet.engines.modelVolume"),
        }
        cases["/images/narwhal"] = {**self.values, "images": {"narwhal": "narwhal:0.13.0"}}
        for message, values in cases.items():
            with self.subTest(message):
                result = self.helm(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        defaults = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertNotEqual(self.helm(defaults).returncode, 0)

    def sidecars(self) -> list[str]:
        """Each engine's attestation sidecar, at the address `spec_env` gives the engine."""
        env = fleet_resource.spec_env(
            {
                "metadata": {"name": "fleet", "namespace": chart_fixtures.NAMESPACE},
                "spec": self.values["fleet"],
            },
            state_claim="narwhal-state",
            policy=VolumePolicy(),
        )
        return [
            env[f"NARWHAL_NODE_{n}_ATTESTATION_URL"].split("/")[2]
            for n in range(1, len(self.values["fleet"]["engines"]["nodes"]) + 1)
        ]

    def mounted(self, deployment: str, volume: str) -> dict[str, str]:
        """The files a Deployment's ConfigMap volume holds, by their path in the volume."""
        spec = self.find("Deployment", deployment)["spec"]["template"]["spec"]
        [source] = [v["configMap"] for v in spec["volumes"] if v["name"] == volume]
        data = self.find("ConfigMap", source["name"])["data"]
        items = source.get("items") or [{"key": key, "path": key} for key in data]
        return {item["path"]: data[item["key"]] for item in items}

    def test_monitoring_reads_the_files_make_observe_stages(self):
        self.maxDiff = None
        staged = self.root / "observe"
        contract = load_contract(
            [f"http://{router}" for router in ROUTERS],
            f"http://{LOAD_BALANCER}",
            sidecars=[f"http://{sidecar}" for sidecar in self.sidecars()],
            # The chart labels the series with the release name unless monitoring.fleet is set.
            fleet="fleet",
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

    def test_prometheus_labels_the_fleet_scrapes_each_sidecar_and_sends_alerts(self):
        files = self.mounted("prometheus", "config")
        config = yaml.safe_load(files["prometheus.yml"])
        for job in config["scrape_configs"]:
            self.assertEqual(
                job["relabel_configs"], [{"target_label": "fleet", "replacement": "fleet"}]
            )
        jobs = {job["job_name"]: job for job in config["scrape_configs"]}
        [sidecars] = jobs["sidecars"]["file_sd_configs"]
        [path] = sidecars["files"]
        self.assertEqual(
            json.loads(files[path.removeprefix("/etc/prometheus/")]),
            [{"targets": self.sidecars()}],
        )
        # A release without a fleet names no sidecar.
        without = {k: v for k, v in self.values.items() if k != "fleet"}
        [configs] = [
            d
            for d in self.render(without)
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "prometheus"
        ]
        self.assertEqual(configs["data"]["sidecars.json"], "[]\n")
        # No Alertmanager by default: alerts show on the Prometheus alerts page only.
        [manager] = config["alerting"]["alertmanagers"]
        self.assertEqual(manager["static_configs"], [{"targets": []}])
        values = copy.deepcopy(self.values)
        values["monitoring"]["fleet"] = "fleet-b"
        values["monitoring"]["alertmanagerUrl"] = "https://alertmanager.monitoring:9093/am"
        [configs] = [
            d
            for d in self.render(values)
            if d["kind"] == "ConfigMap" and "prometheus.yml" in d.get("data", {})
        ]
        config = yaml.safe_load(configs["data"]["prometheus.yml"])
        [manager] = config["alerting"]["alertmanagers"]
        self.assertEqual(
            (manager["scheme"], manager["path_prefix"], manager["static_configs"]),
            ("https", "/am", [{"targets": ["alertmanager.monitoring:9093"]}]),
        )
        self.assertEqual(
            config["scrape_configs"][0]["relabel_configs"][0]["replacement"], "fleet-b"
        )

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
        self.assertEqual(
            env["GF_SECURITY_ALLOW_EMBEDDING"],
            compose["grafana"]["environment"]["GF_SECURITY_ALLOW_EMBEDDING"],
        )
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

    def test_grafana_grants_no_anonymous_access_unless_a_value_enables_it(self):
        def env(values: dict) -> dict:
            [grafana] = [
                d
                for d in self.render(values)
                if d["kind"] == "Deployment" and d["metadata"]["name"] == "grafana"
            ]
            [container] = grafana["spec"]["template"]["spec"]["containers"]
            return {item["name"]: item.get("value") for item in container["env"]}

        default = env(self.values)
        self.assertEqual(default["GF_AUTH_ANONYMOUS_ENABLED"], "false")
        self.assertNotIn("GF_AUTH_ANONYMOUS_ORG_ROLE", default)
        values = copy.deepcopy(self.values)
        values["monitoring"]["grafana"]["anonymous"] = True
        compose = yaml.safe_load((OBSERVABILITY / "compose.yml").read_text())["services"]
        enabled = env(values)
        for name in ("GF_AUTH_ANONYMOUS_ENABLED", "GF_AUTH_ANONYMOUS_ORG_ROLE"):
            self.assertEqual(enabled[name], compose["grafana"]["environment"][name], name)

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

    def test_the_fleet_path_must_be_a_name_on_the_state_volume(self):
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
        self.assertNotIn(fleet_resource.KIND, kinds)
        for document in self.documents:
            if document["kind"] not in ("StatefulSet", "Deployment"):
                continue
            if document["metadata"]["name"] == CONTROLLER:
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
        incomplete = copy.deepcopy(self.values)
        del incomplete["directEngines"]["command"]
        tagged = copy.deepcopy(self.values)
        tagged["directEngines"]["image"] = "llama.cpp:server"
        single = copy.deepcopy(self.values)
        single["directEngines"]["count"] = 1
        crossed = copy.deepcopy(self.values)
        crossed["directEngines"]["sharedMount"] = crossed["directEngines"]["modelMount"]
        cases = {
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
