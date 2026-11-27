"""The objects the fleet controller derives: the pod specs the chart rendered, owner references,
labels from the allow-list only, deterministic Job names and pruning.

`golden/kubernetes_objects.json` holds `helm template` output of the chart's engine, gate and
HAProxy templates before the builders replaced them, with the values it was rendered from.
"""

import base64
import copy
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.kubernetes_objects import (
    ApiKey,
    Deletion,
    Fleet,
    GateJob,
    Owner,
)
from narwhal.deployment.launch_engine import engine_set
from tests.deployment import chart_fixtures
from tests.deployment.test_helm_chart import Comparison

ROOT = Path(__file__).resolve().parents[2]
GROUP = objects.GROUP
GOLDEN = json.loads((Path(__file__).parent / "golden" / "kubernetes_objects.json").read_text())
INPUTS = GOLDEN["inputs"]
OWNER = Owner("fleet", "0b6f4c1e-5d1a-4c55-9f7e-3a1f0f5e9a11", chart_fixtures.NAMESPACE)
OTHER = Owner("fleet", "6a3c2b9d-1e8f-4f70-8b2a-5c4d3e2f1a00", chart_fixtures.NAMESPACE)
CHART_PLACEMENT = "narwhal.io/placement"


def fleet(owner: Owner = OWNER, **overrides: Any) -> Fleet:
    """The fleet the golden values describe."""
    engines = INPUTS["engines"]
    key = INPUTS["credentials"]["engineApiKey"]
    settings: dict[str, Any] = {
        "owner": owner,
        "narwhal_image": INPUTS["images"]["narwhal"],
        "haproxy_image": INPUTS["images"]["haproxy"],
        "fabric_image": INPUTS["images"]["fabric"],
        "state_claim": engines["stateClaim"],
        "api_key": ApiKey(key["env"], key["secretName"], key["key"]),
        "engine_account": engines["podTemplate"]["serviceAccountName"],
        "rdma_resource": engines["rdmaResource"],
        "model_volume": engines["modelVolume"],
        "cluster_domain": INPUTS["haproxy"]["clusterDomain"],
        "haproxy_replicas": INPUTS["haproxy"]["replicas"],
    }
    settings.update(overrides)
    return Fleet(**settings)


def gate(entry: dict, **overrides: Any) -> GateJob:
    """The gate a chart `gates` entry describes."""
    settings: dict[str, Any] = {
        "name": entry["name"],
        "kind": entry["kind"],
        "command": entry["command"],
        "deadline_seconds": entry["deadlineSeconds"],
        "keep": entry["keep"],
        "node": entry.get("node"),
        "env": entry.get("env", {}),
        "resources": entry.get("resources", {}),
        "image": entry.get("image"),
        "runtime_class": entry.get("runtimeClassName"),
        "host_network": entry.get("hostNetwork", False),
        "inputs": entry.get("inputs"),
        "model_volume": entry.get("modelVolume"),
        "service_account": entry.get("serviceAccount", False),
    }
    settings.update(overrides)
    return GateJob(**settings)


def entry(name: str) -> dict:
    [found] = [item for item in INPUTS["gates"] if item["name"] == name]
    return copy.deepcopy(found)


def everything(target: Fleet | None = None) -> list[dict]:
    """Every object the builders make for the golden fleet."""
    target = target or fleet()
    documents = objects.engines(target, INPUTS["engines"])
    documents += [objects.job(target, gate(item)) for item in INPUTS["gates"]]
    documents += [
        objects.inputs(target, gate(item), {"inputs.json": b"{}"})
        for item in INPUTS["gates"]
        if item.get("inputs")
    ]
    return documents + objects.haproxy(target)


def metadata_blocks(document: Any) -> list[dict]:
    """Every metadata mapping anywhere in an object."""
    found = []
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "metadata" and isinstance(value, dict):
                found.append(value)
            else:
                found += metadata_blocks(value)
    elif isinstance(document, list):
        for value in document:
            found += metadata_blocks(value)
    return found


def spec_only(document: dict) -> dict:
    """An object without what differs by design: the labels and annotations, the owner, the
    namespace, the release in selectors and the group of the placement gate."""
    document = copy.deepcopy(document)
    for block in metadata_blocks(document):
        for key in ("labels", "annotations", "ownerReferences", "namespace"):
            block.pop(key, None)
    selector = document.get("spec", {}).get("selector", {})
    for labels in (selector, selector.get("matchLabels", {})):
        labels.pop("app.kubernetes.io/instance", None)
    for gate_ in (document.get("spec", {}).get("template", {}).get("spec", {})).get(
        "schedulingGates", []
    ):
        if gate_["name"] == CHART_PLACEMENT:
            gate_["name"] = objects.PLACEMENT_GATE
    return document


