"""Exercise the offline fleet commands before engine or artifact provisioning."""

import asyncio
import builtins
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from narwhal.config import FleetConfig
from narwhal.config.cli import main
from narwhal.config.inspection import inspect_config
from narwhal.contracts import EFFECTIVE_CONFIG, validate_document
from narwhal.serving.app import create_app


@contextmanager
def offline_guard():
    """Fail at network, process, credential-resolution and filesystem-write boundaries."""
    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def read_only(open_function):
        def guarded(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"filesystem write: {file}")
            return open_function(file, mode, *args, **kwargs)

        return guarded

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError(f"filesystem write: {path}")
        return original_os_open(path, flags, *args, **kwargs)

    with ExitStack() as stack:
        for target in (
            "socket.socket",
            "socket.create_connection",
            "socket.getaddrinfo",
            "httpx.Client",
            "httpx.AsyncClient",
            "subprocess.Popen",
            "os.system",
            "os.mkdir",
            "os.unlink",
            "os.remove",
            "os.rename",
            "os.replace",
            "os.rmdir",
            "os.chmod",
            "os.truncate",
            "os.link",
            "os.symlink",
            "narwhal.config.FleetConfig.resolve_engine_key",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError(target)))
        stack.enter_context(patch("builtins.open", read_only(original_open)))
        stack.enter_context(patch("io.open", read_only(original_io_open)))
        stack.enter_context(patch("os.open", guarded_os_open))
        yield


class ConfigCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config/fleet.json"
        self.raw = {
            "schema": "narwhal.fleet",
            "schema_version": 1,
            "model": "offline-model",
            "engines": [{"iid": "engine", "url": "http://127.0.0.1:1/"}],
            "slo": {"ttft_s": 1, "tpot_s": 0.05},
            "engine": {"engine_api_key_env": "CONFIG_TEST_KEY"},
        }
        self.path.write_text(json.dumps(self.raw))

    def run_command(self, action, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with offline_guard(), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([action, "--fleet", str(self.path), *options])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_validate_and_inspect_use_only_reads_before_provisioning(self):
        for action in ("validate", "inspect"):
            for credential in (None, "synthetic-secret-never-print"):
                env = {} if credential is None else {"CONFIG_TEST_KEY": credential}
                with (
                    self.subTest(action=action, credential=credential),
                    patch.dict(os.environ, env, clear=True),
                ):
                    status, stdout, stderr = self.run_command(action, "--format", "json")
                self.assertEqual(status, 0, stderr)
                self.assertEqual(stderr, "")
                result = json.loads(stdout)
                self.assertEqual(result["operation"], f"config {action}")
                self.assertEqual(result["exit_code"], 0)
                data = result["data"]
                self.assertEqual(validate_document(data, EFFECTIVE_CONFIG), 1)
                self.assertEqual(data["scope"], "fleet_file")
                self.assertEqual(
                    data["settings"]["engine"]["engine_api_key_env"], "CONFIG_TEST_KEY"
                )
                self.assertNotIn("synthetic-secret-never-print", stdout + stderr)
        self.assertEqual(
            sorted(path.relative_to(self.root) for path in self.root.rglob("*")),
            [Path("config"), Path("config/fleet.json")],
        )

    def test_inspect_resolves_endpoints_defaults_and_working_directory_paths(self):
        self.raw["engines"][0]["url"] = "${CONFIG_TEST_URL}"
        self.raw["engines"][0]["attestation_url"] = "${CONFIG_TEST_ATTESTATION}"
        self.raw["profiles"] = {"path": "artifacts/profiles.json"}
        self.raw["recovery"] = {"state_path": str(self.root / "private/state.json")}
        self.path.write_text(json.dumps(self.raw))
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(
                os.environ,
                {
                    "CONFIG_TEST_URL": "http://[::1]:1/",
                    "CONFIG_TEST_ATTESTATION": "http://[::1]:2/attestation",
                },
            ):
                first = self.run_command("inspect", "--format", "json")
                second = self.run_command("inspect", "--format", "json")
        finally:
            os.chdir(previous)
        self.assertEqual(first, second)
        status, output, stderr = first
        self.assertEqual(status, 0, stderr)
        data = json.loads(output)["data"]
        engine = data["settings"]["engines"][0]
        self.assertEqual(engine["url"], "http://[::1]:1")
        self.assertEqual(engine["attestation_url"], "http://[::1]:2/attestation")
        self.assertEqual(engine["role"], "decode")
        self.assertIs(engine["pin"], False)
        self.assertIsNone(engine["shared_device"])
        self.assertEqual(data["settings"]["engine"]["control_connections"], 4)
        self.assertEqual(data["settings"]["serving"]["max_connections"], 512)
        self.assertEqual(data["working_directory"], str(self.root))
        self.assertEqual(
            data["artifact_paths"],
            {
                "profiles": str(self.root / "artifacts/profiles.json"),
                "state": str(self.root / "private/state.json"),
                "journal": str(self.root / "artifacts/journal.jsonl"),
            },
        )

    def test_invalid_configuration_preserves_loader_field_errors(self):
        candidates = []
        for section, field, value, message in (
            (None, "schema_version", 99, "schema version 99"),
            (None, "unknown", 1, "unknown key 'unknown'"),
            ("engine", "control_connections", -1, "engine.control_connections"),
            ("serving", "max_connections", "4", "serving.max_connections must be an integer"),
            ("serving", "queue_capacity", 1, "serving.queue_capacity requires"),
        ):
            raw = copy.deepcopy(self.raw)
            target = raw if section is None else raw.setdefault(section, {})
            target[field] = value
            candidates.append((raw, message))
        raw = copy.deepcopy(self.raw)
        raw["engines"][0]["url"] = "${CONFIG_UNSET_ENGINE}"
        candidates.append((raw, "engines[0].url: environment variable CONFIG_UNSET_ENGINE"))
        for raw, message in candidates:
            self.path.write_text(json.dumps(raw))
            for action in ("validate", "inspect"):
                with (
                    self.subTest(action=action, message=message),
                    patch.dict(os.environ, {}, clear=True),
                ):
                    status, stdout, stderr = self.run_command(action, "--format", "json")
                self.assertEqual(status, 2, stdout + stderr)
                result = json.loads(stdout)
                self.assertEqual(result["exit_code"], status)
                self.assertIn(message, json.dumps(result["errors"]))

    def test_inspection_matches_serving_connection_and_admission_limits(self):
        for count, explicit in ((1, 0), (3, 0), (3, 11)):
            with self.subTest(count=count, explicit=explicit):
                raw = copy.deepcopy(self.raw)
                raw["engines"] = [
                    {"iid": f"engine-{number}", "url": f"http://127.0.0.1:{number + 1}"}
                    for number in range(count)
                ]
                raw["engine"] = {"control_connections": explicit}
                raw["profiles"] = {"path": str(self.root / "profiles.json")}
                raw["serving"] = {
                    "max_connections": 17,
                    "queue_capacity": 3,
                    "queue_timeout_s": 1,
                    "prefill_concurrency": 2,
                    "decode_concurrency": 2,
                    "handoff_timeout_s": 1,
                }
                self.path.write_text(json.dumps(raw))
                config = FleetConfig.load(self.path)
                data = inspect_config(config, self.path)
                app = create_app(config)
                router = app.state.router
                try:
                    state = router.state()
                    self.assertEqual(
                        data["derived"]["control_connections"],
                        state["http_pools"]["control_connections"],
                    )
                    self.assertEqual(
                        data["derived"]["control_connections"], explicit or max(4, count * 2)
                    )
                    self.assertEqual(data["derived"]["max_concurrent"], state["admission"]["limit"])
                    self.assertEqual(
                        data["derived"]["http_retained_limit"],
                        state["serving"]["http_retained_limit"],
                    )
                    self.assertEqual(
                        data["artifact_paths"]["journal"], str(router.journal.path.resolve())
                    )
                finally:
                    asyncio.run(router.engines.aclose())

    def test_default_inspection_prints_the_versioned_document(self):
        status, stdout, stderr = self.run_command("inspect")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(validate_document(json.loads(stdout), EFFECTIVE_CONFIG), 1)

    def test_text_failures_identify_the_fleet_and_loader_error(self):
        self.path.write_text('{"schema": "narwhal.fleet", "schema_version": 99}')
        for action in ("validate", "inspect"):
            with self.subTest(action=action):
                status, stdout, stderr = self.run_command(action)
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn(str(self.path), stderr)
                self.assertIn("schema version 99", stderr)

    def test_artifact_paths_preserve_literal_home_and_environment_syntax(self):
        self.raw["profiles"] = {"path": "~/profiles.json"}
        self.raw["recovery"] = {"state_path": "${CONFIG_STATE_DIR}/state.json"}
        self.path.write_text(json.dumps(self.raw))
        with patch.dict(os.environ, {"CONFIG_STATE_DIR": str(self.root)}):
            status, stdout, stderr = self.run_command("inspect")
        self.assertEqual(status, 0, stderr)
        paths = json.loads(stdout)["artifact_paths"]
        self.assertEqual(paths["profiles"], str(Path.cwd() / "~/profiles.json"))
        self.assertEqual(paths["state"], str(Path.cwd() / "${CONFIG_STATE_DIR}/state.json"))

    def test_help_identifies_offline_workflow_and_live_preflight(self):
        stdout = io.StringIO()
        with offline_guard(), redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("filesystem and environment reads", " ".join(stdout.getvalue().split()))
        self.assertIn("narwhal-check", stdout.getvalue())
