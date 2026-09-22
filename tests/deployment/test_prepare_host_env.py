"""Role exports keep workstation access credentials local and preserve shell literals."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.deployment.prepare_host_env import ENGINE_FIELDS, select_values, write_environment


class HostEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "NARWHAL_DEPLOYMENT_REVISION": "a" * 40,
            "NARWHAL_NODE_1_URL": "http://engine.example.invalid:8000",
            "NARWHAL_NODE_1_ATTESTATION_URL": "http://engine.example.invalid:8010/v1/attestation",
            "NARWHAL_NODE_2_URL": "http://peer.example.invalid:8000",
            "NARWHAL_NODE_1_IP": "192.0.2.1",
            "NARWHAL_NODE_2_IP": "192.0.2.2",
            "NARWHAL_NODE_1_SSH": "operator@engine.example.invalid",
            "NARWHAL_NODE_1_SSH_PASSWORD": "synthetic-management-secret",
            "NARWHAL_ROUTER_SSH_PASSWORD": "synthetic-router-secret",
            "NARWHAL_SSH_KNOWN_HOSTS": "config/ssh.known_hosts",
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


if __name__ == "__main__":
    unittest.main()
