"""Execute the documented supervisor configuration against private synthetic inputs."""

import configparser
import contextlib
import hashlib
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
GUIDE = (ROOT / "docs/Deploy.md").read_text()
SCRIPT = GUIDE.split("<<'PY_SUPERVISOR_CONFIG'\n", 1)[1].split("\nPY_SUPERVISOR_CONFIG", 1)[0]


class DeploymentSupervisorTests(unittest.TestCase):
    def inputs(self, root):
        engine = root / "launch"
        engine.mkdir()
        supervisor = root / "supervisor"
        supervisor.mkdir()
        checkout = root / "checkout with spaces"
        (checkout / ".venv/bin").mkdir(parents=True)
        (checkout / ".venv/bin/narwhal-attest").touch()
        document = root / "document with % and spaces.json"
        document.write_text("{}")
        launch = json.dumps({"endpoint": "http://127.0.0.1:9000"}).encode()
        (engine / "launch.json").write_bytes(launch)
        (engine / "checked.json").write_text(
            json.dumps({"plan_sha256": hashlib.sha256(launch).hexdigest()})
        )
        (engine / "container.id").write_text("a" * 64 + "\n")
        return checkout, {
            "ENGINE_RUN": str(engine),
            "ENGINE_CONTAINER": "a" * 64,
            "SUPERVISOR_RUN": str(supervisor),
            "ATTEST_DOCUMENT": str(document),
            "ATTEST_BASE": "http://127.0.0.1:9010",
            "NARWHAL_SSH_PASSWORD": "synthetic-management-secret",
        }

    def execute(self, checkout, env):
        with (
            patch.dict("os.environ", env),
            patch.object(Path, "cwd", return_value=checkout),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(compile(SCRIPT, "docs/Deploy.md:PY_SUPERVISOR_CONFIG", "exec"), {})

    def test_private_configuration_binds_existing_plan_and_quotes_arguments(self):
        with tempfile.TemporaryDirectory(prefix="sv-") as folder:
            checkout, env = self.inputs(Path(folder))
            self.execute(checkout, env)
            run = Path(env["SUPERVISOR_RUN"])
            config_path = run / "supervisord.conf"
            config = configparser.ConfigParser()
            config.read(config_path)
            program = config["program:attestation"]
            self.assertEqual(
                shlex.split(program["command"]),
                [
                    str(checkout / ".venv/bin/narwhal-attest"),
                    "--document",
                    env["ATTEST_DOCUMENT"],
                    "--engine-base",
                    "http://127.0.0.1:9000",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9010",
                ],
            )
            self.assertFalse(program.getboolean("autostart"))
            self.assertTrue(program.getboolean("autorestart"))
            self.assertTrue(program.getboolean("stopasgroup"))
            self.assertEqual(config["unix_http_server"]["chmod"], "0600")
            self.assertNotIn("inet_http_server", config)
            self.assertNotIn(env["NARWHAL_SSH_PASSWORD"], config_path.read_text())
            record = json.loads((run / "binding.json").read_text())
            self.assertEqual(record["container_id"], env["ENGINE_CONTAINER"])
            self.assertEqual(record["supervisor_version"], "4.3.0")
            location = Path(env["ENGINE_RUN"]) / "supervisor-location.txt"
            self.assertEqual(location.read_text().strip(), str(run))
            for path in (config_path, run / "binding.json", location):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            retained = config_path.read_bytes()
            with self.assertRaisesRegex(SystemExit, "Reuse the supervisor"):
                self.execute(checkout, env)
            self.assertEqual(config_path.read_bytes(), retained)

    def test_mismatched_plan_container_or_url_stops_before_configuration_write(self):
        for bad in ("plan", "container", "url", "command"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory(prefix="sv-") as folder:
                checkout, env = self.inputs(Path(folder))
                engine = Path(env["ENGINE_RUN"])
                if bad == "plan":
                    (engine / "launch.json").write_text('{"endpoint": "http://changed:9000"}')
                elif bad == "container":
                    env["ENGINE_CONTAINER"] = "b" * 64
                elif bad == "url":
                    env["ATTEST_BASE"] += "/v1/attestation"
                else:
                    doc = Path(env["ATTEST_DOCUMENT"])
                    unsafe = doc.with_name("document;other.json")
                    doc.rename(unsafe)
                    env["ATTEST_DOCUMENT"] = str(unsafe)
                with self.assertRaises(SystemExit):
                    self.execute(checkout, env)
                self.assertFalse((Path(env["SUPERVISOR_RUN"]) / "supervisord.conf").exists())


if __name__ == "__main__":
    unittest.main()
