"""The NarwhalFleet resource: names derived from one API group, a schema whose every list,
string and map is bounded, the CRD rendered from that schema, and the variables a fleet gives
`kubernetes_target.inputs()`."""

import copy
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

from narwhal.deployment import fleet_resource, stages
from narwhal.deployment.fleet_resource import (
    GROUP,
    LAUNCH_REFUSED,
    SCHEMA,
    STAGES,
    spec_env,
)
from narwhal.deployment.launch_engine.kubernetes import MODEL_VOLUME_SOURCES
from tests.deployment import chart_schema, fleet_fixtures, kubernetes_cluster
from tools.maintenance import crd_schema

SOURCE = Path(fleet_resource.__file__)
# The variables a full fleet gives that today's Kubernetes environment does not hold, with the
# fact each one belongs to. Step E classifies them so in `inputs()`.
NEW_VARIABLES = {
    "NARWHAL_KUBERNETES_FLEET_UID": "cluster",
    "NARWHAL_KUBERNETES_API_KEY_SECRET": "credential",
    "NARWHAL_KUBERNETES_CONTROL_TOKEN_SECRET": "control_credential",
    "NARWHAL_KUBERNETES_ROUTERS": "router",
    "NARWHAL_KUBERNETES_HAPROXY": "router",
    "NARWHAL_KUBERNETES_SIDECAR_IMAGE": "discovery",
    "NARWHAL_KUBERNETES_HAPROXY_IMAGE": "discovery",
    "NARWHAL_KUBERNETES_FABRIC_IMAGE": "discovery",
    "NARWHAL_KUBERNETES_RDMA_RESOURCE": "discovery",
    "NARWHAL_KUBERNETES_RUNTIME_CLASS": "discovery",
    "NARWHAL_ENGINE_PORT": "discovery",
    "NARWHAL_ATTEST_PORT": "discovery",
    "NARWHAL_MODEL_PATH": "discovery",
    "NARWHAL_MODEL_SHA256": "discovery",
    "NARWHAL_ENGINE_BACKEND": "discovery",
    "NARWHAL_NODE_1_FABRIC_INTERFACE": "discovery",
    "NARWHAL_NODE_2_FABRIC_INTERFACE": "discovery",
    "NARWHAL_NODE_2_RDMA_GID_INDEX": "fabric_test",
    "NARWHAL_NODE_2_RDMA_ROCE_MODE": "fabric_test",
    "NARWHAL_PROFILE_DECODE_CONCURRENCY": "profiling",
    "NARWHAL_STAGE_TIMEOUT_SECONDS": None,
    "NARWHAL_STAGE_PROFILE_TIMEOUT_SECONDS": None,
}
# Credential names with and without a prefix, plural, and as files.
CREDENTIALS = (
    "NARWHAL_TOKEN",
    "NARWHAL_HF_TOKEN",
    "NARWHAL_HF_TOKEN_FILE",
    "NARWHAL_NODE_1_TOKEN",
    "NARWHAL_TOKENS",
    "NARWHAL_SECRET",
    "NARWHAL_SECRET_FILE",
    "NARWHAL_CLIENT_SECRETS",
    "NARWHAL_APIKEY",
    "NARWHAL_OPENAI_APIKEY_FILE",
    "NARWHAL_API_KEYS",
    "NARWHAL_KEY",
    "NARWHAL_KEY_FILE",
    "NARWHAL_CREDENTIALS",
    "NARWHAL_CREDENTIALS_FILE",
    "NARWHAL_AWS_CREDENTIAL_FILE",
    "NARWHAL_CREDS",
    "NARWHAL_DB_PASSWORD_FILE",
    "NARWHAL_PASSWD",
    "NARWHAL_ROUTER_TOKEN",
    "NARWHAL_CONTROL_TOKEN",
)
CLAIM = "narwhal-state"
# The narwhal chart's default: a claim and nothing else.
POLICY = fleet_resource.VolumePolicy()


def nodes(node, path=""):
    """Every schema node under `node`, with its path."""
    yield path, node
    for name, child in node.get("properties", {}).items():
        yield from nodes(child, f"{path}.{name}")
    if isinstance(node.get("items"), dict):
        yield from nodes(node["items"], f"{path}[]")
    if isinstance(node.get("additionalProperties"), dict):
        yield from nodes(node["additionalProperties"], f"{path}{{}}")


