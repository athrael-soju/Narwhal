"""Start deployment preparation from environment values and fresh host observations."""

import copy
import io
import json
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.deployment.deploy_hosts import prepare
from tools.deployment.discover_deployment import (
    PROBE,
    build_records,
    derive_hosts,
    discover,
    fabric_address,
    service_url,
)
from tools.deployment.engine_launch import load_launches
from tools.deployment.host_access import SSH, load_hosts
from tools.deployment.launch_engine import build
from tools.deployment.prepare_host_env import select_values

ROOT = Path(__file__).resolve().parents[2]


def observation(runtime="rocm", address="10.0.0.1"):
    return {
        "hostname": "synthetic-host",
        "image_id": "sha256:" + "a" * 64,
        "model_sha256": "b" * 64,
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


def environment(root):
    env = {
        "NARWHAL_HOSTS": str(root / "config/hosts.local.json"),
        "NARWHAL_LAUNCH_CONFIG": str(root / "config/engine-launch.local.json"),
        "NARWHAL_FLEET": str(root / "config/fleet.json"),
        "NARWHAL_SSH_KNOWN_HOSTS": str(root / "config/ssh.known_hosts"),
        "NARWHAL_ENGINE_IMAGE": "sha256:" + "a" * 64,
        "NARWHAL_ENGINE_MODEL_NAME": "synthetic-model",
        "NARWHAL_MODEL_DIR": "/models/synthetic-model",
        "NARWHAL_RUN_DIR": "/deployment-runs",
        "NARWHAL_FABRIC_INTERFACE": "fabric0",
        "NARWHAL_ENGINE_PORT": "8000",
        "NARWHAL_ATTEST_PORT": "8010",
        "NARWHAL_NIXL_SIDE_CHANNEL_PORT": "5557",
        "NARWHAL_UCX_TCP_PORT_RANGE": "20000-21000",
        "NARWHAL_DEPLOYMENT_REVISION": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
    }
    for node in (1, 2):
        env.update(
            {
                f"NARWHAL_NODE_{node}_SSH": f"test@host{node}.invalid",
                f"NARWHAL_NODE_{node}_SSH_PASSWORD": "private-test-password",
            }
        )
    return env


class FakeInspectionSSH:
    def __init__(self, env, logs, *, enroll_hosts=False):
        assert enroll_hosts
        self.calls = []
        self.trust = Path(env["NARWHAL_SSH_KNOWN_HOSTS"])

    def run(self, host, gate, script, payload=None):
        self.calls.append((host.id, gate))
        if payload:
            if gate == "hash checkpoint files":
                return json.dumps(
                    {
                        "model_tree_sha256": "c" * 64,
                        "files": [{"path": "config.json", "size": 12, "sha256": "d" * 64}],
                        "file_count": 1,
                        "bytes_verified": 12,
                    }
                )
            return json.dumps(observation(address="10.0.0." + host.id.split("-")[-1]))
        with self.trust.open("a") as f:
            f.write(f"{host.id} ssh-ed25519 synthetic-public-key\n")
        return "synthetic-host"


class DiscoveryTests(unittest.TestCase):
    def test_env_only_discovery_feeds_real_preparation_and_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            out = root / "discovery"
            self.assertFalse((root / "config").exists())
            with (
                patch("tools.deployment.discover_deployment.SSH", FakeInspectionSSH),
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
            run = root / "prepared"
            prepare(hosts, env, run, ROOT)
            for node in (1, 2):
                role = f"engine-{node}"
                role_env = select_values("engine", node, fleet, env)
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
            with patch("tools.deployment.discover_deployment.SSH") as ssh:
                with self.assertRaisesRegex(ValueError, "Archive previous"):
                    discover(env, root / "another-discovery")
                ssh.assert_not_called()
            self.assertEqual(Path(env["NARWHAL_FLEET"]).read_bytes(), old)

    def test_discovery_rejects_checkpoint_file_mismatch_before_writing_fleet(self):
        class DifferentCheckpointSSH(FakeInspectionSSH):
            def run(self, host, gate, script, payload=None):
                result = super().run(host, gate, script, payload)
                if gate == "hash checkpoint files" and host.id == "node-2":
                    record = json.loads(result)
                    record["model_tree_sha256"] = "e" * 64
                    record["files"][0]["sha256"] = "f" * 64
                    return json.dumps(record)
                return result

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            with (
                patch("tools.deployment.discover_deployment.SSH", DifferentCheckpointSSH),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(ValueError, "checkpoint file config.json differs"),
            ):
                discover(env, root / "discovery")
            self.assertFalse(Path(env["NARWHAL_FLEET"]).exists())

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
                NARWHAL_ENGINE_ARGS='["--max-model-len", "8192"]',
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
                one["runtime"]["extra_args"],
                ["--max-model-len", "8192", "--trust-remote-code"],
            )
            self.assertEqual(
                launches["engines"]["engine-2"]["runtime"]["extra_args"],
                ["--max-model-len", "8192"],
            )
            env["NARWHAL_MODEL_CONFIG_SHA256"] = "c" * 64
            with self.assertRaisesRegex(ValueError, "hash differs"):
                build_records(hosts, env, observations, root)

    def test_convolutional_transfer_layout_is_derived_before_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = environment(root)
            hosts = derive_hosts(env)
            observations = {f"engine-{i}": observation(address=f"10.0.0.{i}") for i in (1, 2)}
            observations["engine-1"]["requires_ds_conv_state_layout"] = True
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

    def test_ssh_enrollment_keeps_subsequent_connections_strict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            trust = root / "known_hosts"
            trust.write_text("existing-key\n")
            env = {"NARWHAL_SSH_KNOWN_HOSTS": str(trust)}
            enroll = SSH(env, root / "enrollment", enroll_hosts=True)
            normal = SSH(env, root / "normal")
            self.assertIn("StrictHostKeyChecking=accept-new", enroll.options)
            self.assertIn("StrictHostKeyChecking=yes", normal.options)
            self.assertEqual(trust.read_text(), "existing-key\n")

    def test_remote_probe_is_valid_standalone_python(self):
        compile(PROBE, "remote discovery probe", "exec")

    def test_probe_maps_rocm_pci_ids_and_filters_image_environment(self):
        inputs = {
            "image": "sha256:" + "a" * 64,
            "model_dir": "/synthetic-model",
            "run_dir": "/synthetic-runs",
            "interface": "fabric0",
            "environment_prefixes": ["VLLM_", "NIXL_"],
            "managed_environment": ["VLLM_API_KEY"],
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

        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(inputs))),
            patch("subprocess.run", side_effect=run),
            patch.object(Path, "exists", lambda p: str(p).startswith("/dev/")),
            patch.object(Path, "is_dir", lambda p: str(p) == inputs["run_dir"]),
            patch.object(Path, "read_bytes", lambda p: model),
            patch.object(Path, "read_text", lambda p: properties[p]),
            patch.object(Path, "glob", lambda p, pattern: [topology / str(i) for i in range(3)]),
            redirect_stdout(output),
        ):
            exec(compile(PROBE, "remote discovery probe", "exec"), {})
        result = json.loads(output.getvalue())
        self.assertTrue(result["requires_trust_remote_code"])
        self.assertTrue(result["requires_ds_conv_state_layout"])
        self.assertEqual(result["image_environment"], {"VLLM_ROCM_USE_AITER": "1"})
        self.assertEqual(
            [g["device"] for g in result["gpus"]],
            [
                "/dev/dri/renderD128",
                "/dev/dri/renderD129",
            ],
        )
