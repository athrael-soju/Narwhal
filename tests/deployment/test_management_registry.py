"""Validate management registration before a server exposes target operations."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from pydantic import ValidationError

from narwhal.deployment.management_registry import MAX_REGISTRY_BYTES, load_registry


def registry_document() -> dict:
    return {
        "schema": "narwhal.management-registry",
        "schema_version": 1,
        "registry_id": "10000000-0000-4000-8000-000000000001",
        "state_dir": "/srv/narwhal/runs/management",
        "targets": [
            {
                "id": "local-dev",
                "kind": "dev",
                "working_directory": "/srv/narwhal",
                "artifact_root": "/srv/narwhal/runs/dev-evidence",
                "fleet_file": None,
                "instance_dir": "/srv/narwhal/runs/dev",
                "adapter": {"id": "local-dev-v1", "settings_path": None},
                "endpoints": {"router_env": "DEV_ROUTER_URL"},
                "credential_env": ["NARWHAL_ENGINE_API_KEY"],
                "capabilities": ["inspect", "measure", "mutate"],
                "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
                "recipes": [
                    {
                        "id": "small-cuda",
                        "kind": "dev",
                        "path": "/srv/narwhal/config/dev-recipe.json",
                    }
                ],
            }
        ],
    }


class ManagementRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "registry.json"

    def write(self, document: object) -> Path:
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.path.chmod(0o600)
        return self.path

    def test_documented_registration_loads_defaults_without_accessing_inputs(self):
        document = registry_document()
        document["state_dir"] = str(self.root / "state-not-created")
        with patch.dict(os.environ, {}, clear=True):
            registry = load_registry(self.write(document))
        self.assertEqual(registry.registry_id, UUID(document["registry_id"]))
        self.assertFalse(registry.state_dir.exists())
        target = registry.targets[0]
        self.assertEqual(target.id, "local-dev")
        self.assertEqual(target.instance_dir, Path("/srv/narwhal/runs/dev"))
        self.assertEqual(target.endpoints.router_env, "DEV_ROUTER_URL")
        self.assertEqual(target.credential_env, ("NARWHAL_ENGINE_API_KEY",))
        self.assertEqual(target.freshness_s, 60)
        self.assertFalse(target.allow_request_content)
        self.assertEqual(target.queries, ())
        self.assertEqual(target.logs, ())
        self.assertEqual(
            target.preparation.model_dump(),
            {
                "timeout_ms": 300000,
                "term_grace_ms": 10000,
                "kill_grace_ms": 5000,
                "reconcile_ms": 30000,
            },
        )
        self.assertEqual(
            registry.model_dump(mode="json", by_alias=True)["schema"], document["schema"]
        )

    def test_minimal_target_fills_independent_defaults_and_is_immutable(self):
        document = registry_document()
        for key in ("endpoints", "credential_env", "capabilities", "actions", "recipes"):
            del document["targets"][0][key]
        registry = load_registry(self.write(document))
        target = registry.targets[0]
        self.assertEqual(target.capabilities, ("inspect",))
        self.assertEqual(target.actions, ())
        self.assertEqual(target.recipes, ())
        self.assertEqual(target.credential_env, ())
        self.assertIsNone(target.endpoints.router_env)
        with self.assertRaises(ValidationError):
            target.id = "changed"
        with self.assertRaises(ValidationError):
            target.preparation.timeout_ms = 1

    def test_fleet_accepts_future_paths_queries_logs_and_partial_preparation(self):
        document = registry_document()
        target = document["targets"][0]
        target.update(
            kind="fleet",
            fleet_file="/future/fleet.json",
            instance_dir=None,
            adapter={"id": "ssh-v1", "settings_path": "/future/ssh.json"},
            actions=["fleet_deploy", "fleet_profile", "deployment_cleanup"],
            recipes=[{"id": "recipe", "kind": "fleet", "path": "/future/recipe.json"}],
            queries=[{"id": "health", "expression": "up", "kind": "instant"}],
            logs=[{"id": "engine", "host_id": "gpu-1", "source": "/var/log/engine.log"}],
            preparation={"timeout_ms": 1234},
        )
        registry = load_registry(self.write(document))
        fleet = registry.targets[0]
        self.assertEqual(fleet.fleet_file, Path("/future/fleet.json"))
        self.assertEqual(fleet.adapter.settings_path, Path("/future/ssh.json"))
        self.assertEqual(fleet.queries[0].expression, "up")
        self.assertEqual(fleet.logs[0].host_id, "gpu-1")
        self.assertEqual(fleet.preparation.timeout_ms, 1234)
        self.assertEqual(fleet.preparation.term_grace_ms, 10000)

    def test_invalid_contract_types_extra_keys_and_identifiers_are_rejected(self):
        mutations = [
            {"schema": "private-schema-marker"},
            {"schema_version": 2},
            {"schema_version": True},
            {"schema_version": "1"},
            {"registry_id": "private-invalid-uuid-marker"},
            {"state_dir": "relative/state"},
            {"state_dir": 12},
            {"unknown": "private-value-marker"},
            {"targets": {}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                document = registry_document()
                document.update(mutation)
                with self.assertRaises(ValueError) as error:
                    load_registry(self.write(document))
                self.assertNotIn("private-", str(error.exception))
        for value in ([], None, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_registry(self.write(value))

    def test_target_types_and_nested_unknown_fields_are_strict(self):
        mutations = [
            {"id": "bad/id"},
            {"id": "id\n"},
            {"id": "a" * 65},
            {"kind": "other"},
            {"working_directory": "~"},
            {"artifact_root": "relative"},
            {"fleet_file": "relative.json"},
            {"instance_dir": "/bad\x00path"},
            {"unknown": True},
            {"freshness_s": True},
            {"freshness_s": "60"},
            {"freshness_s": 0},
            {"freshness_s": 3601},
            {"allow_request_content": 1},
            {"capabilities": ["admin"]},
            {"actions": ["arbitrary_shell"]},
            {"credential_env": ["INVALID-NAME"]},
            {"credential_env": ["VALID\n"]},
            {"credential_env": "TOKEN"},
            {"endpoints": {"router_env": "https://example.invalid"}},
            {"endpoints": {"url": "https://example.invalid"}},
            {"preparation": {"timeout_ms": True}},
            {"preparation": {"timeout_ms": 0}},
            {"preparation": {"kill_grace_ms": 86400001}},
            {"preparation": {"unknown": 1}},
            {"preparation": None},
            {"queries": [{"id": "q", "expression": "", "kind": "instant"}]},
            {"queries": [{"id": "q", "expression": "up", "kind": "other"}]},
            {"queries": [{"id": "q", "expression": "x" * 4097, "kind": "range"}]},
            {"logs": [{"id": "log", "host_id": "local", "source": "relative.log"}]},
            {"adapter": {"id": "local-dev-v1", "settings_path": None, "unknown": 1}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                document = registry_document()
                document["targets"][0].update(mutation)
                with self.assertRaises(ValueError):
                    load_registry(self.write(document))

    def test_required_target_fields_cannot_be_omitted(self):
        for field in (
            "id",
            "kind",
            "working_directory",
            "artifact_root",
            "fleet_file",
            "instance_dir",
            "adapter",
        ):
            with self.subTest(field=field):
                document = registry_document()
                del document["targets"][0][field]
                with self.assertRaises(ValueError):
                    load_registry(self.write(document))

    def test_incompatible_dev_and_fleet_targets_are_rejected(self):
        changes = [
            {"instance_dir": None},
            {"adapter": {"id": "ssh-v1", "settings_path": "/site.json"}},
            {"recipes": [{"id": "r", "kind": "fleet", "path": "/recipe.json"}]},
            {"actions": ["fleet_deploy"]},
            {"logs": [{"id": "log", "host_id": "remote", "source": "/log.txt"}]},
            {"kind": "fleet", "recipes": [], "actions": []},
        ]
        for change in changes:
            with self.subTest(change=change):
                document = registry_document()
                document["targets"][0].update(change)
                with self.assertRaises(ValueError):
                    load_registry(self.write(document))
        for change in (
            {"fleet_file": None},
            {"instance_dir": "/instance"},
            {"adapter": {"id": "ssh-v1", "settings_path": None}},
            {"actions": ["dev_up"]},
        ):
            with self.subTest(fleet_change=change):
                document = registry_document()
                document["targets"][0].update(
                    kind="fleet",
                    fleet_file="/fleet.json",
                    instance_dir=None,
                    adapter={"id": "ssh-v1", "settings_path": "/site.json"},
                    recipes=[],
                    actions=[],
                )
                document["targets"][0].update(change)
                with self.assertRaises(ValueError):
                    load_registry(self.write(document))

    def test_duplicate_aliases_and_grants_are_rejected(self):
        for field, value in (
            ("credential_env", "TOKEN"),
            ("capabilities", "inspect"),
            ("actions", "dev_up"),
            ("recipes", {"id": "r", "kind": "dev", "path": "/recipe.json"}),
            ("queries", {"id": "q", "expression": "up", "kind": "instant"}),
            ("logs", {"id": "l", "host_id": "local", "source": "/log.txt"}),
        ):
            with self.subTest(field=field):
                document = registry_document()
                document["targets"][0][field] = [value, copy.deepcopy(value)]
                with self.assertRaises(ValueError):
                    load_registry(self.write(document))
        document = registry_document()
        document["targets"].append(copy.deepcopy(document["targets"][0]))
        with self.assertRaises(ValueError):
            load_registry(self.write(document))

    def test_collection_limits_reject_unique_entries_over_the_limit(self):
        for field, values in (
            ("credential_env", [f"TOKEN_{i}" for i in range(65)]),
            ("recipes", [{"id": f"r{i}", "kind": "dev", "path": "/r.json"} for i in range(101)]),
            (
                "queries",
                [{"id": f"q{i}", "expression": "up", "kind": "instant"} for i in range(101)],
            ),
            ("logs", [{"id": f"l{i}", "host_id": "local", "source": "/log"} for i in range(101)]),
        ):
            with self.subTest(field=field):
                document = registry_document()
                document["targets"][0][field] = values
                with self.assertRaises(ValueError):
                    load_registry(self.write(document))
        document = registry_document()
        target = document["targets"][0]
        document["targets"] = [{**target, "id": f"target{i}"} for i in range(101)]
        with self.assertRaises(ValueError):
            load_registry(self.write(document))

    def test_registry_requires_current_owner_and_exact_private_mode(self):
        self.write(registry_document())
        for mode in (0o400, 0o640, 0o660, 0o644, 0o700):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaises(ValueError):
                    load_registry(self.path)
        self.path.chmod(0o600)
        metadata = self.path.stat()
        wrong_owner = SimpleNamespace(
            st_uid=os.getuid() + 1, st_mode=metadata.st_mode, st_size=metadata.st_size
        )
        with (
            patch("narwhal.deployment.management_registry.os.fstat", return_value=wrong_owner),
            self.assertRaisesRegex(ValueError, "owned by the current user"),
        ):
            load_registry(self.path)

    def test_symlinks_and_nonregular_files_are_rejected_without_blocking(self):
        self.write(registry_document())
        link = self.root / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            load_registry(link)
        fifo = self.root / "registry.pipe"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ValueError, "regular file"):
            load_registry(fifo)
        with self.assertRaises((OSError, ValueError)):
            load_registry(self.root)

    def test_size_limit_is_enforced_before_and_during_read(self):
        self.path.write_bytes(b" " * (MAX_REGISTRY_BYTES + 1))
        self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "size limit"):
            load_registry(self.path)
        metadata = self.path.stat()
        earlier_size = SimpleNamespace(st_uid=os.getuid(), st_mode=metadata.st_mode, st_size=0)
        with (
            patch("narwhal.deployment.management_registry.os.fstat", return_value=earlier_size),
            self.assertRaisesRegex(ValueError, "size limit"),
        ):
            load_registry(self.path)

    def test_invalid_json_and_duplicate_keys_do_not_expose_input(self):
        for payload in (
            b'{"secret-marker":',
            b'{"secret-marker": 1, "secret-marker": 2}',
            b"\xff",
        ):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                self.path.chmod(0o600)
                with self.assertRaises(ValueError) as error:
                    load_registry(self.path)
                self.assertNotIn("secret-marker", str(error.exception))
