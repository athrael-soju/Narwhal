"""Validate fixed deployment inputs and the explicitly registered source tree."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from narwhal.deployment import ssh_settings as settings
from narwhal.deployment.management_records import OperationError


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def value(self, **changes):
        return {
            "schema": "narwhal.ssh-settings",
            "schema_version": 1,
            "source_root": str(self.root),
            "source_commit": "a" * 40,
            "hosts_path": str(self.root / "hosts.json"),
            "launch_path": str(self.root / "launch.json"),
            "known_hosts_path": str(self.root / "known_hosts"),
            "remote_root": "/opt/narwhal-managed",
            **changes,
        }

    def test_partial_budgets_preserve_action_defaults(self):
        value = settings.SSHSettings.model_validate(
            self.value(budgets={"engines": {"term_grace_ms": 1000}})
        )
        self.assertEqual(value.budgets.engines.timeout_ms, 3_600_000)
        self.assertEqual(value.budgets.discovery.timeout_ms, 300_000)
        self.assertEqual(value.budgets.engines.term_grace_ms, 1000)
        for change in ({"source_root": "/tmp/../other"}, {"command_timeout_s": True}):
            with self.assertRaises(ValidationError):
                settings.SSHSettings.model_validate(self.value(**change))

    def test_file_reader_rejects_boolean_and_future_versions(self):
        path = self.root / "settings.json"
        for version in (True, 2):
            path.write_text(json.dumps(self.value(schema_version=version)))
            path.chmod(0o600)
            with self.assertRaises(OperationError) as raised:
                settings._document(path, settings.SSHSettings, "narwhal.ssh-settings")
            self.assertEqual(raised.exception.code, "invalid_input")

    def test_recipes_reject_arbitrary_commands_and_reserved_environment(self):
        base = {"schema": "narwhal.ssh-recipe", "schema_version": 1}
        for changes in (
            {"client_argv": ["bash"]},
            {"environment": {"NARWHAL_NODE_1_SSH": "hidden"}},
            {"environment_refs": {"DYLD_INSERT_LIBRARIES": "REGISTERED_SECRET"}},
            {"environment_refs": {"HF_TOKEN": "PYTHONPATH"}},
            {"fabric": {"utilization": float("nan")}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                settings.SSHRecipe.model_validate({**base, **changes})
        settings.SSHRecipe.model_validate(
            {**base, "environment_refs": {"HF_TOKEN": "REGISTERED_HF_TOKEN"}}
        )
        with self.assertRaises(ValidationError):
            settings.SSHHost.model_validate(
                {"id": "one", "ssh_env": "LD_PRELOAD", "roles": ("router", "engine-1")}
            )

    def test_host_aliases_require_unique_destinations_and_roles(self):
        config = settings.SSHSettings.model_validate(self.value())
        path = Path(config.hosts_path)
        hosts = [
            {"id": "one", "ssh_env": "ONE_SSH", "roles": ["router", "engine-1"]},
            {"id": "two", "ssh_env": "TWO_SSH", "roles": ["engine-2"]},
        ]
        path.write_text(json.dumps({"hosts": hosts}))
        path.chmod(0o600)
        self.assertEqual(
            len(settings.load_hosts(config, {"ONE_SSH": "root@one", "TWO_SSH": "root@two"})),
            2,
        )
        for environment in (
            {"ONE_SSH": "root@one", "TWO_SSH": "root@one"},
            {"ONE_SSH": "-oProxyCommand=bad", "TWO_SSH": "root@two"},
        ):
            with self.assertRaises(OperationError):
                settings.load_hosts(config, environment)

    def test_source_verification_binds_commit_assets_and_rejects_extra_files(self):
        executable = shutil.which("git", path=os.defpath)
        self.assertIsNotNone(executable)

        def git(*arguments):
            return (
                subprocess.check_output(
                    [executable, "-C", str(self.root), *arguments], stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )

        for name in (*settings.ASSETS, "_narwhal_build.py", "MANIFEST.in", "src/narwhal/a.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n")
        config = self.root / "config/fleet.example.json"
        config.parent.mkdir()
        config.write_text("{}")
        (self.root / "src/narwhal/fleet.example.json").symlink_to("../../config/fleet.example.json")
        for path in self.root.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        git("init", "-q")
        git("add", ".")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        revision = git("rev-parse", "HEAD")
        value = settings.SSHSettings.model_validate(self.value(source_commit=revision))
        with patch.object(
            settings.provenance, "verified_source", return_value={"commit": revision}
        ):
            observed = settings.verify_source(value, deadline=time.monotonic() + 5)
            self.assertIn("src/narwhal/fleet.example.json", observed["assets"])
            extra = self.root / "src/narwhal/.env"
            extra.write_text("PRIVATE=must-not-be-read")
            with self.assertRaises(OperationError) as raised:
                settings.verify_source(value, deadline=time.monotonic() + 5)
            self.assertEqual(raised.exception.code, "prerequisite_failed")
            extra.unlink()
            alias = self.root / "src/narwhal/unapproved"
            alias.symlink_to(self.root / "tools", target_is_directory=True)
            with self.assertRaises(OperationError):
                settings.verify_source(value, deadline=time.monotonic() + 5)
            alias.unlink()
            (self.root / "tools/deployment/fabric_budget.py").write_text("changed")
            with self.assertRaises(OperationError):
                settings.verify_source(value, deadline=time.monotonic() + 5)
