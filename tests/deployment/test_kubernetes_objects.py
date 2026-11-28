"""The objects the fleet controller derives: the pod specs the chart rendered, owner references,
labels from the allow-list only, deterministic Job names and pruning.

`golden/kubernetes_objects.json` holds `helm template` output of the chart's engine, gate and
HAProxy templates before the builders replaced them, with the values it was rendered from.
`golden/kubernetes_routers.json` holds the same of the chart's router templates.
"""

import base64
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from narwhal.deployment import fleet_resource
from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.kubernetes_objects import (
    ApiKey,
    Deletion,
    Fleet,
    GateJob,
    Owner,
    Routers,
)
from narwhal.deployment.kubernetes_target import ROUTER_HOME, ROUTERS
from narwhal.deployment.launch_engine import engine_set
from tests.deployment import chart_fixtures
from tests.deployment.pod_fixtures import POD_IP, SECRET_VALUE, expand
from tests.deployment.test_helm_chart import Comparison

ROOT = Path(__file__).resolve().parents[2]
GROUP = objects.GROUP
GOLDEN = json.loads((Path(__file__).parent / "golden" / "kubernetes_objects.json").read_text())
INPUTS = GOLDEN["inputs"]
ROUTER_GOLDEN = json.loads(
    (Path(__file__).parent / "golden" / "kubernetes_routers.json").read_text()
)
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
    return documents + objects.haproxy(target) + objects.routers(target, router_settings())


def router_settings(**overrides: Any) -> Routers:
    """The router settings the router golden's values describe."""
    values = ROUTER_GOLDEN["inputs"]["routers"]
    settings: dict[str, Any] = {
        "fleet_file": values["fleet"],
        "storage_class": values["storageClassName"],
        "size": values["size"],
        "args": tuple(values["args"]),
    }
    settings.update(overrides)
    return Routers(**settings)


def router_fleet(**overrides: Any) -> Fleet:
    """The fleet the router golden's values describe."""
    inputs = ROUTER_GOLDEN["inputs"]
    key = inputs["credentials"]["engineApiKey"]
    settings: dict[str, Any] = {
        "narwhal_image": inputs["images"]["narwhal"],
        "state_claim": inputs["stateClaim"],
        "api_key": ApiKey(key["env"], key["secretName"], key["key"]),
        "cluster_domain": inputs["haproxy"]["clusterDomain"],
    }
    settings.update(overrides)
    return fleet(**settings)


def router_set(target: Fleet | None = None, settings: Routers | None = None) -> dict:
    [found] = [
        d
        for d in objects.routers(target or router_fleet(), settings or router_settings())
        if d["kind"] == "StatefulSet"
    ]
    return found


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


# The gate Jobs whose engine API key the builders give differently from the chart.
MOVED_KEY = ("prepare-engine-1", "verify-engine-1")
KEY_VARIABLE = "NARWHAL_ENGINE_API_KEY"


