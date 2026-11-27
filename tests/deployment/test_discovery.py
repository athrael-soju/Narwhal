import copy
import hashlib
import inspect
import io
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from narwhal.backends.sglang import discovery as sglang_discovery
from narwhal.deployment.discovery import (
    PROBE,
    build_records,
    derive_hosts,
    discover,
    fabric_address,
    model_file,
    service_url,
)
from narwhal.deployment.engine_launch import load_launches
from narwhal.deployment.hosts import load_hosts
from narwhal.deployment.install import package
from narwhal.deployment.launch_engine.plan import build, conv_state_layers
from narwhal.deployment.role_env import select_values
from tests.deployment.fixtures import package_inputs

KNOWN_HOSTS = "host1.invalid ssh-ed25519 synthetic-public-key\n"


def observation(runtime="rocm", address="10.0.0.1"):
    return {
        "hostname": "synthetic-host",
        "image_id": "sha256:" + "a" * 64,
        "model_sha256": "b" * 64,
        "model_tree_sha256": "c" * 64,
        "model_dtype": "bfloat16",
        "max_model_len": 32768,
        "packages": {"vllm": "0.29.0+test", "nixl-rocm": "1.0.0", "torch": "2.12.0"},
        "runtime": runtime,
        "gpus": [
            {"id": str(i), "name": "test GPU", "device": f"/dev/dri/renderD{128 + i}"}
            for i in range(8)
        ],
        "common_devices": ["/dev/kfd"],
        "image_environment": {"VLLM_ROCM_USE_AITER": "1"},
        "interfaces": [{"ifname": "fabric0", "addr_info": [{"local": address, "scope": "global"}]}],
    }


def sglang_observation(address="10.0.0.1"):
    observed = observation(runtime="cuda", address=address)
    observed.update(
        packages={
            "sglang": "0.5.0+test",
            "mooncake-transfer-engine-cuda13": "0.3.0",
            "nixl": "1.0.0",
            "torch": "2.12.0",
        },
        gpus=[
            {"id": str(i), "name": "test GPU", "uuid": f"GPU-{i}", "device": f"/dev/nvidia{i}"}
            for i in range(8)
        ],
        common_devices=["/dev/nvidiactl", "/dev/nvidia-uvm"],
        image_environment={"SGLANG_ENABLE_JIT_DEEPGEMM": "0"},
    )
    return observed


def sglang_environment(root):
    env = environment(root)
    env.update(NARWHAL_ENGINE_BACKEND="sglang", NARWHAL_DECODE_CUDA_GRAPH_MEMORY_GB="4")
    return env


def environment(root):
    env = {
        "NARWHAL_HOSTS": str(root / "config/hosts.local.json"),
        "NARWHAL_LAUNCH_CONFIG": str(root / "config/engine-launch.local.json"),
        "NARWHAL_FLEET": str(root / "config/fleet.json"),
        "NARWHAL_SSH_KNOWN_HOSTS": str(root / "known_hosts"),
        "NARWHAL_ENGINE_IMAGE": "sha256:" + "a" * 64,
        "NARWHAL_ENGINE_MODEL_NAME": "synthetic-model",
        "NARWHAL_MODEL_DIR": "/models/synthetic-model",
        "NARWHAL_RUN_DIR": "/deployment-runs",
        "NARWHAL_FABRIC_INTERFACE": "fabric0",
        "NARWHAL_ENGINE_PORT": "8000",
        "NARWHAL_ATTEST_PORT": "8010",
        "NARWHAL_KV_SIDE_CHANNEL_PORT": "5557",
        "NARWHAL_UCX_TCP_PORT_RANGE": "20000-21000",
        "NARWHAL_MODEL_TREE_SHA256": "c" * 64,
    }
    if root.is_dir():
        Path(env["NARWHAL_SSH_KNOWN_HOSTS"]).write_text(KNOWN_HOSTS)
    for node in (1, 2):
        env.update(
            {
                f"NARWHAL_NODE_{node}_SSH": f"test@host{node}.invalid",
                f"NARWHAL_NODE_{node}_SSH_PASSWORD": "private-test-password",
            }
        )
    return env


def environment_file(path):
    """Return the variables that a role environment file exports."""
    values = {}
    for line in path.read_text().splitlines():
        name, quoted = line.removeprefix("export ").split("=", 1)
        values[name] = shlex.split(quoted)[0]
    return values


# The stage whose time limit bounds each remote discovery operation.
STAGES = {
    "management access": "access",
    "inspect GPU and image": "discover",
    "hash checkpoint files": "discover",
}


class FakeInspectionSSH:
    def __init__(self, env, logs):
        self.calls = []
        assert Path(env["NARWHAL_SSH_KNOWN_HOSTS"]).read_text() == KNOWN_HOSTS

    def run(self, host, stage, operation, script, payload=None, reason=None):
        assert STAGES[operation] == stage
        self.calls.append((host.id, operation))
        if payload:
            if operation == "hash checkpoint files":
                return json.dumps(
                    {
                        "model_tree_sha256": "c" * 64,
                        "files": [{"path": "config.json", "size": 12, "sha256": "d" * 64}],
                        "file_count": 1,
                        "bytes_verified": 12,
                    }
                )
            return json.dumps(observation(address="10.0.0." + host.id.split("-")[-1]))
        return "synthetic-host"


GGUF_SHA256 = "9" * 64
PLUGIN_PINS = {"gguf_plugin_python_sha256": "5" * 64, "gguf_plugin_extension_sha256": "6" * 64}


def gguf_environment(env):
    """Pin the qualified GGUF plugin files in a discovery environment."""
    env.update({f"NARWHAL_{name.upper()}": digest for name, digest in PLUGIN_PINS.items()})
    return env