def go_match(pattern, value):
    """Whether `value` matches `pattern` as the API server's Go regexp matches it: there `$`
    ends the text, where Python's also matches before a final newline."""
    if pattern.endswith("$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, value) is not None


def schema_path(field):
    """A field as the API server names it, as a path of `at`: `a.b[1].c` is `a.b.[].c`."""
    return re.sub(r"\[[0-9]+\]", ".[]", field)


def value_at(document, field):
    value = document
    for name in re.findall(r"[^.\[\]]+|\[[0-9]+\]", field):
        value = value[int(name[1:-1])] if name.startswith("[") else value[name]
    return value if field else document["metadata"]["name"]


def strings(value, path):
    """Every string in a document, with the field path the API server would name."""
    if isinstance(value, dict):
        for name, item in value.items():
            yield from strings(item, f"{path}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def rules(node):
    return [item["rule"] for item in node.get("x-kubernetes-validations", [])]


def at(path):
    node = SCHEMA
    for name in path.split(".") if path else ():
        if name == "[]":
            node = node["items"]
        elif "properties" in node:
            node = node["properties"][name]
        else:
            node = node["additionalProperties"]
    return node


class Names(unittest.TestCase):
    def test_every_name_derives_from_the_group(self):
        for name in (
            fleet_resource.PLACEMENT_GATE,
            fleet_resource.RUN_LABEL,
            fleet_resource.GATE_LABEL,
            fleet_resource.CONFIG_SHA256,
            fleet_resource.SCHEMA_SHA256,
            fleet_resource.REQUESTED_AT,
            fleet_resource.stage_condition("profile"),
        ):
            with self.subTest(name):
                self.assertTrue(name.endswith(f"{GROUP}/{name.rpartition('/')[2]}"))
        self.assertEqual(fleet_resource.CONTROLLER_LEASE, f"controller.{GROUP}")
        self.assertEqual(fleet_resource.CRD_NAME, f"narwhalfleets.{GROUP}")
        self.assertEqual(fleet_resource.API_VERSION, f"{GROUP}/v1alpha1")

    def test_the_group_is_written_once_and_the_old_one_is_gone(self):
        text = SOURCE.read_text()
        self.assertEqual(text.count(GROUP), 1)
        self.assertNotIn("narwhal.io", text)
        self.assertTrue(GROUP.endswith(".example.com"))

    def test_each_stage_has_a_condition_the_status_can_hold(self):
        pattern = at("status.conditions.[].type")["pattern"]
        for stage in STAGES:
            with self.subTest(stage):
                self.assertRegex(fleet_resource.stage_condition(stage), pattern)
                self.assertLessEqual(len(fleet_resource.stage_condition(stage)), 316)
        for condition in fleet_resource.CONDITIONS:
            self.assertRegex(condition, pattern)
        self.assertEqual(
            at("status.conditions")["maxItems"], len(fleet_resource.CONDITIONS) + len(STAGES)
        )
        self.assertEqual(at("status.conditions")["x-kubernetes-list-map-keys"], ["type"])
        with self.assertRaisesRegex(ValueError, "not a stage"):
            fleet_resource.stage_condition("helm-upgrade")

    def test_stage_names_follow_the_graph(self):
        self.assertEqual(at("spec.until")["enum"], list(STAGES))
        self.assertEqual(list(at("spec.timeouts.stages")["properties"]), list(STAGES))
        self.assertEqual(list(at("status.stages")["properties"]), list(STAGES))
        self.assertEqual(
            fleet_resource.stage_timeout_env("engine-prepare"),
            "NARWHAL_STAGE_ENGINE_PREPARE_TIMEOUT_SECONDS",
        )
        self.assertIn(
            '"NARWHAL_STAGE_" + re.sub(r"[^A-Z0-9]", "_", stage.upper()) + "_TIMEOUT_SECONDS"',
            Path(stages.__file__).read_text(),
        )


class Bounds(unittest.TestCase):
    """Unbounded input would let a fleet grow without limit and push a CEL rule over the API
    server's cost budget, which refuses the CRD."""

    def test_every_list_string_and_map_is_bounded(self):
        for path, node in nodes(SCHEMA):
            with self.subTest(path or "/"):
                if node.get("type") == "array":
                    self.assertIn("maxItems", node)
                    self.assertIn(node.get("x-kubernetes-list-type"), ("atomic", "map"))
                if node.get("type") == "string":
                    self.assertIn("maxLength", node)
                if "additionalProperties" in node:
                    self.assertIn("maxProperties", node)
                if node.get("type") == "integer":
                    self.assertTrue("maximum" in node or node.get("format") == "int64")

    def test_a_check_finds_a_missing_bound(self):
        broken = copy.deepcopy(SCHEMA)
        del broken["properties"]["spec"]["properties"]["engines"]["properties"]["nodes"]["maxItems"]
        unbounded = [
            path
            for path, node in nodes(broken)
            if node.get("type") == "array" and "maxItems" not in node
        ]
        self.assertEqual(unbounded, [".spec.engines.nodes"])

    def test_every_rule_reads_only_bounded_values(self):
        for path, node in nodes(SCHEMA):
            for rule in rules(node):
                with self.subTest(path=path, rule=rule):
                    if node.get("type") == "string":
                        self.assertIn("maxLength", node)
                    if ".all(" in rule or ".filter(" in rule:
                        self.assertTrue(
                            "maxProperties" in node or "maxItems" in node or "[" in rule
                        )


class Rules(unittest.TestCase):
    def test_a_namespace_holds_one_fleet_named_fleet(self):
        self.assertIn("self.metadata.name == 'fleet'", rules(SCHEMA))
        with self.assertRaisesRegex(ValueError, "named fleet"):
            spec_env(
                fleet_fixtures.changed(lambda d: d["metadata"].update(name="x")),
                state_claim=CLAIM,
                policy=POLICY,
            )

    def test_the_model_volume_takes_exactly_one_source_read_only(self):
        volume = at("spec.engines.modelVolume")
        self.assertEqual(list(volume["properties"]), list(MODEL_VOLUME_SOURCES))
        [rule] = rules(volume)
        for source in MODEL_VOLUME_SOURCES:
            self.assertIn(f"has(self.{source})", rule)
        self.assertTrue(rule.endswith(".size() == 1"))
        for source in ("persistentVolumeClaim", "nfs", "csi"):
            read_only = volume["properties"][source]["properties"]["readOnly"]
            self.assertEqual(rules(read_only), ["self"])
            self.assertIs(read_only["default"], True)
        self.assertNotIn("readOnly", volume["properties"]["hostPath"]["properties"])

    def test_no_source_allows_unchecked_fields(self):
        for path, node in nodes(SCHEMA):
            self.assertNotIn("x-kubernetes-preserve-unknown-fields", node, path)

    def test_launch_refuses_every_variable_the_fleet_sets(self):
        env = spec_env(fleet_fixtures.fleet(), state_claim=CLAIM, policy=POLICY)
        launch = {item["name"] for item in fleet_fixtures.fleet()["spec"]["engines"]["launch"]}
        for name in set(env) - launch:
            with self.subTest(name):
                self.assertRegex(name, LAUNCH_REFUSED)

    def test_launch_refuses_credentials_and_runner_variables(self):
        for name in (
            *CREDENTIALS,
            "NARWHAL_ENGINE_API_KEY",
            "NARWHAL_SGLANG_API_KEY",
            "NARWHAL_SSH_PASSWORD",
            "NARWHAL_HOSTS",
            "NARWHAL_STAGE_TIMEOUT_SECONDS",
            "NARWHAL_TRIAL_RATE",
            "NARWHAL_DEPLOYMENT_REVISION",
            "NARWHAL_NODE_2_IP",
            "NARWHAL_NODE_3_RDMA_GID_INDEX",
            "NARWHAL_KUBERNETES_SERVICE_ACCOUNT",
            "NARWHAL_POD_UID",
        ):
            with self.subTest(name):
                self.assertRegex(name, LAUNCH_REFUSED)
                with self.assertRaisesRegex(ValueError, name):
                    spec_env(
                        fleet_fixtures.changed(
                            lambda d, n=name: d["spec"]["engines"]["launch"].append(
                                {"name": n, "value": "x"}
                            )
                        ),
                        state_claim=CLAIM,
                        policy=POLICY,
                    )

    def test_launch_takes_backend_inputs(self):
        for name in (
            "NARWHAL_ENGINE_BACKEND",
            "NARWHAL_ENGINE_CONNECTOR",
            "NARWHAL_ENGINE_ENV",
            "NARWHAL_TRANSFER_NET_DEVICES",
            "NARWHAL_NODE_2_TRANSFER_NET_DEVICES",
            "NARWHAL_MAX_SEQUENCES",
            "NARWHAL_TOKENIZER_READY",
        ):
            with self.subTest(name):
                self.assertNotRegex(name, LAUNCH_REFUSED)

    def test_cel_patterns_are_raw_strings_that_python_compiles(self):
        for path, node in nodes(SCHEMA):
            for rule in rules(node):
                for pattern in re.findall(r"matches\(r'([^']*)'\)", rule):
                    with self.subTest(path=path, pattern=pattern):
                        re.compile(pattern)
                self.assertNotIn("matches('", rule)

    def test_path_rules_refuse_dot_segments(self):
        [dots, _] = rules(at("spec.engines.modelVolume.hostPath.path"))
        pattern = re.search(r"r'([^']*)'", dots)[1]
        for path in ("/a/../b", "/a/./b", "/..", "../a", "a/.."):
            self.assertRegex(path, pattern)
        for path in ("/models", "/a/..b/c", "/a/b..", "model.gguf"):
            self.assertNotRegex(path, pattern)

    def test_reserved_resource_domains_are_refused(self):
        [rule] = rules(at("spec.engines.resources.gpu"))
        pattern = re.search(r"r'([^']*)'", rule)[1]
        for name in ("kubernetes.io/gpu", "nvidia.k8s.io/gpu", "a.kubernetes.io/x"):
            self.assertRegex(name, pattern)
        for name in ("gpu.example.com/gpu", "nvidia.com/gpu", "notkubernetes.io/gpu"):
            self.assertNotRegex(name, pattern)

    def test_each_refused_case_meets_the_check_its_field_holds(self):
        """Each case names its field and check; the check exists there, and where Python can
        run it, it refuses the case's value and accepts the fixture's."""
        for case, refusal in fleet_fixtures.REFUSED.items():
            with self.subTest(case):
                node = at(schema_path(refusal.field))
                bad = value_at(fleet_fixtures.changed(refusal.change), refusal.field)
                if refusal.check == "rule":
                    [rule] = [
                        item["rule"]
                        for item in node.get("x-kubernetes-validations", [])
                        if item["message"] == refusal.message
                    ]
                    if match := re.fullmatch(r"!self\.matches\(r'([^']*)'\)", rule):
                        self.assertRegex(bad, match[1])
                elif refusal.check == "pattern":
                    self.assertFalse(go_match(node["pattern"], bad))
                else:
                    self.assertNotIn(bad, node["enum"])

    def test_the_host_path_rule_and_the_controller_share_one_expression(self):
        [_, system] = rules(at("spec.engines.modelVolume.hostPath.path"))
        self.assertEqual(system, f"!self.matches(r'{fleet_resource.HOST_PATH_REFUSED}')")
        node = at("spec.engines.modelVolume.hostPath")
        self.assertEqual(node["properties"]["type"]["enum"], ["Directory"])
        self.assertIn("type", node["required"])

    def test_host_paths_are_matched_by_segment(self):
        for path in (
            "/proc",
            "/proc/1/root",
            "/sys/fs",
            "/dev",
            "/run/containerd/containerd.sock",
            "/var/run/docker.sock",
            "/etc",
            "/etc/kubernetes/pki",
            "/var/lib/kubelet/pods",
            "/var/lib/containerd",
            "/var/lib/docker",
            "/var/lib/containers/storage",
            "/var/lib/rancher/k3s",
            "/var/lib/etcd/member",
            "/var/lib/k0s",
            "/var/lib/cni/networks",
            "/var/log",
            "/var/snap/microk8s",
            "/root",
            "/home/user/.ssh",
            "/opt/cni/bin",
            "/host",
            "/boot",
            "/var",
            "/var/lib",
            "/opt",
        ):
            self.assertRegex(path, fleet_resource.HOST_PATH_REFUSED)
        for path in (
            "/process",
            "/etcd/models",
            "/devices",
            "/var/lib/kubeletx",
            "/srv/models",
            "/homes",
            "/opt/models",
            "/hosted",
            "/var/lib/models",
        ):
            self.assertNotRegex(path, fleet_resource.HOST_PATH_REFUSED)

    def test_every_string_that_reaches_a_variable_refuses_control_characters(self):
        documents = [fleet_fixtures.full_fleet()] + [
            fleet_fixtures.changed(
                lambda d, v=volume: d["spec"]["engines"].update(modelVolume=copy.deepcopy(v))
            )
            for volume in fleet_fixtures.VOLUMES
        ]
        checked = set()
        for document in documents:
            for path, value in strings(document["spec"], "spec"):
                node = at(schema_path(path))
                checked.add(re.sub(r"\[[0-9]+\]", "[]", path))
                with self.subTest(path):
                    if "enum" in node:
                        continue
                    self.assertIn("pattern", node)
                    self.assertTrue(go_match(node["pattern"], value))
                    for control in ("\x00", "\x01", "\n", "\x1b", "\x7f", "\x85"):
                        for spoiled in (
                            value + control,
                            control + value,
                            value[:1] + control + value[1:],
                        ):
                            self.assertFalse(go_match(node["pattern"], spoiled), repr(spoiled))
        unchecked = (
            {
                path
                for path, node in nodes(SCHEMA["properties"]["spec"], "spec")
                if node.get("type") == "string" and "enum" not in node
            }
            - checked
            - {"spec.engines.modelVolume.csi.volumeAttributes{}"}
        )
        self.assertEqual(unchecked, set())

    def test_a_control_character_never_reaches_a_variable(self):
        document = fleet_fixtures.changed(
            lambda d: d["spec"]["engines"]["launch"].append(
                {"name": "NARWHAL_ENGINE_ENV", "value": "{}\n"}
            )
        )
        with self.assertRaisesRegex(ValueError, "control character reaches NARWHAL_ENGINE_ENV"):
            spec_env(document, state_claim=CLAIM, policy=POLICY)


class Values(unittest.TestCase):
    """The schema without its CEL rules, as Helm checks a chart's `fleet` value."""

    schema = crd_schema.values_schema()

    def errors(self, document):
        return chart_schema.errors(json.loads(json.dumps(document["spec"])), self.schema)

    def test_the_fixture_fleet_is_valid(self):
        self.assertEqual(self.errors(fleet_fixtures.fleet()), [])

    def test_the_schema_refuses_malformed_and_unbounded_fields(self):
        cases = {
            "nodes": lambda d: d["spec"]["engines"].update(
                nodes=[{"node": f"n{i}", "fabricInterface": "f"} for i in range(65)]
            ),
            "one node": lambda d: d["spec"]["engines"]["nodes"].pop(),
            "image": lambda d: d["spec"]["engines"].update(image="registry.example/engine:latest"),
            "name": lambda d: d["spec"]["engines"]["model"].update(name="m" * 257),
            "launch name": lambda d: d["spec"]["engines"]["launch"].append(
                {"name": "PATH", "value": "/"}
            ),
            "unknown": lambda d: d["spec"]["engines"].update(serviceAccount="x"),
            "decimal": lambda d: d["spec"]["slo"].update(tpotS="1e-1"),
            "until": lambda d: d["spec"].update(until="helm-upgrade"),
            "retain": lambda d: d["spec"].update(retainRuns=17),
            "port": lambda d: d["spec"]["fabric"].update(testPort=70000),
        }
        for case, change in cases.items():
            with self.subTest(case):
                self.assertNotEqual(self.errors(fleet_fixtures.changed(change)), [])

    def test_the_values_schema_has_no_kubernetes_extensions(self):
        text = json.dumps(self.schema)
        self.assertNotIn("x-kubernetes", text)
        for path, node in nodes(self.schema):
            if "properties" in node:
                self.assertIs(node["additionalProperties"], False, path)


class Variables(unittest.TestCase):
    def test_a_fleet_gives_the_variables_the_target_reads_today_and_no_others(self):
        env = spec_env(fleet_fixtures.full_fleet(), state_claim=CLAIM, policy=POLICY)
        with tempfile.TemporaryDirectory() as root:
            today = kubernetes_cluster.environment(Path(root))
        # The fleet forces the model volume read-only.
        volume = json.loads(today["NARWHAL_KUBERNETES_MODEL_VOLUME"])
        volume["persistentVolumeClaim"]["readOnly"] = True
        today["NARWHAL_KUBERNETES_MODEL_VOLUME"] = json.dumps(volume, sort_keys=True)
        for name in sorted(set(today) & set(env)):
            with self.subTest(name):
                self.assertEqual(env[name], today[name])
        # The spec replaces the values file and the chart directory, and names each engine's
        # fabric interface.
        self.assertEqual(
            set(today) - set(env),
            {"NARWHAL_KUBERNETES_VALUES", "NARWHAL_KUBERNETES_CHART", "NARWHAL_FABRIC_INTERFACE"},
        )
        self.assertEqual(
            {name: fleet_resource.variable_fact(name) for name in set(env) - set(today)},
            NEW_VARIABLES,
        )
        for n in (1, 2):
            self.assertEqual(
                env[f"NARWHAL_NODE_{n}_FABRIC_INTERFACE"], today["NARWHAL_FABRIC_INTERFACE"]
            )

    def test_each_variable_has_the_fact_the_design_gives_it(self):
        env = spec_env(fleet_fixtures.full_fleet(), state_claim=CLAIM, policy=POLICY)
        facts = {name: fleet_resource.variable_fact(name) for name in env}
        expected = {
            "NARWHAL_KUBERNETES_NAMESPACE": "cluster",
            "NARWHAL_ENGINE_IMAGE": "discovery",
            "NARWHAL_KUBERNETES_MODEL_VOLUME": "discovery",
            "NARWHAL_NODE_1_KUBERNETES_NODE": "discovery",
            "NARWHAL_NODE_1_URL": "discovery",
            "NARWHAL_TTFT_S": "slo",
            "NARWHAL_ROUTER_OVERHEAD_S": "slo",
            "NARWHAL_FIRST_TOKEN_MARGIN_S": "deadline_margin",
            "NARWHAL_PROFILE_DECODE_CONCURRENCY": "profiling",
            "NARWHAL_CALIBRATION_SAMPLES": "calibration",
            "NARWHAL_FABRIC_HEADROOM": "fabric_workload",
            "NARWHAL_FABRIC_TEST_PORT": "fabric_test",
            **NEW_VARIABLES,
        }
        for name, fact in expected.items():
            with self.subTest(name):
                self.assertEqual(facts[name], fact)
        with self.assertRaisesRegex(ValueError, "no variable of a fleet"):
            fleet_resource.variable_fact("PATH")

    def test_each_engine_takes_its_node_urls_and_rdma_port(self):
        document = fleet_fixtures.changed(
            lambda d: d["spec"]["engines"]["nodes"][1].update(
                rdma={"gidIndex": 3, "roceMode": "v2"}
            )
        )
        env = spec_env(document, state_claim=CLAIM, policy=POLICY)
        self.assertEqual(env["NARWHAL_NODE_2_KUBERNETES_NODE"], "gpu-b")
        self.assertEqual(
            env["NARWHAL_NODE_2_URL"],
            f"http://engine-2.engines.{kubernetes_cluster.NAMESPACE}.svc:8100",
        )
        self.assertEqual(
            env["NARWHAL_NODE_1_ATTESTATION_URL"],
            f"http://engine-1.engines.{kubernetes_cluster.NAMESPACE}.svc:8200/v1/attestation",
        )
        self.assertEqual(env["NARWHAL_NODE_2_RDMA_GID_INDEX"], "3")
        self.assertEqual(env["NARWHAL_NODE_2_RDMA_ROCE_MODE"], "v2")
        self.assertNotIn("NARWHAL_NODE_1_RDMA_GID_INDEX", env)

    def test_the_rest_of_the_spec_becomes_its_variables(self):
        def change(d):
            spec = d["spec"]
            spec["engines"]["model"].update(file="gguf/model.gguf", fileSha256="a" * 64)
            spec["engines"]["apiKeySecret"] = {"name": "engine-key-v2", "key": "api-key"}
            spec["engines"]["resources"]["runtimeClassName"] = "nvidia"
            spec["profiling"]["decodeConcurrency"] = [1, 8]
            spec["timeouts"] = {"defaultSeconds": 600, "stages": {"profile": 7200}}

        env = spec_env(fleet_fixtures.changed(change), state_claim=CLAIM, policy=POLICY)
        self.assertEqual(env["NARWHAL_MODEL_PATH"], "/model/gguf/model.gguf")
        self.assertEqual(env["NARWHAL_MODEL_SHA256"], "a" * 64)
        self.assertEqual(env["NARWHAL_KUBERNETES_API_KEY_SECRET"], "engine-key-v2/api-key")
        self.assertEqual(env["NARWHAL_KUBERNETES_RUNTIME_CLASS"], "nvidia")
        self.assertEqual(env["NARWHAL_PROFILE_DECODE_CONCURRENCY"], "1,8")
        self.assertEqual(env["NARWHAL_STAGE_TIMEOUT_SECONDS"], "600")
        self.assertEqual(env["NARWHAL_STAGE_PROFILE_TIMEOUT_SECONDS"], "7200")
        self.assertEqual(env[fleet_resource.FLEET_UID_ENV], fleet_fixtures.UID)
        self.assertEqual(env[fleet_resource.HAPROXY_IMAGE_ENV], fleet_fixtures.HAPROXY_IMAGE)
        self.assertEqual(
            json.loads(env[fleet_resource.ROUTERS_ENV]),
            {"size": "1Gi", "storageClassName": "local"},
        )
        self.assertEqual(
            json.loads(env[fleet_resource.HAPROXY_ENV]),
            {"clusterDomain": "cluster.local", "replicas": 1},
        )
        self.assertEqual(env["NARWHAL_ENGINE_BACKEND"], "vllm")

    def test_no_variable_comes_from_until_stopped_or_retained_runs(self):
        base = spec_env(fleet_fixtures.fleet(), state_claim=CLAIM, policy=POLICY)
        edited = spec_env(
            fleet_fixtures.changed(
                lambda d: d["spec"].update(until="profile", stopped=True, retainRuns=5)
            ),
            state_claim=CLAIM,
            policy=POLICY,
        )
        self.assertEqual(base, edited)

    def test_the_model_volume_is_read_only_whatever_the_spec_says(self):
        writable = fleet_fixtures.changed(
            lambda d: d["spec"]["engines"]["modelVolume"]["persistentVolumeClaim"].update(
                readOnly=False
            )
        )
        env = spec_env(writable, state_claim=CLAIM, policy=POLICY)
        self.assertEqual(
            json.loads(env["NARWHAL_KUBERNETES_MODEL_VOLUME"]),
            {"persistentVolumeClaim": {"claimName": "model", "readOnly": True}},
        )
        for volume in ({}, {"hostPath": {"path": "/m"}, "nfs": {"server": "s", "path": "/m"}}):
            with self.subTest(volume), self.assertRaisesRegex(ValueError, "exactly one"):
                spec_env(
                    fleet_fixtures.changed(
                        lambda d, v=volume: d["spec"]["engines"].update(modelVolume=v)
                    ),
                    state_claim=CLAIM,
                    policy=POLICY,
                )

    def test_a_missing_field_gives_no_variable(self):
        env = spec_env(
            {
                "metadata": {"name": "fleet", "namespace": "n", "uid": fleet_fixtures.UID},
                "spec": {},
            },
            state_claim=CLAIM,
            policy=POLICY,
        )
        self.assertEqual(
            env,
            {
                "NARWHAL_KUBERNETES_NAMESPACE": "n",
                fleet_resource.FLEET_UID_ENV: fleet_fixtures.UID,
                "NARWHAL_KUBERNETES_STATE_CLAIM": CLAIM,
            },
        )


def volume(source):
    """`fleet()` with `source` as its model volume."""
    return fleet_fixtures.changed(lambda d: d["spec"]["engines"].update(modelVolume=source))


def policy(*sources, **lists):
    return fleet_resource.VolumePolicy(sources, **lists)


HOST = {"hostPath": {"path": "/srv/models", "type": "Directory"}}
NFS = {"nfs": {"server": "nfs.example.com", "path": "/exports/models", "readOnly": True}}
CSI = {"csi": {"driver": "csi.example.com", "readOnly": True}}


class ModelVolumes(unittest.TestCase):
    """A fleet mounts only the model volumes the cluster admin allows: a claim by default, and
    a hostPath, NFS export or CSI volume only once the admin lists it and what it may name."""

    def check(self, source, allowed):
        fleet_resource.check_model_volume(volume(source), allowed)

    def refused(self, source, allowed, message):
        with self.assertRaisesRegex(ValueError, message):
            self.check(source, allowed)

    def test_by_default_only_a_claim_is_allowed(self):
        self.check(fleet_fixtures.fleet()["spec"]["engines"]["modelVolume"], POLICY)
        everything = policy(
            "persistentVolumeClaim",
            host_path_prefixes=("/srv",),
            nfs_servers=("nfs.example.com",),
            csi_drivers=("csi.example.com",),
        )
        for source, kind in ((HOST, "hostPath"), (NFS, "nfs"), (CSI, "csi")):
            with self.subTest(kind):
                self.refused(source, POLICY, f"{kind}: the cluster admin allows only")
                self.refused(source, everything, "allows only persistentVolumeClaim")
        self.refused(
            fleet_fixtures.fleet()["spec"]["engines"]["modelVolume"],
            policy("nfs", nfs_servers=("nfs.example.com",)),
            "persistentVolumeClaim: the cluster admin allows only nfs",
        )

    def test_a_listed_source_needs_its_own_list(self):
        self.refused(HOST, policy("hostPath"), "hostPath is off")
        self.refused(NFS, policy("nfs"), "nfs.example.com is not in modelNfsServers")
        self.refused(CSI, policy("csi"), "csi.example.com is not in modelCsiDrivers")
        self.check(HOST, policy("hostPath", host_path_prefixes=("/srv",)))
        self.check(NFS, policy("nfs", nfs_servers=("nfs.example.com",)))
        self.check(CSI, policy("csi", csi_drivers=("csi.example.com",)))

    def test_an_unlisted_nfs_server_or_csi_driver_is_refused(self):
        loopback = {"nfs": {"server": "127.0.0.1", "path": "/etc", "readOnly": True}}
        self.refused(
            loopback, policy("nfs", nfs_servers=("nfs.example.com",)), "127.0.0.1 is not in"
        )
        other = {"csi": {"driver": "hostpath.csi.k8s.io", "readOnly": True}}
        self.refused(
            other, policy("csi", csi_drivers=("csi.example.com",)), "hostpath.csi.k8s.io is not"
        )

    def test_a_volume_needs_exactly_one_source(self):
        for source in ({}, {**HOST, **NFS}):
            with self.subTest(source):
                self.refused(source, POLICY, "exactly one")

    def test_a_policy_names_only_real_sources_and_normalised_prefixes(self):
        with self.assertRaisesRegex(ValueError, "emptyDir is no model volume source"):
            policy("emptyDir")
        with self.assertRaisesRegex(ValueError, "modelHostPathPrefixes: '/srv/'"):
            policy("hostPath", host_path_prefixes=("/srv/",))

    def test_spec_env_applies_the_policy(self):
        with self.assertRaisesRegex(ValueError, "allows only"):
            spec_env(volume(HOST), state_claim=CLAIM, policy=POLICY)
        env = spec_env(
            volume({"hostPath": {"path": "/srv/models"}}),
            state_claim=CLAIM,
            policy=policy("hostPath", host_path_prefixes=("/srv",)),
        )
        self.assertEqual(
            json.loads(env["NARWHAL_KUBERNETES_MODEL_VOLUME"]),
            {"hostPath": {"path": "/srv/models", "type": "Directory"}},
        )

    def test_the_chart_allows_a_claim_only_by_default(self):
        chart = kubernetes_cluster.CHART
        values = yaml.safe_load((chart / "values.yaml").read_text())
        self.assertEqual(fleet_resource.VolumePolicy.from_values(values), POLICY)
        self.assertEqual(fleet_resource.VolumePolicy.from_values({}), fleet_resource.VolumePolicy())
        schema = json.loads((chart / "values.schema.json").read_text())["properties"]
        self.assertEqual(schema["modelVolumeSources"]["items"]["enum"], list(MODEL_VOLUME_SOURCES))
        self.assertEqual(
            schema["modelHostPathPrefixes"]["items"]["pattern"], fleet_resource._NORMALISED
        )
        self.assertEqual(
            schema["modelNfsServers"]["items"]["pattern"],
            at("spec.engines.modelVolume.nfs.server")["pattern"],
        )
        self.assertEqual(
            schema["modelCsiDrivers"]["items"]["pattern"],
            at("spec.engines.modelVolume.csi.driver")["pattern"],
        )


class HostPaths(unittest.TestCase):
    """hostPath stays off until the cluster admin lists prefixes, and never reaches a node's
    system, kubelet or runtime directories."""

    def host(self, path, prefixes):
        fleet_resource.check_model_volume(
            volume({"hostPath": {"path": path, "type": "Directory"}}),
            policy("hostPath", host_path_prefixes=prefixes),
        )

    def test_without_prefixes_host_path_is_off(self):
        with self.assertRaisesRegex(ValueError, "hostPath is off"):
            self.host("/srv/models", ())

    def test_a_path_must_lie_at_or_below_a_prefix_by_segment(self):
        prefixes = ("/srv/models", "/data")
        for path in ("/srv/models", "/srv/models/llama", "/data/x/y"):
            self.host(path, prefixes)
        for path in ("/srv/models2", "/srv", "/opt/models", "/database"):
            with self.subTest(path), self.assertRaisesRegex(ValueError, "outside"):
                self.host(path, prefixes)

    def test_a_prefix_never_admits_a_system_directory(self):
        for path in (
            "/var/lib/kubelet/pods",
            "/var/run/docker.sock",
            "/etc/kubernetes",
            "/var/lib/etcd",
            "/var/log/pods",
            "/root/.kube",
            "/home/admin",
            "/opt/cni/bin",
            "/host/etc",
        ):
            with self.subTest(path), self.assertRaisesRegex(ValueError, "container runtime"):
                self.host(path, ("/var", "/etc", "/root", "/home", "/opt", "/host"))

    def test_a_path_must_be_normalised(self):
        for path in (
            "//models",
            "/models/",
            "/models/../etc",
            "/models/./a",
            "/",
            "models",
            "/m\n",
        ):
            with self.subTest(path), self.assertRaisesRegex(ValueError, "normalised"):
                self.host(path, ("/models",))


class Definition(unittest.TestCase):
    def test_the_crd_serves_and_stores_one_namespaced_version_with_status(self):
        crd = fleet_resource.crd()
        spec = crd["spec"]
        self.assertEqual(crd["metadata"]["name"], fleet_resource.CRD_NAME)
        self.assertEqual(spec["group"], GROUP)
        self.assertEqual(spec["scope"], "Namespaced")
        [version] = spec["versions"]
        self.assertEqual(
            (version["name"], version["served"], version["storage"]), ("v1alpha1", True, True)
        )
        self.assertEqual(version["subresources"], {"status": {}})
        self.assertEqual(
            [column["name"] for column in version["additionalPrinterColumns"]],
            ["Serving", "Ready", "Stage", "Engines", "Age"],
        )

    def test_the_crd_is_kept_and_carries_its_schema_digest(self):
        annotations = fleet_resource.crd()["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/resource-policy"], "keep")
        self.assertEqual(
            annotations[f"{GROUP}/schema-sha256"],
            hashlib.sha256(
                json.dumps(
                    fleet_resource.crd_spec(), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        )

    def test_the_digest_follows_the_schema(self):
        before = fleet_resource.schema_digest()
        original = SCHEMA["properties"]["spec"]["properties"]["retainRuns"]["maximum"]
        try:
            SCHEMA["properties"]["spec"]["properties"]["retainRuns"]["maximum"] = original + 1
            self.assertNotEqual(fleet_resource.schema_digest(), before)
        finally:
            SCHEMA["properties"]["spec"]["properties"]["retainRuns"]["maximum"] = original
        self.assertEqual(fleet_resource.schema_digest(), before)


class StoredDefinition(unittest.TestCase):
    """The controller checks the CRD the API server holds from its spec, not its annotation."""

    def stored(self):
        document = copy.deepcopy(fleet_resource.crd())
        # What the API server adds to a CRD it stores.
        document["metadata"].update(uid=fleet_fixtures.UID, resourceVersion="1", generation=1)
        document["spec"]["conversion"] = {"strategy": "None"}
        document["status"] = {"storedVersions": ["v1alpha1"], "conditions": []}
        return document

    def test_a_stored_crd_with_server_defaults_is_current(self):
        self.assertTrue(fleet_resource.crd_current(self.stored()))
        self.assertEqual(
            fleet_resource.stored_digest(self.stored()), fleet_resource.schema_digest()
        )

    def test_a_changed_schema_is_found_whatever_the_annotation_says(self):
        document = self.stored()
        engines = document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"][
            "spec"
        ]["properties"]["engines"]
        del engines["properties"]["modelVolume"]["x-kubernetes-validations"]
        self.assertEqual(
            document["metadata"]["annotations"][fleet_resource.SCHEMA_SHA256],
            fleet_resource.schema_digest(),
        )
        self.assertFalse(fleet_resource.crd_current(document))

    def test_a_wrong_annotation_alone_does_not_stall_the_fleet(self):
        document = self.stored()
        document["metadata"]["annotations"][fleet_resource.SCHEMA_SHA256] = "0" * 64
        self.assertTrue(fleet_resource.crd_current(document))

    def test_another_crd_is_not_the_fleet_crd(self):
        document = self.stored()
        document["metadata"]["name"] = "other.example.com"
        self.assertFalse(fleet_resource.crd_current(document))


class Generated(unittest.TestCase):
    def test_the_crd_chart_is_current(self):
        self.assertEqual(
            crd_schema.stale(),
            [],
            "run tools/maintenance/crd_schema.py after changing fleet_resource.py",
        )

    def test_the_template_holds_the_crd_and_the_chart_labels(self):
        text = crd_schema.crd_template()
        body = [
            line
            for line in text.splitlines()
            if "{{" not in line and line != "  labels:" and line.strip() not in crd_schema.LABELS
        ]
        self.assertEqual(yaml.safe_load("\n".join(body)), fleet_resource.crd())
        templated = [line for line in text.splitlines() if "{{" in line]
        self.assertEqual(len(templated), 1 + sum("{{" in label for label in crd_schema.LABELS))

    def test_the_yaml_form_round_trips(self):
        document = {
            "on": True,
            "a": [{"b": ["x", 1, False]}, [], {}],
            "quoted": 'it\'s "x" \\ é\n',
            "a/b": {"c": []},
        }
        self.assertEqual(yaml.safe_load("\n".join(crd_schema.yaml_lines(document))), document)

    def test_the_group_template_gives_the_group(self):
        self.assertIn(
            f'{{{{- define "narwhal.group" -}}}}{GROUP}{{{{- end -}}}}', crd_schema.group_template()
        )


if __name__ == "__main__":
    unittest.main()