def env_names(document: dict) -> list[str]:
    return [e["name"] for e in template(document)["containers"][0].get("env") or []]


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
                ours, chart = spec_only(built[key]), spec_only(document)
                if key[1] in MOVED_KEY:
                    # The chart gave the key to prepare, whose launch checks read it; the
                    # builders give it to sidecar-verify, which calls the engine, instead.
                    has, lacks = (ours, chart) if key[1] == "verify-engine-1" else (chart, ours)
                    self.assertIn(KEY_VARIABLE, env_names(has))
                    self.assertNotIn(KEY_VARIABLE, env_names(lacks))
                    for side in (ours, chart):
                        container = template(side)["containers"][0]
                        container["env"] = [
                            e for e in container["env"] if e["name"] != KEY_VARIABLE
                        ]
                self.assertEqual(ours, chart)

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

    def test_the_launchs_engine_pods_pass_the_charts_admission_policy(self):
        engines = by_name(self.documents)[("StatefulSet", "engine")]
        self.assertEqual(policy_refusals(engines), [])
        spec = template(engines)
        [checkpoint] = [c for c in spec["initContainers"] if c["name"] == "checkpoint"]
        self.assertEqual(checkpoint["securityContext"]["capabilities"]["add"], ["CHOWN", "FOWNER"])
        # CHOWN and FOWNER are the checkpoint step's alone.
        broken = copy.deepcopy(engines)
        for container in template(broken)["containers"]:
            container.setdefault("securityContext", {}).setdefault("capabilities", {})["add"] = [
                "CHOWN"
            ]
        self.assertEqual(policy_refusals(broken), ["capabilities"])

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

    def test_gate_and_haproxy_pods_run_the_runtime_default_seccomp_profile(self):
        # The chart's admission policy refuses a pod outside the engines without one.
        pods = [*self.jobs().values(), template(objects.haproxy(fleet())[1])]
        for spec in pods:
            self.assertEqual(
                spec["securityContext"], {"seccompProfile": {"type": "RuntimeDefault"}}
            )

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

        for job in ("preflight", "verify-engine-1", "measure-node-a"):
            self.assertEqual(
                env(job)["NARWHAL_ENGINE_API_KEY"]["valueFrom"]["secretKeyRef"],
                {"name": "engine-credential", "key": "api-key"},
            )
        for job in (
            "discover-node-a",
            "fabric-node-a-node-b-server",
            "probe-node-a",
            "prepare-engine-1",
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


def without_release(document: dict) -> dict:
    """An object without what differs by design, as `spec_only`, and without the release in
    the routers' spread constraint.

    The chart at ad38650c ran the routers with no security context and no volume check; the
    builder adds both on purpose. This drops the security contexts, the scratch volume and the
    start script, which `test_each_router_runs_unprivileged_on_a_read_only_root` and
    `test_the_router_pair_matches_the_chart` check instead.
    """
    document = spec_only(document)
    pod = document["spec"].get("template", {}).get("spec", {})
    for constraint in pod.get("topologySpreadConstraints", []):
        constraint["labelSelector"]["matchLabels"].pop("app.kubernetes.io/instance", None)
    if pod:
        pod.pop("securityContext", None)
        pod["volumes"] = [v for v in pod["volumes"] if v["name"] != objects.TMP_VOLUME]
        for container in pod["containers"]:
            container.pop("securityContext", None)
            container["command"][3] = "<router start>"
            container["volumeMounts"] = [
                m for m in container["volumeMounts"] if m["name"] != objects.TMP_VOLUME
            ]
    return document


# The chart's admission policy, mirrored in Python for workloads a requester creates: the CEL
# cannot run here. Each entry is one of the policy's validations, in its order.
POLICY = ROOT / "deploy" / "helm" / "narwhal" / "templates" / "admission-policy.yaml"
CONTROLLER_USER = f"system:serviceaccount:{chart_fixtures.NAMESPACE}:fleet-controller"
POLICY_KINDS = {
    "engine": ["StatefulSet"],
    "router": ["StatefulSet"],
    "gate": ["Job"],
    "haproxy": ["Deployment", "ReplicaSet"],
}
SYSCTLS = {
    "kernel.shm_rmid_forced",
    "net.ipv4.ip_local_port_range",
    "net.ipv4.ip_unprivileged_port_start",
    "net.ipv4.tcp_syncookies",
    "net.ipv4.ping_group_range",
    "net.ipv4.ip_local_reserved_ports",
    "net.ipv4.tcp_keepalive_time",
    "net.ipv4.tcp_fin_timeout",
    "net.ipv4.tcp_keepalive_intvl",
    "net.ipv4.tcp_keepalive_probes",
}
SELINUX_TYPES = {"", "container_t", "container_init_t", "container_kvm_t", "container_engine_t"}
VOLUME_TYPES = (
    "configMap",
    "secret",
    "emptyDir",
    "projected",
    "downwardAPI",
    "persistentVolumeClaim",
)


def policy_refusals(
    document: dict,
    requester: str = CONTROLLER_USER,
    *,
    volumes: fleet_resource.VolumePolicy | None = None,
    runtime_classes: tuple[str, ...] = (),
) -> list[str]:
    """The validations of the admission policy that refuse a workload `requester` creates,
    under the model volume allow-lists `volumes` and the RuntimeClasses `runtime_classes`."""
    allowed = volumes or fleet_resource.VolumePolicy()
    pod = document["spec"]["template"]
    spec = pod["spec"]
    labels = (pod.get("metadata") or {}).get("labels") or {}
    component = (
        labels.get("app.kubernetes.io/component", "")
        if labels.get("app.kubernetes.io/name") == "narwhal"
        else ""
    )
    trusted = document["kind"] if requester == CONTROLLER_USER else ""
    controlled = component in POLICY_KINDS and trusted in POLICY_KINDS[component]
    engine = component == "engine" and controlled
    host_network_gate = (
        component == "gate"
        and controlled
        and labels.get(fleet_resource.GATE_LABEL) in objects.HOST_NETWORK_KINDS
    )
    host_network_allowed = engine or host_network_gate
    containers = [
        *spec["containers"],
        *spec.get("initContainers", []),
        *spec.get("ephemeralContainers", []),
    ]
    checkpoint = engine and any(c["name"] == "checkpoint" for c in spec.get("initContainers", []))
    pod_context = spec.get("securityContext") or {}
    contexts = [pod_context] if "securityContext" in spec else []
    contexts += [c["securityContext"] for c in containers if "securityContext" in c]
    pod_seccomp = (pod_context.get("seccompProfile") or {}).get("type", "")
    volumes = spec.get("volumes", [])
    model = objects.MODEL_VOLUME

    def context(c: dict) -> dict:
        return c.get("securityContext") or {}

    def seccomp(c: dict) -> str:
        found = context(c).get("seccompProfile")
        return found["type"] if found else pod_seccomp

    def adds(c: dict) -> list[str]:
        return (context(c).get("capabilities") or {}).get("add") or []

    annotations = (pod.get("metadata") or {}).get("annotations") or {}
    rules = {
        "host network": not spec.get("hostNetwork") or host_network_allowed,
        "reserved label": component not in POLICY_KINDS or controlled,
        "host namespaces": not spec.get("hostPID") and not spec.get("hostIPC"),
        "privileged": not any(context(c).get("privileged") for c in containers),
        "capabilities": all(
            (a == "IPC_LOCK" and host_network_allowed)
            or (a in ("CHOWN", "FOWNER") and c["name"] == "checkpoint" and checkpoint)
            for c in containers
            for a in adds(c)
        ),
        "seccomp": engine or all(seccomp(c) in ("RuntimeDefault", "Localhost") for c in containers),
        "host ports": all(
            not port.get("hostPort")
            or (spec.get("hostNetwork") and port["hostPort"] == port["containerPort"])
            for c in containers
            for port in c.get("ports", [])
        ),
        "SELinux": all(
            "seLinuxOptions" not in s
            or (
                s["seLinuxOptions"].get("type", "") in SELINUX_TYPES
                and "user" not in s["seLinuxOptions"]
                and "role" not in s["seLinuxOptions"]
            )
            for s in contexts
        ),
        "AppArmor": all(
            (s.get("appArmorProfile") or {}).get("type") != "Unconfined" for s in contexts
        )
        and not any(
            key.startswith("container.apparmor.security.beta.kubernetes.io/")
            and value == "unconfined"
            for key, value in annotations.items()
        ),
        "procMount": all(context(c).get("procMount", "Default") == "Default" for c in containers),
        "Windows host process": not any(
            (s.get("windowsOptions") or {}).get("hostProcess") for s in contexts
        ),
        "sysctls": all(item["name"] in SYSCTLS for item in pod_context.get("sysctls", [])),
        "RuntimeClass": "runtimeClassName" not in spec
        or spec["runtimeClassName"] in runtime_classes,
        "image digest": all(
            "image" in c and re.search(fleet_resource._DIGEST, c["image"]) for c in containers
        ),
        "volume types": all(
            v["name"] == model or any(kind in v for kind in VOLUME_TYPES) for v in volumes
        ),
        "model volume source": all(
            v["name"] != model or any(kind in v for kind in allowed.sources) for v in volumes
        ),
        # The hostPath, nfs and csi allow-lists apply only to a model volume of those sources,
        # as the controller's own check applies them.
        "model hostPath": all(
            v["name"] != model
            or "hostPath" not in v
            or (v["hostPath"].get("type") == "Directory" and model_volume_allowed(v, allowed))
            for v in volumes
        ),
        "model nfs": all(
            v["name"] != model or "nfs" not in v or model_volume_allowed(v, allowed)
            for v in volumes
        ),
        "model csi": all(
            v["name"] != model or "csi" not in v or model_volume_allowed(v, allowed)
            for v in volumes
        ),
        "model read-only": all(
            m["name"] != model or m.get("readOnly")
            for c in containers
            for m in c.get("volumeMounts", [])
        ),
    }
    return [name for name, holds in rules.items() if not holds]


def model_volume_allowed(volume: dict, policy: fleet_resource.VolumePolicy) -> bool:
    """Whether the controller's check admits a pod's model volume under `policy`, its source
    aside."""
    [(kind, settings)] = [(k, v) for k, v in volume.items() if k != "name"]
    source = fleet_resource.VolumePolicy(
        (*policy.sources, kind), policy.host_path_prefixes, policy.nfs_servers, policy.csi_drivers
    )
    try:
        fleet_resource.check_model_volume(
            {"spec": {"engines": {"modelVolume": {kind: settings}}}}, source
        )
    except ValueError:
        return False
    return True


# The golden fleet's model volume and RuntimeClass, as its cluster admin would allow them.
GOLDEN_VOLUMES = fleet_resource.VolumePolicy(
    ("persistentVolumeClaim", "hostPath"), host_path_prefixes=("/models",)
)
GOLDEN_RUNTIME_CLASSES = ("nvidia",)


def workloads(documents: list[dict]) -> list[dict]:
    """The documents the admission policy checks."""
    return [d for d in documents if d["kind"] in ("Job", "StatefulSet", "Deployment")]


class PolicyTests(unittest.TestCase):
    """The chart's admission policy admits every workload the controller builds, and refuses
    each one from another requester."""

    def test_every_workload_the_controller_builds_passes_the_policy(self):
        documents = workloads(everything())
        self.assertEqual(
            {d["kind"] for d in documents}, {"Job", "StatefulSet", "Deployment"}, documents
        )
        for document in documents:
            with self.subTest(document["metadata"]["name"]):
                self.assertEqual(
                    policy_refusals(
                        document,
                        volumes=GOLDEN_VOLUMES,
                        runtime_classes=GOLDEN_RUNTIME_CLASSES,
                    ),
                    [],
                )
                self.assertIn(
                    "reserved label",
                    policy_refusals(
                        document,
                        f"system:serviceaccount:{chart_fixtures.NAMESPACE}:x",
                        volumes=GOLDEN_VOLUMES,
                        runtime_classes=GOLDEN_RUNTIME_CLASSES,
                    ),
                )

    def test_the_model_volume_allow_lists_refuse_what_the_controller_refuses(self):
        [document] = [
            d for d in workloads(everything()) if d["metadata"]["name"] == "discover-node-b"
        ]
        refused = {
            "model volume source": fleet_resource.VolumePolicy(),
            "model hostPath": fleet_resource.VolumePolicy(
                ("persistentVolumeClaim", "hostPath"), host_path_prefixes=("/srv",)
            ),
        }
        for rule, volumes in refused.items():
            with self.subTest(rule):
                self.assertIn(
                    rule,
                    policy_refusals(
                        document, volumes=volumes, runtime_classes=GOLDEN_RUNTIME_CLASSES
                    ),
                )
        self.assertEqual(policy_refusals(document, volumes=GOLDEN_VOLUMES), ["RuntimeClass"])


class RouterTests(unittest.TestCase):
    """The router pair the chart ran on the Lease: its Service and StatefulSet."""

    def test_the_router_pair_matches_the_chart(self):
        built = by_name(objects.routers(router_fleet(), router_settings()))
        rendered = by_name(ROUTER_GOLDEN["rendered"])
        self.assertEqual(sorted(built), [("Service", "routers"), ("StatefulSet", "router")])
        self.assertEqual(sorted(built), sorted(rendered))
        for key, document in rendered.items():
            with self.subTest(kind=key[0], name=key[1]):
                self.assertEqual(without_release(built[key]), without_release(document))
        # The script is the chart's files/router-start.py with the router volume check added.
        [container] = template(rendered[("StatefulSet", "router")])["containers"]
        chart = container["command"][3]
        self.assertIn("import sys\n\nname = ", chart)
        self.assertEqual(
            objects.ROUTER_START,
            chart.replace("import sys\n\n", "import sys\n\n" + objects.ROUTER_VOLUME_CHECK, 1),
        )
        [built_container] = template(router_set())["containers"]
        self.assertEqual(built_container["command"][3], objects.ROUTER_START)

    def test_the_routers_names_agree_with_haproxy_the_target_and_the_chart(self):
        statefulset = router_set()
        names = tuple(f"{statefulset['metadata']['name']}-{n}" for n in range(2))
        self.assertEqual(statefulset["spec"]["replicas"], 2)
        self.assertNotIn("ordinals", statefulset["spec"])
        self.assertEqual(names, objects.ROUTER_PODS)
        self.assertEqual(names, ROUTERS)
        self.assertEqual(objects.ROUTER_HOME, ROUTER_HOME)
        self.assertEqual(statefulset["spec"]["serviceName"], objects.ROUTER_SERVICE)
        config = objects.haproxy_config(chart_fixtures.NAMESPACE, "cluster.example")
        for pod in names:
            self.assertIn(
                f"{pod}.routers.{chart_fixtures.NAMESPACE}.svc.cluster.example:8000", config
            )
        # The chart's Role grants the routers' ServiceAccount the Lease of the same name.
        helpers = (ROOT / "deploy/helm/narwhal/templates/_helpers.tpl").read_text()
        [name] = re.findall(r'define "narwhal.routerName" -}}\n(\S+)\n', helpers)
        self.assertEqual(name, objects.ROUTER_ACCOUNT)
        self.assertEqual(objects.ROUTER_LEASE, objects.ROUTER_ACCOUNT)

    def test_router_0_claims_the_lease_first_until_it_holds_a_handoff(self):
        [container] = template(router_set())["containers"]
        self.assertEqual(container["workingDir"], "/narwhal-router")
        variables = {
            "NARWHAL_POD_IP": POD_IP,
            "NARWHAL_ROUTERS_DOMAIN": container["env"][2]["value"],
        }
        argv = [expand(part, variables) for part in container["command"]]
        self.assertEqual(
            argv[argv.index("--fleet") + 1],
            "/narwhal-state/deploy/run-1/stages/fleet-limits/fleet.json",
        )
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        folder = Path(scratch.name)
        handoff = folder / "router-state.json"
        fleet_file = folder / "fleet.json"
        fleet_file.write_text(json.dumps({"recovery": {"state_path": str(handoff)}}))
        argv[argv.index("--fleet") + 1] = str(fleet_file)
        domain = f"routers.{chart_fixtures.NAMESPACE}.svc.cluster.example:8000"
        first, second = f"http://router-1.{domain}", f"http://router-0.{domain}"
        # Each case: the router, whether its handoff exists, and the standby options it takes.
        cases = (
            ("router-0", False, ["--standby-of", first, "--first-start-primary"]),
            ("router-1", False, ["--standby-of", second]),
            ("router-0", True, ["--standby-of", first]),
            ("router-1", True, ["--standby-of", second]),
        )
        for name, saved, standby in cases:
            with self.subTest(name=name, saved=saved):
                handoff.unlink(missing_ok=True)
                if saved:
                    handoff.write_text("{}")
                executed: list[list[str]] = []
                environment = {**variables, "NARWHAL_POD_NAME": name}
                with (
                    patch.dict(os.environ, environment),
                    patch("os.getcwd", return_value=str(folder)),
                    patch.object(sys, "argv", ["-c", *argv[4:]]),
                    patch.object(sys, "executable", "python3"),
                    patch("os.execv", lambda path, args, found=executed: found.append(args)),
                ):
                    exec(argv[3], {"__name__": "__main__"})
                [args] = executed
                self.assertEqual(args[:5], ["python3", "-I", "-m", "narwhal.cli", "--fleet"])
                tail = ["--router-id", name, *standby]
                self.assertEqual(args[-len(tail) :], tail)
                self.assertEqual(args[args.index("--kubernetes-lease") + 1], "narwhal-router")
                self.assertNotIn("--lease-path", args)
                self.assertEqual(args[args.index("--host") + 1], POD_IP)
                self.assertEqual(args[args.index("--lease-ttl") + 1], "6")

    def test_only_the_router_container_holds_the_lease_token(self):
        spec = template(router_set())
        self.assertEqual(spec["serviceAccountName"], "narwhal-router")
        self.assertFalse(spec["automountServiceAccountToken"])
        [api] = [volume for volume in spec["volumes"] if volume["name"] == "narwhal-api"]
        sources = api["projected"]["sources"]
        self.assertEqual(
            sources[0], {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}
        )
        self.assertEqual(sources[1]["configMap"]["name"], "kube-root-ca.crt")
        self.assertEqual(
            sources[2]["downwardAPI"]["items"][0]["fieldRef"]["fieldPath"], "metadata.namespace"
        )
        self.assertNotIn("initContainers", spec)
        [container] = spec["containers"]
        [mount] = [m for m in container["volumeMounts"] if m["name"] == "narwhal-api"]
        self.assertEqual(mount["mountPath"], "/var/run/secrets/kubernetes.io/serviceaccount")
        self.assertTrue(mount["readOnly"])
        claims = [v for v in spec["volumes"] if "persistentVolumeClaim" in v]
        self.assertEqual(
            [v["persistentVolumeClaim"] for v in claims],
            [{"claimName": "narwhal-state", "readOnly": True}],
        )
        state = [m for m in container["volumeMounts"] if m["name"] == "narwhal-state"]
        self.assertEqual(
            state, [{"name": "narwhal-state", "mountPath": "/narwhal-state", "readOnly": True}]
        )
        # No other derived object runs as the routers' ServiceAccount.
        for document in everything():
            if document["kind"] in ("StatefulSet", "Deployment", "Job"):
                with self.subTest(kind=document["kind"], name=document["metadata"]["name"]):
                    account = template(document).get("serviceAccountName")
                    self.assertEqual(
                        account == "narwhal-router", document["metadata"]["name"] == "router"
                    )

    def test_each_router_runs_unprivileged_on_a_read_only_root(self):
        spec = template(router_set())
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
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            with self.subTest(container["name"]):
                self.assertEqual(
                    container["securityContext"],
                    {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                )
        custom = template(router_set(router_fleet(run_as_user=2000, run_as_group=3000)))
        self.assertEqual(
            {k: custom["securityContext"][k] for k in ("runAsUser", "runAsGroup", "fsGroup")},
            {"runAsUser": 2000, "runAsGroup": 3000, "fsGroup": 3000},
        )
        for value in (0, -1, 2**31, True, "10001"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "non-root"):
                router_fleet(run_as_user=value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "non-root"):
                router_fleet(run_as_group=value)

    def test_the_router_writes_only_its_own_volume_and_scratch(self):
        spec = template(router_set())
        [container] = spec["containers"]
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        writable = {}
        for mount in container["volumeMounts"]:
            if not mount.get("readOnly"):
                writable[mount["mountPath"]] = mount["name"]
        self.assertEqual(writable, {"/narwhal-router": "router-state", "/tmp": "tmp"})
        self.assertEqual(volumes["tmp"], {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}})
        [claim] = router_set()["spec"]["volumeClaimTemplates"]
        self.assertEqual(claim["metadata"]["name"], "router-state")
        # What the router writes: its journal, its handoff state with the temporary file
        # written beside it, and any relative path from its working directory.
        argv = container["command"]
        work = Path(container["workingDir"])
        # fleet-limits sets the handoff state path under the target's ROUTER_HOME.
        handoff = Path(ROUTER_HOME) / "router-state.json"
        written = [
            Path(argv[argv.index("--journal") + 1]),
            handoff,
            handoff.with_name(f".{handoff.name}.1.0.tmp"),
            work / "runs" / "state.json",
        ]
        for path in written:
            with self.subTest(path=str(path)):
                self.assertTrue(any(path.is_relative_to(mount) for mount in writable), path)

    def test_the_router_pair_passes_the_charts_admission_policy(self):
        validations = POLICY.read_text().split("  validations:\n", 1)[1].split("\n---", 1)[0]
        # The mirror holds every validation of the policy.
        self.assertEqual(validations.count("\n    - expression:") + 1, 20)
        statefulset = router_set()
        self.assertEqual(policy_refusals(statefulset), [])
        # The router labels are reserved: only the controller may apply them.
        self.assertEqual(
            policy_refusals(statefulset, f"system:serviceaccount:{chart_fixtures.NAMESPACE}:x"),
            ["reserved label"],
        )
        # Each rule the routers keep to refuses them once broken.
        broken = {
            "seccomp": lambda pod, c: pod["securityContext"].update(
                seccompProfile={"type": "Unconfined"}
            ),
            "image digest": lambda pod, c: c.update(image="registry.example/narwhal:latest"),
            "capabilities": lambda pod, c: c["securityContext"]["capabilities"].update(
                add=["NET_ADMIN"]
            ),
            "host network": lambda pod, c: pod.update(hostNetwork=True),
            "privileged": lambda pod, c: c["securityContext"].update(privileged=True),
            "volume types": lambda pod, c: pod["volumes"].append(
                {"name": "host", "hostPath": {"path": "/"}}
            ),
        }
        for rule, breaks in broken.items():
            with self.subTest(rule):
                document = copy.deepcopy(statefulset)
                pod = template(document)
                breaks(pod, pod["containers"][0])
                self.assertIn(rule, policy_refusals(document))
        # The router pod runs the RuntimeDefault profile and adds no capability.
        pod = template(statefulset)
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertNotIn("add", pod["containers"][0]["securityContext"]["capabilities"])
        self.assertTrue(re.search(fleet_resource._DIGEST, pod["containers"][0]["image"]))

    def test_the_engines_may_not_run_as_the_routers_service_account(self):
        with self.assertRaisesRegex(ValueError, "may not be narwhal-router"):
            fleet(engine_account="narwhal-router")

    def test_the_routers_never_share_a_node_while_another_can_take_one(self):
        statefulset = router_set()
        [constraint] = template(statefulset)["topologySpreadConstraints"]
        self.assertEqual(
            constraint,
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "DoNotSchedule",
                # A tainted node the routers cannot run on is not a node to spread over.
                "nodeTaintsPolicy": "Honor",
                "labelSelector": {"matchLabels": statefulset["spec"]["selector"]["matchLabels"]},
            },
        )
        self.assertEqual(
            statefulset["spec"]["selector"]["matchLabels"],
            {"app.kubernetes.io/name": "narwhal", "app.kubernetes.io/component": "router"},
        )

    def test_only_the_router_container_gets_the_engine_key(self):
        [container] = template(router_set())["containers"]
        self.assertEqual(
            container["env"][-1],
            {
                "name": "NARWHAL_ENGINE_API_KEY",
                "valueFrom": {"secretKeyRef": {"name": "engine-credential", "key": "api-key"}},
            },
        )
        [bare] = template(router_set(router_fleet(api_key=None)))["containers"]
        self.assertNotIn("secretKeyRef", json.dumps(bare))
        self.assertNotIn(
            SECRET_VALUE, json.dumps(objects.routers(router_fleet(), router_settings()))
        )

    def test_a_fleets_router_option_reaches_narwhal_serve_as_written(self):
        written = ("--log-level", "$(NARWHAL_ENGINE_API_KEY)", "$$x")
        [container] = template(router_set(settings=router_settings(args=written)))["containers"]
        variables = {"NARWHAL_ENGINE_API_KEY": SECRET_VALUE, "NARWHAL_POD_IP": POD_IP}
        argv = [expand(part, variables) for part in container["command"]]
        self.assertEqual(tuple(argv[-3:]), written)
        self.assertNotIn(SECRET_VALUE, argv)

    def test_router_settings_come_from_the_fleet_spec(self):
        spec = {"storageClassName": "local-path", "size": "2Gi", "args": ["--lease-ttl", "6"]}
        self.assertEqual(
            Routers.of(spec, "deploy/run-1/fleet.json"),
            Routers("deploy/run-1/fleet.json", "local-path", "2Gi", ("--lease-ttl", "6")),
        )
        self.assertEqual(Routers.of({"storageClassName": "a", "size": "1Gi"}, "f.json").args, ())

    def test_router_settings_that_cannot_run_the_pair_are_refused(self):
        for fleet_file in (
            "../fleet.json",
            "/fleet.json",
            "a/../../fleet.json",
            "fleet.json\nx",
            ".hidden/fleet.json",
            "fleet.yaml",
            "a//fleet.json",
        ):
            with (
                self.subTest(fleet_file=fleet_file),
                self.assertRaisesRegex(ValueError, "fleet file"),
            ):
                router_settings(fleet_file=fleet_file)
        with self.assertRaisesRegex(ValueError, "storage class"):
            router_settings(storage_class="Local_Path")
        with self.assertRaisesRegex(ValueError, "quantity"):
            router_settings(size="2 GiB")
        with self.assertRaisesRegex(ValueError, "at most 32"):
            router_settings(args=("--log-level", "info") * 17)
        with self.assertRaisesRegex(ValueError, "at most 32"):
            router_settings(args=["--log-level", "info"])
        for argument in ("x\ny", "\x1b[2J", "a" * 257):
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, "printable"):
                router_settings(args=(argument,))
        # An option of the pair's own, whole, with its value, or as a prefix argparse expands.
        for argument, reserved in (
            ("--kubernetes-lease", "--kubernetes-lease"),
            ("--kubernetes-lease=other", "--kubernetes-lease"),
            ("--kube", "--kubernetes-lease"),
            ("--lease-path", "--lease-path"),
            ("--lease-witness=/w", "--lease-witness"),
            ("--standby-of", "--standby-of"),
            ("--first-start-primary", "--first-start-primary"),
            ("--router-id", "--router-id"),
            ("--fleet=/etc/fleet.json", "--fleet"),
            ("--journal", "--journal"),
            ("--host", "--host"),
            ("--port", "--port"),
        ):
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, reserved):
                router_settings(args=(argument, "x"))
        # Whatever ends option parsing, exits, or stops the router at start.
        for arguments in (
            ("--",),
            ("--lease-ttl", "6", "--", "-x"),
            ("--help",),
            ("--he",),
            ("-h",),
            ("--version",),
            ("--vers",),
            ("--log",),
            ("--lease-tl", "6"),
            ("-x",),
            ("--log-level", "-v"),
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(ValueError, "no option the pair takes|the pair's own"),
            ):
                router_settings(args=arguments)
        router_settings(
            args=(
                "--lease-ttl",
                "6",
                "--lease-safety-margin=1",
                "--standby-max-handoff-age",
                "30",
                "--resume",
                "--shutdown-drain",
                "-0.5",
            )
        )

    def test_the_options_the_pair_takes_or_sets_are_every_narwhal_serve_option(self):
        from contextlib import redirect_stdout
        from io import StringIO

        from narwhal.cli import serve

        output = StringIO()
        # Wide enough that no option name wraps.
        with (
            patch.dict(os.environ, {"COLUMNS": "10000"}),
            redirect_stdout(output),
            self.assertRaises(SystemExit),
        ):
            serve(["--help"])
        offered = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*[a-z])", output.getvalue()))
        self.assertEqual(
            offered,
            {*objects.ROUTER_OPTIONS, *objects.ROUTER_SERVE_OPTIONS, "--help", "--version"},
        )
        self.assertFalse(set(objects.ROUTER_OPTIONS) & set(objects.ROUTER_SERVE_OPTIONS))

    def test_a_router_whose_volume_it_cannot_write_stops_with_the_fix(self):
        [container] = template(router_set())["containers"]
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        missing = Path(scratch.name) / "router-state"
        with (
            patch("os.getcwd", return_value=str(missing)),
            patch.dict(os.environ, {"NARWHAL_POD_NAME": "router-0"}),
            patch("os.execv") as execv,
            self.assertRaises(SystemExit) as stopped,
        ):
            exec(container["command"][3], {"__name__": "__main__"})
        execv.assert_not_called()
        message = str(stopped.exception.code)
        self.assertIn(f"the router volume {missing} is not writable by UID {os.getuid()}", message)
        self.assertIn("Delete this router's router-state PVC", message)
        if os.getuid() != 0:
            locked = Path(scratch.name) / "locked"
            locked.mkdir(mode=0o555)
            self.addCleanup(locked.chmod, 0o755)
            with (
                patch("os.getcwd", return_value=str(locked)),
                patch("os.execv") as execv,
                self.assertRaises(SystemExit),
            ):
                exec(container["command"][3], {"__name__": "__main__"})
            execv.assert_not_called()
            self.assertEqual(list(locked.iterdir()), [])

    def test_the_image_user_is_the_uid_the_routers_run_as(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("groupadd --system --gid 10001 narwhal", dockerfile)
        self.assertIn("useradd --system --uid 10001 --gid 10001 --create-home narwhal", dockerfile)
        # A numeric user lets the kubelet check runAsNonRoot without the pod's runAsUser.
        self.assertIn("\nUSER 10001:10001\n", dockerfile)
        defaults = fleet()
        self.assertEqual((defaults.run_as_user, defaults.run_as_group), (10001, 10001))

    def test_stopping_deletes_the_routers_and_keeps_their_volumes(self):
        live = [
            named(d["kind"], d["metadata"]["name"])
            for d in objects.routers(router_fleet(), router_settings())
        ]
        live.append(named("PersistentVolumeClaim", "router-state-router-0", owner=None))
        deletions = objects.prune(OWNER, [], live)
        self.assertEqual(
            [(d.kind, d.name) for d in deletions],
            [("StatefulSet", "router"), ("Service", "routers")],
        )
        for deletion in deletions:
            self.assertEqual(deletion.options()["propagationPolicy"], "Background")


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