class GgufInspectionSSH(FakeInspectionSSH):
    """Inspects a model directory with a GGUF file and an image with the GGUF plugin."""

    def run(self, host, stage, operation, script, payload=None, reason=None):
        result = super().run(host, stage, operation, script, payload)
        if operation == "hash checkpoint files":
            record = json.loads(result)
            record["files"].append({"path": "weights.gguf", "size": 4, "sha256": GGUF_SHA256})
            return json.dumps(record)
        if operation == "inspect GPU and image":
            pattern = re.compile(json.loads(payload.read_text())["packages"])
            observed = json.loads(result)
            observed["multimodal"] = True
            observed["packages"]["vllm-gguf-plugin"] = "0.0.5"
            observed["packages"] = {
                name: version
                for name, version in observed["packages"].items()
                if pattern.fullmatch(name)
            }
            return json.dumps(observed)
        return result


def gguf_observation(address):
    observed = observation(address=address)
    observed["packages"]["vllm-gguf-plugin"] = "0.0.5"
    observed.update(model_file="weights.gguf", model_file_sha256=GGUF_SHA256)
    return observed


class DiscoveryTests(unittest.TestCase):
    def test_gguf_discovery_pins_the_file_and_plugin_and_feeds_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = gguf_environment(environment(root))
            env["NARWHAL_MODEL_PATH"] = env["NARWHAL_MODEL_DIR"] + "/weights.gguf"
            out = root / "discovery"
            with (
                patch("narwhal.deployment.discovery.SSH", GgufInspectionSSH),
                redirect_stdout(io.StringIO()),
            ):
                discover(env, out)
            for line in (root / "config/deployment.env").read_text().splitlines():
                name, quoted = line.removeprefix("export ").split("=", 1)
                env[name] = shlex.split(quoted)[0]
            inputs = json.loads((out / "engine-1-inputs.json").read_text())
            self.assertEqual(inputs["packages"], "vllm|torch|nixl|nixl-rocm|vllm-gguf-plugin")
            observed = json.loads((out / "engine-1.json").read_text())
            self.assertEqual(observed["model_file"], "weights.gguf")
            self.assertEqual(observed["model_file_sha256"], GGUF_SHA256)
            fleet = json.loads(Path(env["NARWHAL_FLEET"]).read_text())
            launches = json.loads(Path(env["NARWHAL_LAUNCH_CONFIG"]).read_text())
            for node in (1, 2):
                role = f"engine-{node}"
                self.assertEqual(env[f"NARWHAL_NODE_{node}_MODEL_SHA256"], GGUF_SHA256)
                serving = launches["engines"][role]["runtime"]
                self.assertEqual(serving["expected_packages"]["vllm-gguf-plugin"], "0.0.5")
                self.assertEqual(serving["tokenizer"], "/model")
                self.assertEqual(
                    serving["extra_args"][-5:],
                    [
                        "--load-format",
                        "gguf",
                        "--hf-config-path",
                        "/model",
                        "--language-model-only",
                    ],
                )
                self.assertEqual({name: serving[name] for name in PLUGIN_PINS}, PLUGIN_PINS)
                role_env = select_values("engine", node, fleet, env)
                role_env["NARWHAL_DEPLOYMENT_REVISION"] = "f" * 64
                self.assertEqual(role_env["NARWHAL_MODEL_PATH"], env["NARWHAL_MODEL_PATH"])
                self.assertEqual(role_env["NARWHAL_MODEL_SHA256"], GGUF_SHA256)
                records = load_launches(
                    Path(env["NARWHAL_LAUNCH_CONFIG"]), [role], {role: role_env}
                )
                plan, _ = build(records[role], role_env, root / role)
                self.assertEqual(
                    plan["args"][plan["args"].index("--model") + 1], "/model/weights.gguf"
                )
                self.assertEqual(plan["model_path"], env["NARWHAL_MODEL_PATH"])
                self.assertEqual(plan["runtime_pins"], PLUGIN_PINS)

    def test_gguf_discovery_requires_a_file_in_the_model_directory(self):
        for path, message in (
            ("/elsewhere/weights.gguf", "absolute file path in NARWHAL_MODEL_DIR"),
            ("weights.gguf", "absolute file path in NARWHAL_MODEL_DIR"),
            ("/models/synthetic-model/other.gguf", "names no file in NARWHAL_MODEL_DIR"),
            ("/models/synthetic-model/../weights.gguf", "absolute file path in NARWHAL_MODEL_DIR"),
            ("/models/synthetic-model/sub/../../weights.gguf", "absolute file path in"),
        ):
            with self.subTest(path), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                env = environment(root)
                env["NARWHAL_MODEL_PATH"] = path
                with (
                    patch("narwhal.deployment.discovery.SSH", GgufInspectionSSH),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(ValueError, message),
                ):
                    discover(env, root / "discovery")
                self.assertFalse(Path(env["NARWHAL_FLEET"]).exists())

    def test_a_backend_without_model_files_refuses_a_model_path(self):
        for name, label in (("sglang", "SGLang"), ("trtllm", "TensorRT-LLM")):
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                env = environment(root)
                env.update(
                    NARWHAL_ENGINE_BACKEND=name,
                    NARWHAL_MODEL_PATH=env["NARWHAL_MODEL_DIR"] + "/weights.gguf",
                )
                with (
                    patch("narwhal.deployment.discovery.SSH", GgufInspectionSSH),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(ValueError, f"{label} serves a model directory"),
                ):
                    discover(env, root / "discovery")
                self.assertFalse(Path(env["NARWHAL_FLEET"]).exists())

    def test_a_router_pair_adds_the_standby_and_load_balancer_roles(self):
        env = {**environment(Path("/synthetic")), "NARWHAL_STANDBY_ROUTER_SSH": "deploy@other"}
        env["NARWHAL_ROUTER_SSH"] = ""
        roles = {host.id: host.roles for host in derive_hosts(env)}
        self.assertIn("router", roles["node-1"])
        self.assertIn("load-balancer", roles["node-1"])
        self.assertEqual(roles["standby-router"], ("standby-router",))
        env["NARWHAL_LOAD_BALANCER_SSH"] = env["NARWHAL_STANDBY_ROUTER_SSH"]
        roles = {host.id: host.roles for host in derive_hosts(env)}
        self.assertEqual(roles["standby-router"], ("standby-router", "load-balancer"))
        env["NARWHAL_STANDBY_ROUTER_SSH"] = env["NARWHAL_NODE_1_SSH"]
        with self.assertRaisesRegex(ValueError, "another host than NARWHAL_NODE_1_SSH"):
            derive_hosts(env)

    def test_a_model_path_through_a_subdirectory_segment_names_the_same_file(self):
        env = environment(Path("/synthetic"))
        env["NARWHAL_MODEL_PATH"] = "/models/synthetic-model/sub/../weights.gguf"
        self.assertEqual(model_file(env, 1, env["NARWHAL_MODEL_DIR"]), "weights.gguf")

    def test_gguf_records_need_the_plugin_one_pinned_file_and_a_gguf_name(self):
        env = gguf_environment(environment(Path("/synthetic")))
        hosts = derive_hosts(env)

        def records(change=None):
            observations = {f"engine-{i}": gguf_observation(f"10.0.0.{i}") for i in (1, 2)}
            if change:
                change(observations)
            return build_records(hosts, env, observations, Path("/synthetic"))

        _, launches, _, derived = records()
        self.assertEqual(derived["NARWHAL_NODE_2_MODEL_SHA256"], GGUF_SHA256)
        # A language-only configuration needs no flag to leave out encoders.
        self.assertNotIn(
            "--language-model-only", launches["engines"]["engine-1"]["runtime"]["extra_args"]
        )

        def multimodal(observations):
            for observed in observations.values():
                observed["multimodal"] = True

        _, launches, _, _ = records(multimodal)
        self.assertEqual(
            launches["engines"]["engine-1"]["runtime"]["extra_args"][-1], "--language-model-only"
        )
        # The operator keeps multimodal input for the whole fleet.
        env["NARWHAL_LANGUAGE_MODEL_ONLY"] = "false"
        _, launches, _, _ = records(multimodal)
        for name in ("engine-1", "engine-2"):
            self.assertNotIn(
                "--language-model-only", launches["engines"][name]["runtime"]["extra_args"]
            )
        # One engine cannot differ, since a multimodal request could reach a text-only engine.
        env["NARWHAL_NODE_2_LANGUAGE_MODEL_ONLY"] = "true"
        with self.assertRaisesRegex(
            ValueError, "set NARWHAL_LANGUAGE_MODEL_ONLY once for the fleet, not NARWHAL_NODE_2_"
        ):
            records(multimodal)
        del env["NARWHAL_LANGUAGE_MODEL_ONLY"], env["NARWHAL_NODE_2_LANGUAGE_MODEL_ONLY"]
        env["NARWHAL_ENGINE_ARGS"] = json.dumps(["--hf-config-path", "/model/config"])
        _, launches, _, _ = records()
        self.assertEqual(
            launches["engines"]["engine-1"]["runtime"]["extra_args"],
            ["--hf-config-path", "/model/config", "--load-format", "gguf"],
        )
        del env["NARWHAL_ENGINE_ARGS"]

        def unpinned_plugin(observations):
            del observations["engine-1"]["packages"]["vllm-gguf-plugin"]

        def other_file(observations):
            observations["engine-2"]["model_file_sha256"] = "8" * 64

        def checkpoint_peer(observations):
            for field in ("model_file", "model_file_sha256"):
                del observations["engine-2"][field]

        def not_gguf(observations):
            observations["engine-1"]["model_file"] = "weights.bin"

        for change, message in (
            (unpinned_plugin, "engine-1: a GGUF model requires vllm-gguf-plugin"),
            (other_file, "Every engine must serve the same model file"),
            (checkpoint_peer, "Every engine must serve the same model file"),
            (not_gguf, "engine-1: set NARWHAL_MODEL_PATH to a .gguf file"),
        ):
            with self.subTest(change.__name__), self.assertRaisesRegex(ValueError, message):
                records(change)
        env["NARWHAL_NODE_1_GGUF_PLUGIN_EXTENSION_SHA256"] = "not-a-digest"
        with self.assertRaisesRegex(ValueError, "engine-1: set GGUF_PLUGIN_PYTHON_SHA256"):
            records()
        del env["NARWHAL_NODE_1_GGUF_PLUGIN_EXTENSION_SHA256"]
        del env["NARWHAL_GGUF_PLUGIN_PYTHON_SHA256"]
        with self.assertRaisesRegex(ValueError, "engine-1: set GGUF_PLUGIN_PYTHON_SHA256"):
            records()
        gguf_environment(env)
        env["NARWHAL_MODEL_SHA256"] = "7" * 64
        with self.assertRaisesRegex(ValueError, "engine-1: model-file hash differs"):
            records()

    def test_checkpoint_records_name_no_model_file(self):
        env = environment(Path("/synthetic"))
        hosts = derive_hosts(env)
        observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
        _, launches, _, derived = build_records(hosts, env, observations, Path("/synthetic"))
        self.assertNotIn("NARWHAL_NODE_1_MODEL_SHA256", derived)
        serving = launches["engines"]["engine-1"]["runtime"]
        self.assertNotIn("tokenizer", serving)
        self.assertEqual(
            serving["extra_args"], ["--gpu-memory-utilization", "0.9", "--enforce-eager"]
        )
        env["NARWHAL_MODEL_SHA256"] = GGUF_SHA256
        with self.assertRaisesRegex(ValueError, "engine-1: model-file hash differs"):
            build_records(hosts, env, observations, Path("/synthetic"))
        del env["NARWHAL_MODEL_SHA256"]
        # A checkpoint deployment checks the fleet's language-only choice too.
        for setting in ("true", "false"):
            env["NARWHAL_LANGUAGE_MODEL_ONLY"] = setting
            _, launches, _, _ = build_records(hosts, env, observations, Path("/synthetic"))
            self.assertNotIn(
                "--language-model-only", launches["engines"]["engine-1"]["runtime"]["extra_args"]
            )
        env["NARWHAL_LANGUAGE_MODEL_ONLY"] = "no"
        with self.assertRaisesRegex(ValueError, "engine-1: set LANGUAGE_MODEL_ONLY to true"):
            build_records(hosts, env, observations, Path("/synthetic"))
        env["NARWHAL_LANGUAGE_MODEL_ONLY"] = "true"
        env["NARWHAL_NODE_1_LANGUAGE_MODEL_ONLY"] = "true"
        with self.assertRaisesRegex(ValueError, "once for the fleet, not NARWHAL_NODE_1_"):
            build_records(hosts, env, observations, Path("/synthetic"))

    def test_env_only_discovery_feeds_real_preparation_and_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            out = root / "discovery"
            self.assertFalse((root / "config").exists())
            with (
                patch("narwhal.deployment.discovery.SSH", FakeInspectionSSH),
                redirect_stdout(io.StringIO()),
            ):
                discover(env, out)
            for line in (root / "config/deployment.env").read_text().splitlines():
                name, quoted = line.removeprefix("export ").split("=", 1)
                env[name] = shlex.split(quoted)[0]
            self.assertEqual(env["NARWHAL_NODE_1_IP"], "10.0.0.1")
            self.assertFalse((out / "derived.env").exists())
            self.assertEqual(env["NARWHAL_NODE_2_IP"], "10.0.0.2")
            self.assertEqual(env["NARWHAL_NODE_1_URL"], "http://10.0.0.1:8000")
            self.assertEqual(
                env["NARWHAL_NODE_2_ATTESTATION_URL"],
                "http://10.0.0.2:8010/v1/attestation",
            )
            hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
            self.assertEqual(len(hosts), 2)
            self.assertEqual(hosts[0].roles, ("engine-1", "router"))
            fleet = json.loads(Path(env["NARWHAL_FLEET"]).read_text())
            self.assertEqual(fleet["hardware"]["tensor_parallel"], 8)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["model_tree_sha256"], "c" * 64)
            self.assertTrue((out / "engine-1-checkpoint.json").is_file())
            self.assertNotIn("engine_contract", fleet)
            self.assertFalse((root / "runs/profiles.json").exists())
            env.update(package_inputs(root))
            run = root / "packaged"
            package(hosts, env, run)
            for node in (1, 2):
                role = f"engine-{node}"
                role_env = environment_file(run / f"node-{node}/.env.{role}")
                # prepare checks the checkpoint against the digest that discovery verified.
                self.assertEqual(role_env["NARWHAL_MODEL_TREE_SHA256"], "c" * 64)
                self.assertNotIn(f"NARWHAL_NODE_{node}_MODEL_TREE_SHA256", env)
                records = load_launches(
                    Path(env["NARWHAL_LAUNCH_CONFIG"]), [role], {role: role_env}
                )
                plan, _ = build(records[role], role_env, root / role)
                self.assertIn("--tensor-parallel-size", plan["args"])
                self.assertEqual(records[role]["tensor_parallel_size"], 8)
                self.assertTrue((run / f"node-{node}/.env.{role}").exists())
            for file in (root / "config").iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("private-test-password", file.read_text())
            old = Path(env["NARWHAL_FLEET"]).read_bytes()
            with patch("narwhal.deployment.discovery.SSH") as ssh:
                with self.assertRaisesRegex(ValueError, "Archive previous"):
                    discover(env, root / "another-discovery")
                ssh.assert_not_called()
            self.assertEqual(Path(env["NARWHAL_FLEET"]).read_bytes(), old)

    def test_sglang_discovery_feeds_real_preparation_and_launcher(self):
        class SglangInspectionSSH(FakeInspectionSSH):
            def run(self, host, stage, operation, script, payload=None, reason=None):
                result = super().run(host, stage, operation, script, payload)
                if operation == "inspect GPU and image":
                    return json.dumps(sglang_observation("10.0.0." + host.id.split("-")[-1]))
                return result

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = sglang_environment(root)
            with (
                patch("narwhal.deployment.discovery.SSH", SglangInspectionSSH),
                redirect_stdout(io.StringIO()),
            ):
                discover(env, root / "discovery")
            for line in (root / "config/deployment.env").read_text().splitlines():
                name, quoted = line.removeprefix("export ").split("=", 1)
                env[name] = shlex.split(quoted)[0]
            inputs = json.loads((root / "discovery/engine-1-inputs.json").read_text())
            self.assertIn("SGLANG_", inputs["environment_prefixes"])
            self.assertIn("NARWHAL_SGLANG_BOOTSTRAP_PORT", inputs["managed_environment"])
            fleet = json.loads(Path(env["NARWHAL_FLEET"]).read_text())
            self.assertEqual(fleet["engine"], {"backend": "sglang"})
            self.assertNotIn("pin", fleet["engines"][0])
            launches = json.loads(Path(env["NARWHAL_LAUNCH_CONFIG"]).read_text())
            serving = launches["engines"]["engine-1"]["runtime"]
            self.assertEqual(serving["backend"], "sglang")
            self.assertEqual(serving["connector"], "mooncake")
            self.assertEqual(serving["decode_cuda_graph_memory_gb"], 4.0)
            self.assertNotIn("role", serving)
            self.assertEqual(serving["extra_args"], [])
            self.assertEqual(serving["context_length"], 16384)
            hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
            env.update(package_inputs(root))
            run = root / "packaged"
            package(hosts, env, run)
            limits = json.loads((run / "node-1/profiling-limits.json").read_text())
            self.assertEqual(limits["engines"], {"n1": 256, "n2": 256})
            # SGLang has no cache capture, so its engines receive the transfer measurement tool
            # and no capture hook.
            self.assertFalse((run / "node-1/cache_capture_hook.py").exists())
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertNotIn("node-1/cache_capture_hook.py", manifest["hashes"])
            self.assertIn("node-1/transfer_measure.py", manifest["hashes"])
            for node in (1, 2):
                role = f"engine-{node}"
                role_env = environment_file(run / f"node-{node}/.env.{role}")
                self.assertEqual(role_env["NARWHAL_KV_SIDE_CHANNEL_PORT"], "5557")
                records = load_launches(
                    Path(env["NARWHAL_LAUNCH_CONFIG"]), [role], {role: role_env}
                )
                plan, _ = build(records[role], role_env, root / role)
                self.assertEqual(plan["engine"], "sglang")
                self.assertIn("--tp-size", plan["args"])

    def test_env_sets_prefix_caching_cache_events_and_tokenizer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            env.update(
                NARWHAL_NODE_1_PREFIX_CACHING="false",
                NARWHAL_NODE_2_KV_EVENTS="false",
                NARWHAL_TOKENIZER="/tokenizer",
            )
            hosts = derive_hosts(env)
            observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
            _, launches, _, _ = build_records(hosts, env, observations, root)
            one = launches["engines"]["engine-1"]["runtime"]
            two = launches["engines"]["engine-2"]["runtime"]
            self.assertEqual((one["prefix_caching"], one["tokenizer"]), (False, "/tokenizer"))
            self.assertNotIn("kv_events", one)
            self.assertEqual((two["kv_events"], two["tokenizer"]), (False, "/tokenizer"))
            self.assertNotIn("prefix_caching", two)
            env["NARWHAL_KV_EVENTS"] = "no"
            with self.assertRaisesRegex(ValueError, "set KV_EVENTS to true or false"):
                build_records(hosts, env, observations, root)

    def test_trtllm_discovery_refuses_cache_events(self):
        env = environment(Path("/synthetic"))
        env.update(NARWHAL_ENGINE_BACKEND="trtllm", NARWHAL_KV_EVENTS="true")
        hosts = derive_hosts(env)
        observations = {}
        for i in (1, 2):
            observed = sglang_observation(f"10.0.0.{i}")
            observed.update(packages={"tensorrt_llm": "1.4.0rc3", "torch": "2.12.0"})
            observations[f"engine-{i}"] = observed
        with self.assertRaisesRegex(ValueError, "set NARWHAL_KV_EVENTS to false in .env"):
            build_records(hosts, env, observations, Path("/synthetic"))

    def test_sglang_engines_need_the_mooncake_connector_and_graph_memory(self):
        env = sglang_environment(Path("/synthetic"))
        hosts = derive_hosts(env)
        observations = {f"engine-{i}": sglang_observation(f"10.0.0.{i}") for i in (1, 2)}
        for connector in ("nixl", "unknown"):
            env["NARWHAL_ENGINE_CONNECTOR"] = connector
            with self.assertRaisesRegex(ValueError, f"no connector '{connector}'"):
                build_records(hosts, env, observations, Path("/synthetic"))
        env["NARWHAL_ENGINE_CONNECTOR"] = "mooncake"
        del env["NARWHAL_DECODE_CUDA_GRAPH_MEMORY_GB"]
        with self.assertRaisesRegex(ValueError, "engine-1: set DECODE_CUDA_GRAPH_MEMORY_GB"):
            build_records(hosts, env, observations, Path("/synthetic"))

    def test_switching_sglang_engines_cache_ssm_state_without_the_extra_buffer(self):
        env = sglang_environment(Path("/synthetic"))
        hosts = derive_hosts(env)
        observations = {f"engine-{i}": sglang_observation(f"10.0.0.{i}") for i in (1, 2)}
        for observed in observations.values():
            observed["conv_state_layers"] = True

        def extra_args() -> list[str]:
            _, launches, _, _ = build_records(hosts, env, observations, Path("/synthetic"))
            return launches["engines"]["engine-1"]["runtime"]["extra_args"]

        self.assertEqual(
            extra_args(),
            ["--mamba-radix-cache-strategy", "no_buffer", "--disable-overlap-schedule"],
        )
        env["NARWHAL_ENGINE_ARGS"] = json.dumps(["--mamba-radix-cache-strategy", "no_buffer"])
        self.assertEqual(extra_args(), ["--mamba-radix-cache-strategy", "no_buffer"])
        env["NARWHAL_ENGINE_ARGS"] = json.dumps(["--disable-radix-cache"])
        with self.assertRaisesRegex(
            ValueError, "sets --disable-radix-cache, which runtime.prefix_caching sets"
        ):
            extra_args()
        del env["NARWHAL_ENGINE_ARGS"]
        env["NARWHAL_PREFIX_CACHING"] = "false"
        self.assertEqual(extra_args(), [])

    def test_current_sglang_switches_roles_with_the_overlap_scheduler_and_flashinfer_decode(self):
        env = sglang_environment(Path("/synthetic"))
        hosts = derive_hosts(env)
        observations = {f"engine-{i}": sglang_observation(f"10.0.0.{i}") for i in (1, 2)}

        def extra_args() -> list[str]:
            _, launches, _, _ = build_records(hosts, env, observations, Path("/synthetic"))
            return launches["engines"]["engine-1"]["runtime"]["extra_args"]

        decode = ["--decode-attention-backend", "flashinfer"]
        for version in ("0.5.23", "0.5.23.post1", "0.6.0+test"):
            for observed in observations.values():
                observed["packages"]["sglang"] = version
                observed["conv_state_layers"] = True
            self.assertEqual(extra_args(), decode, version)
        env["NARWHAL_ENGINE_ARGS"] = json.dumps(["--decode-attention-backend", "triton"])
        self.assertEqual(extra_args(), ["--decode-attention-backend", "triton"])
        strategy = ["--mamba-radix-cache-strategy", "extra_buffer"]
        env["NARWHAL_ENGINE_ARGS"] = json.dumps(strategy)
        self.assertEqual(extra_args(), strategy + decode)
        del env["NARWHAL_ENGINE_ARGS"]
        # Earlier releases, their pre-releases and development builds keep both flags.
        pins = ["--mamba-radix-cache-strategy", "no_buffer", "--disable-overlap-schedule"]
        for version in ("0.5.22", "0.5.22.post1", "0.5.23rc0", "0.5.23.dev1"):
            for observed in observations.values():
                observed["packages"]["sglang"] = version
            self.assertEqual(extra_args(), pins, version)

    def test_an_unreadable_sglang_version_is_an_earlier_release(self):
        for version in ("", "unknown", "0.5"):
            self.assertFalse(sglang_discovery.at_least(version, (0, 5, 23)), version)
        self.assertTrue(sglang_discovery.at_least("v0.5.23+cu130", (0, 5, 23)))

    def test_discovery_rejects_checkpoint_file_mismatch_before_writing_fleet(self):
        class DifferentCheckpointSSH(FakeInspectionSSH):
            def run(self, host, stage, operation, script, payload=None, reason=None):
                result = super().run(host, stage, operation, script, payload)
                if operation == "hash checkpoint files" and host.id == "node-2":
                    record = json.loads(result)
                    record["model_tree_sha256"] = "e" * 64
                    record["files"][0]["sha256"] = "f" * 64
                    return json.dumps(record)
                return result

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            with (
                patch("narwhal.deployment.discovery.SSH", DifferentCheckpointSSH),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(ValueError, "checkpoint file config.json differs"),
            ):
                discover(env, root / "discovery")
            self.assertFalse(Path(env["NARWHAL_FLEET"]).exists())

    def test_an_interrupt_during_checkpoint_hashing_stops_every_host_at_once(self):
        started = []
        stopped = threading.Event()

        class SlowCheckpointSSH(FakeInspectionSSH):
            def run(self, host, stage, operation, script, payload=None, reason=None):
                if operation != "hash checkpoint files":
                    return super().run(host, stage, operation, script, payload)
                started.append(host.id)
                if stopped.wait(20):
                    raise RuntimeError(f"{host.id}: the hash was stopped")
                return super().run(host, stage, operation, script, payload)

            def cancel(self):
                stopped.set()

        def interrupt():
            deadline = time.monotonic() + 10
            while len(started) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            os.kill(os.getpid(), signal.SIGINT)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            threading.Thread(target=interrupt, daemon=True).start()
            began = time.monotonic()
            with (
                patch("narwhal.deployment.discovery.SSH", SlowCheckpointSSH),
                redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                discover(env, root / "discovery")
            self.assertLess(time.monotonic() - began, 10)
            self.assertTrue(stopped.is_set())
            self.assertEqual(len(started), 2)

    def test_router_management_destination_defaults_to_first_engine(self):
        env = environment(Path("/synthetic"))
        hosts = derive_hosts(env)
        self.assertEqual(hosts[0].roles, ("engine-1", "router"))
        env["NARWHAL_ROUTER_SSH"] = "test@router.invalid"
        hosts = derive_hosts(env)
        self.assertEqual(len(hosts), 3)
        self.assertEqual(hosts[-1].roles, ("router",))
        self.assertEqual(hosts[-1].ssh_env, "NARWHAL_ROUTER_SSH")

    def test_fabric_address_requires_unique_interface_address_or_checked_override(self):
        env = environment(Path("/synthetic"))
        interfaces = observation()["interfaces"]
        self.assertEqual(fabric_address(env, 1, interfaces), "10.0.0.1")
        interfaces[0]["addr_info"].append({"local": "10.0.0.9", "scope": "global"})
        with self.assertRaisesRegex(ValueError, "explicit selection"):
            fabric_address(env, 1, interfaces)
        env["NARWHAL_NODE_1_IP"] = "10.0.0.9"
        self.assertEqual(fabric_address(env, 1, interfaces), "10.0.0.9")
        env["NARWHAL_NODE_1_IP"] = "10.0.0.8"
        with self.assertRaisesRegex(ValueError, "selected fabric interface"):
            fabric_address(env, 1, interfaces)

    def test_service_urls_use_discovered_ipv6_and_accept_explicit_override(self):
        env = environment(Path("/synthetic"))
        env["NARWHAL_NODE_1_IP"] = "fd00::1"
        self.assertEqual(service_url(env, 1, "URL"), "http://[fd00::1]:8000")
        self.assertEqual(
            service_url(env, 1, "ATTESTATION_URL"),
            "http://[fd00::1]:8010/v1/attestation",
        )
        env["NARWHAL_NODE_1_URL"] = "http://engine.example.invalid:8000"
        self.assertEqual(service_url(env, 1, "URL"), env["NARWHAL_NODE_1_URL"])
        env["NARWHAL_NODE_1_ATTESTATION_URL"] = "http://engine.example.invalid:8010/wrong"
        with self.assertRaisesRegex(ValueError, "HTTP service URL"):
            service_url(env, 1, "ATTESTATION_URL")

    def test_policy_overrides_and_image_hash_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            env.update(
                NARWHAL_GPU_IDS="2,3",
                NARWHAL_TENSOR_PARALLEL_SIZE="2",
                NARWHAL_CONTEXT_LENGTH="8192",
                NARWHAL_NODE_1_MAX_SEQUENCES="6",
                NARWHAL_ENGINE_ARGS='["--enforce-eager"]',
                NARWHAL_ENGINE_ENV='{"VLLM_ROCM_USE_AITER": "0"}',
            )
            hosts = derive_hosts(env)
            observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
            observations["engine-1"]["requires_trust_remote_code"] = True
            fleet, launches, _, _ = build_records(hosts, env, observations, root)
            self.assertEqual(fleet["hardware"]["accelerators_per_engine"], 2)
            one = launches["engines"]["engine-1"]
            self.assertEqual(
                one["accelerator_devices"],
                ["/dev/kfd", "/dev/dri/renderD130", "/dev/dri/renderD131"],
            )
            self.assertEqual(one["runtime"]["environment"]["VLLM_ROCM_USE_AITER"], "0")
            self.assertEqual(
                one["runtime"]["extra_args"], ["--enforce-eager", "--trust-remote-code"]
            )
            self.assertEqual(one["runtime"]["context_length"], 8192)
            self.assertEqual(one["runtime"]["max_sequences"], 6)
            two = launches["engines"]["engine-2"]["runtime"]
            self.assertEqual(
                (two["extra_args"], two["context_length"], two["max_sequences"]),
                (["--enforce-eager"], 8192, 8),
            )
            env["NARWHAL_ENGINE_ARGS"] = '["--max-model-len", "8192"]'
            with self.assertRaisesRegex(
                ValueError, "sets --max-model-len, which runtime.context_length sets"
            ):
                build_records(hosts, env, observations, root)
            env["NARWHAL_ENGINE_ARGS"] = '["--enforce-eager"]'
            env["NARWHAL_MODEL_CONFIG_SHA256"] = "c" * 64
            with self.assertRaisesRegex(ValueError, "hash differs"):
                build_records(hosts, env, observations, root)

    def test_convolutional_transfer_layout_is_derived_before_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            hosts = derive_hosts(env)
            observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
            observations["engine-1"]["conv_state_layers"] = True
            _, launches, _, _ = build_records(hosts, env, observations, root)
            self.assertEqual(
                launches["engines"]["engine-1"]["runtime"]["environment"][
                    "VLLM_SSM_CONV_STATE_LAYOUT"
                ],
                "DS",
            )
            self.assertNotIn(
                "VLLM_SSM_CONV_STATE_LAYOUT",
                launches["engines"]["engine-2"]["runtime"]["environment"],
            )
            env["NARWHAL_ENGINE_ENV"] = '{"VLLM_SSM_CONV_STATE_LAYOUT":"SD"}'
            with self.assertRaisesRegex(ValueError, "requires VLLM_SSM_CONV_STATE_LAYOUT=DS"):
                build_records(hosts, env, observations, root)

    def test_colocated_engines_require_disjoint_allocations(self):
        env = environment(Path("/synthetic"))
        env["NARWHAL_NODE_2_SSH"] = env["NARWHAL_NODE_1_SSH"]
        hosts = derive_hosts(env)
        observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
        with self.assertRaisesRegex(ValueError, "GPU_IDS"):
            build_records(hosts, env, observations, Path("/synthetic"))
        env.update(NARWHAL_NODE_1_GPU_IDS="0,1", NARWHAL_NODE_2_GPU_IDS="1,2")
        with self.assertRaisesRegex(ValueError, "overlap"):
            build_records(hosts, env, observations, Path("/synthetic"))

    def test_mixed_hardware_and_management_environment_are_rejected(self):
        env = environment(Path("/synthetic"))
        hosts = derive_hosts(env)
        observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
        changed = copy.deepcopy(observations)
        for gpu in changed["engine-2"]["gpus"]:
            gpu["name"] = "different GPU"
        with self.assertRaisesRegex(ValueError, "matching GPU"):
            build_records(hosts, env, changed, Path("/synthetic"))
        env["NARWHAL_ENGINE_ENV"] = '{"NARWHAL_SSH_PASSWORD": "sensitive"}'
        with self.assertRaisesRegex(ValueError, "unsupported runtime environment"):
            build_records(hosts, env, observations, Path("/synthetic"))

    def test_discovery_trusts_only_the_supplied_known_hosts_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            trust = Path(env["NARWHAL_SSH_KNOWN_HOSTS"])
            for value in ("", str(root / "absent")):
                with (
                    self.subTest(value=value),
                    patch.dict(env, {"NARWHAL_SSH_KNOWN_HOSTS": value}),
                    patch("narwhal.deployment.discovery.SSH") as ssh,
                    self.assertRaisesRegex(ValueError, "Set NARWHAL_SSH_KNOWN_HOSTS"),
                ):
                    discover(env, root / "discovery")
                ssh.assert_not_called()
                self.assertFalse((root / "discovery").exists())
            # No generated output may replace the known-hosts file.
            for name in ("NARWHAL_HOSTS", "NARWHAL_FLEET", "NARWHAL_LAUNCH_CONFIG"):
                with (
                    self.subTest(name=name),
                    patch.dict(env, {name: str(trust)}),
                    patch("narwhal.deployment.discovery.SSH") as ssh,
                    self.assertRaisesRegex(ValueError, "Select distinct private configuration"),
                ):
                    discover(env, root / "discovery")
                ssh.assert_not_called()
                self.assertEqual(trust.read_text(), KNOWN_HOSTS)
            with (
                patch("narwhal.deployment.discovery.SSH", FakeInspectionSSH),
                redirect_stdout(io.StringIO()),
            ):
                discover(env, root / "discovery")
            manifest = json.loads((root / "discovery/manifest.json").read_text())
            self.assertEqual(
                manifest["files"][str(trust)], hashlib.sha256(KNOWN_HOSTS.encode()).hexdigest()
            )
            self.assertNotIn(
                "NARWHAL_SSH_KNOWN_HOSTS", (root / "config/deployment.env").read_text()
            )

    def test_discovery_checks_every_checkpoint_against_the_expected_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            for name, value in (
                ("NARWHAL_MODEL_TREE_SHA256", ""),
                ("NARWHAL_MODEL_TREE_SHA256", "sha256:" + "c" * 64),
                ("NARWHAL_NODE_2_MODEL_TREE_SHA256", "C" * 64),
            ):
                with (
                    self.subTest(name=name, value=value),
                    patch.dict(env, {name: value}),
                    patch("narwhal.deployment.discovery.SSH") as ssh,
                    self.assertRaisesRegex(ValueError, "set NARWHAL_MODEL_TREE_SHA256 to the"),
                ):
                    discover(env, root / "discovery")
                ssh.assert_not_called()
            env["NARWHAL_MODEL_TREE_SHA256"] = "e" * 64
            with (
                patch("narwhal.deployment.discovery.SSH", FakeInspectionSSH),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(
                    ValueError,
                    "^engine-1: checkpoint tree digest c{64} differs from "
                    "NARWHAL_MODEL_TREE_SHA256; inspect .*engine-1-checkpoint.json$",
                ),
            ):
                discover(env, root / "discovery")
            self.assertFalse(Path(env["NARWHAL_FLEET"]).exists())

    def test_remote_probe_is_valid_standalone_python(self):
        compile(PROBE, "remote discovery probe", "exec")

    def test_probe_applies_the_launcher_convolutional_state_rule(self):
        rule = inspect.getsource(conv_state_layers)
        self.assertTrue(PROBE.startswith(rule))
        namespace = {}
        exec(compile(rule, "remote discovery rule", "exec"), namespace)
        model = {
            "text_config": {
                "linear_conv_kernel_dim": 4,
                "layer_types": ["linear_attention", "linear_attention", "full_attention"],
            }
        }
        self.assertTrue(namespace["conv_state_layers"](model))
        self.assertFalse(namespace["conv_state_layers"]({"layer_types": ["full"]}))

    def test_probe_maps_rocm_pci_ids_and_filters_image_environment(self):
        inputs = {
            "image": "sha256:" + "a" * 64,
            "model_dir": "/synthetic-model",
            "run_dir": "/synthetic-runs",
            "interface": "fabric0",
            "environment_prefixes": ["VLLM_", "NIXL_"],
            "managed_environment": ["VLLM_API_KEY"],
            "packages": "vllm|nixl-rocm",
        }
        model = (
            b'{"torch_dtype":"bfloat16","max_position_embeddings":32768,'
            b'"auto_map":{"AutoConfig":"configuration_custom.CustomConfig"},'
            b'"text_config":{"linear_attn_config":{"kda_layers":[1,2],"short_conv_kernel_size":4}}}'
        )
        image = [
            {
                "Id": inputs["image"],
                "Config": {
                    "Env": [
                        "VLLM_API_KEY=private",
                        "NIXL_SECRET=private",
                        "VLLM_ROCM_USE_AITER=1",
                        "PATH=/bin",
                    ]
                },
            }
        ]
        topology = Path("/sys/class/kfd/kfd/topology/nodes")
        properties = {
            topology / "0/properties": "drm_render_minor 0\nlocation_id 0\n",
            topology / "1/properties": "drm_render_minor 129\nlocation_id 4096\n",
            topology / "2/properties": "drm_render_minor 128\nlocation_id 2048\n",
        }

        def run(args, **kwargs):
            if args[:3] == ["docker", "image", "inspect"]:
                output = json.dumps(image)
            elif args[:2] == ["docker", "run"]:
                output = '{"vllm":"1.0","nixl-rocm":"1.0"}'
                self.assertEqual(args[-1], inputs["packages"])
                self.assertIn("--rm", args)
                self.assertNotIn("--gpus", args)
            elif args == ["rocminfo"]:
                output = (
                    "Agent 1\nDevice Type: CPU\n"
                    "Agent 2\nDevice Type: GPU\nMarketing Name: test GPU\nBDFID: 2048\n"
                    "Agent 3\nDevice Type: GPU\nMarketing Name: test GPU\nBDFID: 4096\n"
                )
            elif args[0] == "ip":
                output = '[{"ifname":"fabric0"}]'
            elif args == ["hostname"]:
                output = "synthetic-host"
            else:
                self.fail(f"Unexpected remote command: {args[0]}")
            return subprocess.CompletedProcess(args, 0, output, "")

        def probe(config: bytes) -> dict:
            output = io.StringIO()
            with (
                patch("sys.stdin", io.StringIO(json.dumps(inputs))),
                patch("subprocess.run", side_effect=run),
                patch.object(Path, "exists", lambda p: str(p).startswith("/dev/")),
                patch.object(Path, "is_dir", lambda p: str(p) == inputs["run_dir"]),
                patch.object(Path, "read_bytes", lambda p: config),
                patch.object(Path, "read_text", lambda p: properties[p]),
                patch.object(
                    Path, "glob", lambda p, pattern: [topology / str(i) for i in range(3)]
                ),
                redirect_stdout(output),
            ):
                exec(compile(PROBE, "remote discovery probe", "exec"), {})
            return json.loads(output.getvalue())

        self.assertTrue(probe(b'{"vision_config": {}, "text_config": {}}')["multimodal"])
        result = probe(model)
        self.assertTrue(result["requires_trust_remote_code"])
        self.assertTrue(result["conv_state_layers"])
        self.assertFalse(result["multimodal"])
        self.assertEqual(result["image_environment"], {"VLLM_ROCM_USE_AITER": "1"})
        self.assertEqual(
            [g["device"] for g in result["gpus"]],
            [
                "/dev/dri/renderD128",
                "/dev/dri/renderD129",
            ],
        )