def by_name(documents: list[dict]) -> dict[tuple[str, str], dict]:
    return {(d["kind"], d["metadata"]["name"]): d for d in documents}


def template(document: dict) -> dict:
    return document["spec"]["template"]["spec"]


class GoldenTests(unittest.TestCase):
    """The builders render the pod specs the chart's templates rendered."""

    def test_the_engines_gates_and_haproxy_match_the_chart(self):
        built = by_name(
            objects.engines(fleet(), INPUTS["engines"])
            + [objects.job(fleet(), gate(item)) for item in INPUTS["gates"]]
            + objects.haproxy(fleet())
        )
        rendered = by_name(GOLDEN["rendered"])
        self.assertEqual(sorted(built), sorted(rendered))
        for key, document in rendered.items():
            with self.subTest(kind=key[0], name=key[1]):
                self.assertEqual(spec_only(built[key]), spec_only(document))

    def test_a_placed_engine_set_matches_the_chart(self):
        [statefulset] = [
            d
            for d in objects.engines(fleet(), INPUTS["engines"], GOLDEN["placement"])
            if d["kind"] == "StatefulSet"
        ]
        self.assertEqual(spec_only(statefulset), spec_only(GOLDEN["placed"]))
        self.assertEqual(template(statefulset)["schedulingGates"], [{"name": f"{GROUP}/placement"}])

    def test_gates_before_any_engine_match_the_chart(self):
        bare = fleet(engine_account=None, rdma_resource=None, model_volume=None)
        kinds = ("discovery", "fabric-probe", "preflight")
        built = by_name(
            [objects.job(bare, gate(item)) for item in INPUTS["gates"] if item["kind"] in kinds]
        )
        rendered = by_name(GOLDEN["withoutEngines"])
        self.assertEqual(sorted(built), sorted(rendered))
        for key, document in rendered.items():
            with self.subTest(name=key[1]):
                self.assertEqual(spec_only(built[key]), spec_only(document))

    def test_selectors_keep_the_charts_labels_except_the_release(self):
        rendered = by_name(GOLDEN["rendered"])
        for key, document in by_name(everything()).items():
            if key not in rendered or "selector" not in document.get("spec", {}):
                continue
            with self.subTest(kind=key[0], name=key[1]):
                selector = document["spec"]["selector"]
                chart = rendered[key]["spec"]["selector"]
                if "matchLabels" in chart:
                    selector, chart = selector["matchLabels"], chart["matchLabels"]
                chart = {k: v for k, v in chart.items() if k != "app.kubernetes.io/instance"}
                self.assertEqual(selector, chart)
                self.assertEqual(
                    set(selector), {"app.kubernetes.io/name", "app.kubernetes.io/component"}
                )

    def test_haproxy_finds_each_router_pod_by_its_dns_name(self):
        config, deployment, _ = objects.haproxy(fleet())
        text = config["data"]["haproxy.cfg"]
        shipped = (ROOT / "deploy" / "ha" / "haproxy.cfg").read_text().splitlines()
        servers = re.findall(r"(?m)^\s*server (router-[ab]) (\S+) (.*)$", text)
        domain = "routers.example.svc.cluster.example"
        self.assertEqual(
            servers,
            [
                ("router-a", f"router-0.{domain}:8000", "resolvers kubernetes init-addr none"),
                ("router-b", f"router-1.{domain}:8000", "resolvers kubernetes init-addr none"),
            ],
        )
        lines = text.splitlines()
        kept = [line for line in shipped if line.strip() and "_ADDRESS:8000" not in line]
        self.assertEqual([line for line in lines if line in kept], kept)
        self.assertIn("resolvers kubernetes", lines)
        self.assertIn("    parse-resolv-conf", lines)
        [golden] = [
            d
            for d in GOLDEN["rendered"]
            if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "haproxy"
        ]
        self.assertEqual(text, golden["data"]["haproxy.cfg"])
        # The pods roll whenever any part of the configuration changes.
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(
            annotations, {f"{GROUP}/config-sha256": hashlib.sha256(text.encode()).hexdigest()}
        )

    def test_haproxy_forwards_only_the_serving_routes(self):
        text = objects.haproxy(fleet())[0]["data"]["haproxy.cfg"]
        frontend = text.split("frontend narwhal\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(frontend[0], "    bind :8000")
        # The deny rule comes before anything sends a request on.
        deny = next(n for n, line in enumerate(frontend) if "http-request deny" in line)
        backend = next(n for n, line in enumerate(frontend) if "default_backend" in line)
        self.assertLess(deny, backend)
        acls: dict[str, list[str]] = {}
        for line in frontend:
            words = line.split()
            if words[:1] == ["acl"]:
                acls[words[1]] = words[2:]
        condition = frontend[deny].split(" unless ", 1)[1]
        self.assertEqual(
            frontend[deny].split(" unless ")[0].split(),
            ["http-request", "deny", "deny_status", "404"],
        )

        def forwarded(method: str, path: str) -> bool:
            def holds(name: str) -> bool:
                kind, *values = acls[name]
                return (method if kind == "method" else path) in values

            return any(
                all(holds(name) for name in alternative.split())
                for alternative in condition.split(" || ")
            )

        routes = {
            ("GET", "/narwhal/state"),
            ("GET", "/narwhal/handoff"),
            ("GET", "/narwhal/lifecycle"),
            ("POST", "/narwhal/lifecycle/drain"),
            ("POST", "/narwhal/lifecycle/readmit"),
            ("GET", "/narwhal/engines"),
            ("POST", "/narwhal/engines"),
            ("DELETE", "/narwhal/engines/n1"),
            ("GET", "/metrics"),
            ("GET", "/openapi.json"),
            ("GET", "/docs"),
            ("GET", "/v1/completions"),
            ("POST", "/v1/models"),
            ("POST", "/narwhal/targets"),
            ("GET", "//health"),
            *objects.SERVING_ROUTES,
        }
        self.assertEqual(
            {route for route in routes if forwarded(*route)}, set(objects.SERVING_ROUTES)
        )
        self.assertIn(("GET", "/narwhal/targets"), objects.SERVING_ROUTES)
        self.assertEqual(
            {path for _, path in objects.SERVING_ROUTES if path.startswith("/narwhal/")},
            {"/narwhal/targets"},
        )


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        cls.runs, values = chart_fixtures.example(cls.root / "fleet")
        cls.values = values["engines"]
        key = values["credentials"]["engineApiKey"]
        cls.fleet = fleet(
            narwhal_image=values["images"]["narwhal"],
            state_claim=cls.values["stateClaim"],
            api_key=ApiKey(key["env"], key["secretName"], key["key"]),
            engine_account=cls.values["podTemplate"]["serviceAccountName"],
        )
        cls.documents = objects.engines(cls.fleet, cls.values)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_the_engine_statefulset_runs_each_plans_pod(self):
        found = by_name(self.documents)
        engines = found[("StatefulSet", "engine")]
        self.assertEqual(engines["spec"]["replicas"], 2)
        self.assertEqual(engines["spec"]["ordinals"], {"start": 1})
        self.assertEqual(engines["spec"]["updateStrategy"], {"type": "OnDelete"})
        self.assertEqual(engines["spec"]["serviceName"], "engines")
        maps = {name: d for (kind, name), d in found.items() if kind == "ConfigMap"}
        self.assertEqual(
            set(maps), {item["metadata"]["name"] for item in self.values["configMaps"]}
        )
        scratch = self.root / "scratch"
        scratch.mkdir()
        for run in self.runs:
            Comparison(self, scratch).same(run, template(engines), maps)
        service = found[("Service", "engines")]
        self.assertEqual(service["spec"]["clusterIP"], "None")
        self.assertTrue(service["spec"]["publishNotReadyAddresses"])
        self.assertNotIn("schedulingGates", template(engines))

    def test_placement_holds_each_engine_pod_until_the_run_pins_it(self):
        placed = objects.engines(self.fleet, self.values, {"engine-1": "a", "engine-2": "b"})
        statefulset = by_name(placed)[("StatefulSet", "engine")]
        self.assertEqual(template(statefulset)["schedulingGates"], [{"name": f"{GROUP}/placement"}])
        with self.assertRaisesRegex(ValueError, "placement names no node for engine-2"):
            objects.engines(self.fleet, self.values, {"engine-1": "a"})

    def test_the_engines_must_fit_their_fleet(self):
        cases = {
            "run the fleet there": (fleet(Owner("fleet", OWNER.uid, "other")), self.values),
            "state claim": (self.fleet_with(state_claim="another-claim"), self.values),
            "another Narwhal image": (
                self.fleet_with(narwhal_image="x@sha256:" + "d" * 64),
                self.values,
            ),
            "set the fleet's engine API key": (self.fleet_with(api_key=None), self.values),
            "Secret engine-credential key api-key": (
                self.fleet_with(api_key=ApiKey("KEY", "other", "api-key")),
                self.values,
            ),
            "ServiceAccount": (self.fleet_with(engine_account="other"), self.values),
        }
        cases["fleet's Service engines"] = (self.fleet, {**self.values, "serviceName": "routers"})
        cases["fleet's RDMA resource"] = (
            self.fleet_with(rdma_resource="rdma.example.com/other"),
            self.values,
        )
        cases["the fleet names None"] = (self.fleet_with(rdma_resource=None), self.values)
        cases["another model volume"] = (
            self.fleet_with(model_volume={"hostPath": {"path": "/models"}}),
            self.values,
        )
        cases["mount another model volume"] = (self.fleet_with(model_volume=None), self.values)
        for message, (target, values) in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                objects.engines(target, values)

    def test_the_fabric_and_discovery_gates_use_the_engine_pods_resource_and_volume(self):
        statefulset = by_name(self.documents)[("StatefulSet", "engine")]
        pod = template(statefulset)
        limits = [c["resources"]["limits"] for c in pod["containers"] if c.get("resources")]
        [model] = [v for v in pod["volumes"] if v["name"] == "model"]
        server = template(objects.job(self.fleet, gate(entry("fabric-node-a-node-b-server"))))
        [rdma] = server["containers"][0]["resources"]["limits"]
        self.assertTrue(limits)
        self.assertTrue(all(limit[rdma] == "1" for limit in limits))
        discovery = template(objects.job(self.fleet, gate(entry("discover-node-a"))))
        self.assertIn(model, discovery["volumes"])

    def fleet_with(self, **overrides: Any) -> Fleet:
        settings = {name: getattr(self.fleet, name) for name in self.fleet.__dataclass_fields__}
        settings.update(overrides)
        return Fleet(**settings)

    def test_check_names_each_difference_between_the_plans_and_the_fleet(self):
        arguments = {
            "namespace": chart_fixtures.NAMESPACE,
            "state_claim": self.values["stateClaim"],
            "narwhal_image": self.values["sidecarImage"],
            "api_key_secret": {"name": "engine-credential", "key": "api-key"},
        }
        engine_set.check(self.values, **arguments)
        for name, value, message in (
            ("namespace", "other", "name namespace example"),
            ("state_claim", "other", "plans name state claim narwhal-state"),
            ("narwhal_image", "x@sha256:" + "d" * 64, "another Narwhal image"),
            ("api_key_secret", {"name": "engine-credential", "key": "other"}, "key api-key"),
        ):
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                engine_set.check(self.values, **{**arguments, name: value})


class GateTests(unittest.TestCase):
    def jobs(self, target: Fleet | None = None) -> dict[str, dict]:
        target = target or fleet()
        return {item["name"]: template(objects.job(target, gate(item))) for item in INPUTS["gates"]}

    def test_gate_jobs_run_once_on_their_node(self):
        job = objects.job(fleet(), gate(entry("fabric-node-a-node-b-server")))
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 180)
        spec = template(job)
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertEqual(spec["nodeName"], "node-b")
        self.assertTrue(spec["hostNetwork"])
        [container] = spec["containers"]
        self.assertEqual(container["image"], chart_fixtures.FABRIC_IMAGE)
        self.assertEqual(container["resources"]["limits"], {"rdma.example.com/hca": "1"})

    def test_the_runners_gates_get_their_inputs_node_and_identity(self):
        jobs = self.jobs()
        prepare = jobs["prepare-engine-1"]
        mounts = {m["mountPath"]: m for m in prepare["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["/narwhal-input"]["readOnly"])
        self.assertTrue(mounts["/model"]["readOnly"])
        volumes = {v["name"]: v for v in prepare["volumes"]}
        self.assertEqual(volumes["narwhal-input"]["configMap"]["name"], "prepare-engine-1")
        self.assertEqual(prepare["nodeName"], "node-a")
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
        # Discovery reads the engines' model volume unless its gate names one.
        discovery = {v["name"]: v for v in jobs["discover-node-a"]["volumes"]}
        self.assertEqual(
            {k: v for k, v in discovery["model"].items() if k != "name"},
            INPUTS["engines"]["modelVolume"],
        )
        own = {v["name"]: v for v in jobs["discover-node-b"]["volumes"]}
        self.assertEqual(own["model"]["hostPath"], {"path": "/models", "type": "Directory"})
        self.assertEqual(jobs["discover-node-b"]["runtimeClassName"], "nvidia")

    def test_only_fabric_and_transfer_jobs_run_on_the_host_network(self):
        # The chart's admission policy allows the host network on these gate kinds alone.
        for kind in objects.GATE_KINDS:
            with self.subTest(kind):
                settings = {"kind": kind, "node": "node-a", "host_network": True}
                if kind in objects.HOST_NETWORK_KINDS:
                    job = objects.job(fleet(), gate(entry("preflight"), **settings))
                    self.assertTrue(template(job)["hostNetwork"])
                else:
                    with self.assertRaisesRegex(ValueError, "run on the host network"):
                        gate(entry("preflight"), **settings)
        on_host = {
            item["kind"]
            for item in INPUTS["gates"]
            if template(objects.job(fleet(), gate(item))).get("hostNetwork")
        }
        self.assertLessEqual(on_host, set(objects.HOST_NETWORK_KINDS))

    def test_only_a_sidecar_verify_job_gets_a_token(self):
        for name, spec in self.jobs().items():
            with self.subTest(name):
                verify = name == "verify-engine-1"
                self.assertEqual(spec["automountServiceAccountToken"], verify)
                self.assertEqual("serviceAccountName" in spec, verify)
        for kind in objects.GATE_KINDS:
            if kind == objects.ACCOUNT_KIND:
                continue
            with self.subTest(kind), self.assertRaisesRegex(ValueError, "only a sidecar-verify"):
                gate(entry("preflight"), kind=kind, node="node-a", service_account=True)
        deployment = objects.haproxy(fleet())[1]
        self.assertFalse(template(deployment)["automountServiceAccountToken"])

    def test_only_the_gates_that_call_engines_get_the_key_and_fabric_jobs_are_confined(self):
        jobs = self.jobs()

        def env(job: str) -> dict:
            return {e["name"]: e for e in jobs[job]["containers"][0]["env"]}

        for job in ("preflight", "prepare-engine-1", "measure-node-a"):
            self.assertEqual(
                env(job)["NARWHAL_ENGINE_API_KEY"]["valueFrom"]["secretKeyRef"],
                {"name": "engine-credential", "key": "api-key"},
            )
        for job in (
            "discover-node-a",
            "fabric-node-a-node-b-server",
            "probe-node-a",
            "verify-engine-1",
            "state-1",
        ):
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
                    "subPath": entry("fabric-node-a-node-b-server")["keep"],
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
        unkeyed = {
            e["name"] for e in self.jobs(fleet(api_key=None))["preflight"]["containers"][0]["env"]
        }
        self.assertEqual(unkeyed, {"NARWHAL_GATE_KEEP"})

    def test_a_gate_that_cannot_run_is_refused(self):
        preflight = entry("preflight")
        refused = {
            "keep": [
                {"keep": keep}
                for keep in ("deploy/../fleet.json", "fleet.json", "deploy/run/.hidden")
            ],
            "invalid variable": [{"env": {"A: b": "1"}}],
            "own": [{"env": {"NARWHAL_GATE_KEEP": "/x"}}],
            "invalid resource": [{"resources": {"cpu x": "1"}}, {"resources": {"cpu": "lots"}}],
            "registry digest": [{"image": "narwhal:latest"}],
            "Kubernetes name": [{"name": "Prepare_1"}, {"name": "x" * 64}, {"node": "Node A"}],
            "no gate kind": [{"kind": "shell"}],
            "list of strings": [{"command": []}],
            "positive whole second": [{"deadline_seconds": 0}],
            "model volume": [{"model_volume": {"emptyDir": {}}}],
            "runs on the node": [{"kind": kind} for kind in objects.NODE_KINDS],
        }
        for message, cases in refused.items():
            for overrides in cases:
                with self.subTest(overrides), self.assertRaisesRegex(ValueError, message):
                    gate(preflight, **overrides)
        server = gate(entry("fabric-node-a-node-b-server"))
        for target, message in (
            (fleet(fabric_image=None), "needs the fabric image"),
            (fleet(rdma_resource=None), "RDMA resource"),
        ):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                objects.job(target, server)
        with self.assertRaisesRegex(ValueError, "engines' ServiceAccount"):
            objects.job(fleet(engine_account=None), gate(entry("verify-engine-1")))

    def test_job_names_are_the_same_on_every_replay_and_differ_by_attempt(self):
        arguments = (OWNER.uid, "fabric-edge", "gpu-a-to-gpu-b", 2, 0)
        name = objects.job_name("fabric-server", *arguments)
        self.assertEqual(name, objects.job_name("fabric-server", *arguments))
        self.assertRegex(name, r"^fabric-server-[0-9a-f]{12}$")
        for index in range(len(arguments)):
            changed = list(arguments)
            changed[index] = changed[index] + 1 if isinstance(changed[index], int) else "x"
            with self.subTest(index):
                self.assertNotEqual(objects.job_name("fabric-server", *changed), name)
        # A fleet recreated under the same name gets a new uid and new Job names.
        self.assertNotEqual(objects.job_name("fabric-server", OTHER.uid, *arguments[1:]), name)
        longest = max(objects.GATE_KINDS, key=len)
        self.assertLessEqual(len(objects.job_name(longest, *arguments)), 63)
        # The parts cannot run into each other.
        self.assertNotEqual(
            objects.job_name("state", OWNER.uid, "a", "bc", 1, 0),
            objects.job_name("state", OWNER.uid, "ab", "c", 1, 0),
        )
        with self.assertRaisesRegex(ValueError, "no gate kind"):
            objects.job_name("shell", *arguments)

    def test_inputs_hold_the_gates_files(self):
        prepare = gate(entry("prepare-engine-1"), run="a" * 32)
        files = {"inputs.json": b'{"a": 1}', "gate.py": b"print()"}
        document = objects.inputs(fleet(), prepare, files)
        self.assertEqual(document["metadata"]["name"], "prepare-engine-1")
        self.assertEqual({k: base64.b64decode(v) for k, v in document["binaryData"].items()}, files)
        self.assertEqual(document["metadata"]["labels"][f"{GROUP}/run"], "a" * 32)
        self.assertEqual(document["metadata"]["labels"][f"{GROUP}/gate"], "prepare")
        job = objects.job(fleet(), prepare)
        self.assertEqual(job["metadata"]["labels"][f"{GROUP}/run"], "a" * 32)
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            objects.inputs(fleet(), prepare, {"big": b"x" * objects.INPUTS_BYTES})
        with self.assertRaisesRegex(ValueError, "ConfigMap key"):
            objects.inputs(fleet(), prepare, {"../x": b""})
        with self.assertRaisesRegex(ValueError, "names no inputs"):
            objects.inputs(fleet(), gate(entry("preflight")), files)


# Every key the denylist names, with a sample key for each prefix.
DENIED = {
    "app.kubernetes.io/instance": "fleet",
    "app.kubernetes.io/managed-by": "Helm",
    "helm.sh/chart": "narwhal-0.13.0",
    "argocd.argoproj.io/tracking-id": "narwhal:narwhal.example.com/NarwhalFleet:example/fleet",
    "argocd.argoproj.io/instance": "narwhal",
    "argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true",
    "meta.helm.sh/release-name": "narwhal",
    "meta.helm.sh/release-namespace": "example",
    "kustomize.toolkit.fluxcd.io/name": "narwhal",
    "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
    "helm.toolkit.fluxcd.io/name": "narwhal",
    "helm.toolkit.fluxcd.io/namespace": "flux-system",
}


class MetadataTests(unittest.TestCase):
    def test_every_builder_names_the_fleet_as_controlling_owner(self):
        for document in everything():
            with self.subTest(kind=document["kind"], name=document["metadata"]["name"]):
                self.assertEqual(
                    document["metadata"]["ownerReferences"],
                    [
                        {
                            "apiVersion": f"{GROUP}/v1alpha1",
                            "kind": "NarwhalFleet",
                            "name": "fleet",
                            "uid": OWNER.uid,
                            "controller": True,
                        }
                    ],
                )
                self.assertEqual(document["metadata"]["namespace"], chart_fixtures.NAMESPACE)
                self.assertTrue(objects.controlled(OWNER, document))
                self.assertFalse(objects.controlled(OTHER, document))

    def test_no_derived_object_carries_the_fleets_metadata(self):
        resource = {
            "apiVersion": f"{GROUP}/v1alpha1",
            "kind": "NarwhalFleet",
            "metadata": {
                "name": "fleet",
                "namespace": chart_fixtures.NAMESPACE,
                "uid": OWNER.uid,
                "labels": {**DENIED, "team": "inference"},
                "annotations": {
                    **DENIED,
                    "kubectl.kubernetes.io/last-applied-configuration": "{}",
                },
            },
        }
        owner = Owner.of(resource)
        self.assertEqual(owner, OWNER)
        fleet_keys = set(resource["metadata"]["labels"]) | set(resource["metadata"]["annotations"])
        for document in everything(fleet(owner)):
            for block in metadata_blocks(document):
                with self.subTest(kind=document["kind"], name=document["metadata"]["name"]):
                    for field_name in ("labels", "annotations"):
                        keys = block.get(field_name, {})
                        self.assertFalse(set(keys) & fleet_keys - set(objects.ALLOWED_LABELS))
                        for key in keys:
                            self.assertTrue(
                                key.startswith(f"{GROUP}/") or key in objects.ALLOWED_LABELS, key
                            )
                    self.assertNotEqual(
                        block.get("labels", {}).get("app.kubernetes.io/managed-by"), "Helm"
                    )
                    self.assertNotIn("app.kubernetes.io/instance", block.get("labels", {}))

    def test_the_denylist_names_every_helm_argo_cd_and_flux_key(self):
        # Apart from the allow-list, which would refuse these keys as well.
        for key, value in DENIED.items():
            with self.subTest(key):
                self.assertTrue(objects.denied(key, value))
        for key, value in (
            ("app.kubernetes.io/managed-by", objects.MANAGER),
            ("app.kubernetes.io/name", "narwhal"),
            (f"{GROUP}/gate", "preflight"),
            ("team", "inference"),
        ):
            with self.subTest(key):
                self.assertFalse(objects.denied(key, value))

    def test_check_metadata_refuses_every_denied_key_wherever_it_stands(self):
        job = objects.job(fleet(), gate(entry("preflight")))
        objects.check_metadata(job)
        for key, value in [*DENIED.items(), ("team", "inference")]:
            for field_name in ("labels", "annotations"):
                for path in ("object", "template"):
                    document = copy.deepcopy(job)
                    block = (
                        document["metadata"]
                        if path == "object"
                        else document["spec"]["template"]["metadata"]
                    )
                    block.setdefault(field_name, {})[key] = value
                    with (
                        self.subTest(key=key, field=field_name, path=path),
                        self.assertRaisesRegex(ValueError, re.escape(key)),
                    ):
                        objects.check_metadata(document)
        # The managed-by label is the controller's; only Helm's value is refused.
        self.assertEqual(job["metadata"]["labels"]["app.kubernetes.io/managed-by"], objects.MANAGER)

    def test_labels_and_a_fleet_from_another_group_are_checked(self):
        with self.assertRaisesRegex(ValueError, "invalid value"):
            objects.metadata(OWNER, "x", "gate", extra={"run": "not a label value"})
        with self.assertRaisesRegex(ValueError, "expected"):
            Owner.of({"apiVersion": "narwhal.io/v1alpha1", "kind": "NarwhalFleet"})
        with self.assertRaisesRegex(ValueError, "uid"):
            Owner("fleet", "", "example")


def live(document: dict, *, uid: str = "", owner: Owner | None = OWNER, **meta: Any) -> dict:
    """An object as the API returns it."""
    document = copy.deepcopy(document)
    document["metadata"]["uid"] = (
        uid
        or hashlib.sha256(
            json.dumps(document["metadata"]["name"] + document["kind"]).encode()
        ).hexdigest()[:8]
    )
    if owner is None:
        document["metadata"].pop("ownerReferences", None)
    else:
        document["metadata"]["ownerReferences"] = [owner.reference()]
    document["metadata"].update(meta)
    return document


def named(
    kind: str,
    name: str,
    owner: Owner | None = OWNER,
    namespace: str = chart_fixtures.NAMESPACE,
    **meta: Any,
) -> dict:
    api = {"Deployment": "apps/v1", "StatefulSet": "apps/v1", "Job": "batch/v1"}
    return live(
        {
            "apiVersion": api.get(kind, "v1"),
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace},
        },
        owner=owner,
        **meta,
    )


class PruneTests(unittest.TestCase):
    def test_prune_deletes_only_what_the_fleet_controls_and_no_longer_wants(self):
        desired = objects.engines(fleet(), INPUTS["engines"]) + objects.haproxy(fleet())
        stale_map = named("ConfigMap", "narwhal-engine-1-000000000000-hooks")
        chart_owned = named(
            "ConfigMap",
            "grafana",
            owner=None,
            labels={"app.kubernetes.io/managed-by": "Helm"},
        )
        not_controller = named("Service", "shared")
        not_controller["metadata"]["ownerReferences"][0]["controller"] = False
        state = named("PersistentVolumeClaim", "narwhal-state")
        live_objects = [
            *[live(document) for document in desired],
            stale_map,
            named("Job", "preflight-0123456789ab"),
            named("Deployment", "old-haproxy"),
            named("StatefulSet", "router"),
            named("Service", "old-engines"),
            chart_owned,
            named("ConfigMap", "other-fleet", owner=OTHER),
            not_controller,
            state,
            named("Secret", "engine-credential"),
            named("ConfigMap", "leaving", deletionTimestamp="2026-11-27T00:00:00Z"),
        ]
        deletions = objects.prune(OWNER, desired, live_objects)
        self.assertEqual(
            [(d.kind, d.name) for d in deletions],
            [
                ("Deployment", "old-haproxy"),
                ("StatefulSet", "router"),
                ("Job", "preflight-0123456789ab"),
                ("Service", "old-engines"),
                ("ConfigMap", "narwhal-engine-1-000000000000-hooks"),
            ],
        )
        self.assertEqual(deletions[-1].uid, stale_map["metadata"]["uid"])

    def test_every_deletion_propagates_in_the_background(self):
        deletions = objects.prune(
            OWNER, [], [named(kind, kind.lower()) for kind in objects.PRUNED_KINDS]
        )
        self.assertEqual({d.kind for d in deletions}, set(objects.PRUNED_KINDS))
        for deletion in deletions:
            with self.subTest(deletion.kind):
                options = deletion.options()
                self.assertEqual(options["propagationPolicy"], "Background")
                self.assertEqual(options["preconditions"], {"uid": deletion.uid})
            self.assertEqual(deletion.namespace, chart_fixtures.NAMESPACE)
        self.assertEqual(
            Deletion("Job", "preflight-0123456789ab", "example", "1234").options(),
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Background",
                "preconditions": {"uid": "1234"},
            },
        )
        with self.assertRaisesRegex(ValueError, "no uid"):
            Deletion("Job", "preflight-0123456789ab", "example", None).options()

    def test_prune_leaves_objects_in_other_namespaces(self):
        elsewhere = [
            named("Job", "preflight-0123456789ab", namespace="other"),
            named("ConfigMap", "stale", namespace=None),
        ]
        self.assertEqual(objects.prune(OWNER, [], elsewhere), [])

    def test_a_hook_map_a_running_engine_pod_mounts_stays(self):
        old = "narwhal-engine-1-000000000000-hooks"
        spec = "narwhal-spec-engine-1-000000000000"
        pod = {
            "metadata": {"name": "engine-1"},
            "spec": {
                "volumes": [
                    {"name": "narwhal-hooks", "configMap": {"name": old}},
                    {
                        "name": "narwhal-spec",
                        "projected": {"sources": [{"configMap": {"name": spec}}, {"secret": {}}]},
                    },
                    {"name": "model", "persistentVolumeClaim": {"claimName": "models"}},
                ]
            },
        }
        self.assertEqual(objects.mounted_maps([pod]), {old, spec})
        stale = [named("ConfigMap", old), named("ConfigMap", spec), named("ConfigMap", "gone")]
        deletions = objects.prune(OWNER, [], stale, pods=[pod])
        self.assertEqual([d.name for d in deletions], ["gone"])
        self.assertEqual(len(objects.prune(OWNER, [], stale)), 3)

    def test_prune_refuses_a_desired_object_of_another_owner(self):
        [service] = [d for d in objects.haproxy(fleet(OTHER)) if d["kind"] == "Service"]
        with self.assertRaisesRegex(ValueError, "another owner"):
            objects.prune(OWNER, [service], [])


if __name__ == "__main__":
    unittest.main()
