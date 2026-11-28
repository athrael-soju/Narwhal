"""Role exports keep workstation access credentials local and preserve shell literals."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from narwhal.deployment.role_env import ENGINE_FIELDS, select_values, write_environment
from narwhal.serving.control_token import ROUTER_TOKEN_ENV
from tests import CONTROL_TOKEN


class HostEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "NARWHAL_NODE_1_URL": "http://engine.example.invalid:8000",
            "NARWHAL_NODE_1_ATTESTATION_URL": "http://engine.example.invalid:8010/v1/attestation",
            "NARWHAL_NODE_2_URL": "http://peer.example.invalid:8000",
            "NARWHAL_NODE_1_IP": "192.0.2.1",
            "NARWHAL_NODE_2_IP": "192.0.2.2",
            "NARWHAL_NODE_1_SSH": "operator@engine.example.invalid",
            "NARWHAL_NODE_1_SSH_PASSWORD": "synthetic-management-secret",
            "NARWHAL_ROUTER_SSH_PASSWORD": "synthetic-router-secret",
            "NARWHAL_SSH_KNOWN_HOSTS": "config/ssh.known_hosts",
            ROUTER_TOKEN_ENV: CONTROL_TOKEN,
            "SSH_AUTH_SOCK": "/synthetic/agent",
            "UNRELATED_TOKEN": "synthetic-unrelated-secret",
            "ENGINE_TOKEN": "synthetic-api-token",
        }
        self.env.update({f"NARWHAL_{field}": "synthetic" for field in ENGINE_FIELDS})
        self.fleet = {
            "engines": [
                {
                    "url": "${NARWHAL_NODE_1_URL}",
                    "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
                },
                {"url": "${NARWHAL_NODE_2_URL}"},
            ],
            "engine": {"engine_api_key_env": "ENGINE_TOKEN"},
        }

    def test_role_exports_exclude_management_and_unrelated_credentials(self):
        for role, node in (("router", None), ("engine", 1)):
            values = select_values(role, node, self.fleet, self.env)
            self.assertFalse(any("SSH" in name for name in values))
            self.assertNotIn("UNRELATED_TOKEN", values)
            self.assertEqual(values["ENGINE_TOKEN"], self.env["ENGINE_TOKEN"])
        router = select_values("router", None, self.fleet, self.env)
        self.assertNotIn("NARWHAL_ENGINE_IMAGE", router)
        self.assertEqual(router["NARWHAL_FLEET"], "runs/deployment/fleet.json")
        self.assertEqual(router["NARWHAL_NODE_2_URL"], self.env["NARWHAL_NODE_2_URL"])
        engine = select_values("engine", 1, self.fleet, self.env)
        self.assertNotIn("NARWHAL_NODE_2_URL", engine)
        self.assertEqual(engine["NARWHAL_NODE_2_IP"], "192.0.2.2")

    def test_a_kubernetes_role_file_names_no_engine_api_key(self):
        env = {
            name: value
            for name, value in self.env.items()
            if name not in ("ENGINE_TOKEN", ROUTER_TOKEN_ENV)
        }
        for role, node in (("engine", 1), ("router", None)):
            values = select_values(role, node, self.fleet, env, by_reference=True)
            self.assertNotIn("ENGINE_TOKEN", values)
            self.assertNotIn("NARWHAL_ENGINE_API_KEY", values)
            self.assertNotIn(ROUTER_TOKEN_ENV, values)
        for held in ({"ENGINE_TOKEN": "k"}, {"NARWHAL_ENGINE_API_KEY": "k"}):
            with self.subTest(held), self.assertRaisesRegex(ValueError, "holds the engine API key"):
                select_values("engine", 1, self.fleet, {**env, **held}, by_reference=True)
        held = {**env, ROUTER_TOKEN_ENV: CONTROL_TOKEN}
        with self.assertRaisesRegex(ValueError, "holds the router control token"):
            select_values("router", None, self.fleet, held, by_reference=True)

    def test_only_the_router_role_file_holds_the_control_token(self):
        router = select_values("router", None, self.fleet, self.env)
        self.assertEqual(router[ROUTER_TOKEN_ENV], CONTROL_TOKEN)
        engine = select_values("engine", 1, self.fleet, self.env)
        self.assertNotIn(CONTROL_TOKEN, engine.values())
        env = {name: value for name, value in self.env.items() if name != ROUTER_TOKEN_ENV}
        with self.assertRaisesRegex(ValueError, f"{ROUTER_TOKEN_ENV} is unset"):
            select_values("router", None, self.fleet, env)

    def test_router_export_carries_the_monitored_router_pair(self):
        pair = {
            "NARWHAL_ROUTER_URL": "http://router-a:8000,http://router-b:8000",
            "NARWHAL_TARGETS_URL": "http://balancer:8000",
        }
        router = select_values("router", None, self.fleet, {**self.env, **pair})
        self.assertEqual({name: router[name] for name in pair}, pair)
        self.assertNotIn("NARWHAL_TARGETS_URL", select_values("router", None, self.fleet, self.env))

    def test_engine_export_carries_a_model_file_with_its_pinned_hash(self):
        engine = select_values("engine", 1, self.fleet, self.env)
        self.assertNotIn("NARWHAL_MODEL_PATH", engine)
        self.assertNotIn("NARWHAL_MODEL_SHA256", engine)
        self.env.update(
            NARWHAL_MODEL_PATH="/models/synthetic/weights.gguf",
            NARWHAL_NODE_1_MODEL_SHA256="9" * 64,
        )
        engine = select_values("engine", 1, self.fleet, self.env)
        self.assertEqual(engine["NARWHAL_MODEL_PATH"], "/models/synthetic/weights.gguf")
        self.assertEqual(engine["NARWHAL_MODEL_SHA256"], "9" * 64)
        with self.assertRaisesRegex(ValueError, "set NARWHAL_MODEL_PATH and NARWHAL_MODEL_SHA256"):
            select_values("engine", 2, self.fleet, self.env)

    def test_engine_export_names_the_neutral_side_channel_port(self):
        self.fleet["engine"]["backend"] = "sglang"
        self.env["NARWHAL_NODE_1_KV_SIDE_CHANNEL_PORT"] = "5601"
        engine = select_values("engine", 1, self.fleet, self.env)
        self.assertEqual(engine["NARWHAL_KV_SIDE_CHANNEL_PORT"], "5601")
        self.assertNotIn("NARWHAL_SGLANG_BOOTSTRAP_PORT", engine)

    def test_engine_export_names_the_helper_snapshots_of_its_backend(self):
        helpers = {
            None: {"NARWHAL_CACHE_CAPTURE_HOOK"},
            "trtllm": {"NARWHAL_CACHE_CAPTURE_HOOK"},
            "sglang": {"NARWHAL_TRANSFER_MEASURE_TOOL"},
        }
        for backend, names in helpers.items():
            with self.subTest(backend=backend):
                self.fleet["engine"].pop("backend", None)
                if backend:
                    self.fleet["engine"]["backend"] = backend
                engine = select_values("engine", 1, self.fleet, self.env)
                paths = {
                    name: engine[name]
                    for name in engine
                    if name.endswith(("_TOOL", "_HOOK")) and name.startswith("NARWHAL_")
                }
                self.assertEqual(set(paths), names)
                for path in paths.values():
                    self.assertRegex(path, r"^runs/deployment-tools/[a-z_]+\.py$")
                self.assertNotIn("NARWHAL_DEPLOYMENT_REVISION", engine)

    def test_node_override_preserves_shared_defaults_for_other_hosts(self):
        self.env["NARWHAL_NODE_2_MODEL_DIR"] = "/synthetic/second-model"
        first = select_values("engine", 1, self.fleet, self.env)
        second = select_values("engine", 2, self.fleet, self.env)
        self.assertEqual(first["NARWHAL_MODEL_DIR"], "synthetic")
        self.assertEqual(second["NARWHAL_MODEL_DIR"], "/synthetic/second-model")

    def test_missing_input_reports_variable_name(self):
        del self.env["NARWHAL_MODEL_DIR"]
        with self.assertRaisesRegex(ValueError, "NARWHAL_MODEL_DIR is unset or empty"):
            select_values("engine", 1, self.fleet, self.env)
        self.env["NARWHAL_NODE_2_URL"] = ""
        with self.assertRaisesRegex(ValueError, "NARWHAL_NODE_2_URL is unset or empty"):
            select_values("router", None, self.fleet, self.env)

    def test_fleet_reference_cannot_export_ssh_credentials(self):
        for field in ("url", "engine_api_key_env"):
            fleet = json.loads(json.dumps(self.fleet))
            if field == "url":
                fleet["engines"][0][field] = "${NARWHAL_NODE_1_SSH_PASSWORD}"
            else:
                fleet["engine"][field] = "NARWHAL_NODE_1_SSH_PASSWORD"
            with self.assertRaisesRegex(ValueError, "deployment variable names"):
                select_values("router", None, fleet, self.env)

    def test_shell_literals_permissions_and_existing_files(self):
        values = {"ENGINE_TOKEN": "spaces 'quotes' $HOME $(exit 7) `exit 8`\nnext line"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env.router"
            write_environment(path, values)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            code = 'import json,os; print(json.dumps(os.environ["ENGINE_TOKEN"]))'
            result = subprocess.run(
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    '. "$1"; python3 -c "$2"',
                    "test",
                    str(path),
                    code,
                ],
                env={"PATH": os.environ["PATH"]},
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout), values["ENGINE_TOKEN"])
            with self.assertRaises(FileExistsError):
                write_environment(path, {"ENGINE_TOKEN": "replacement"})
            self.assertIn("next line", path.read_text())
