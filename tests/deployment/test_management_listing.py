"""Check private retained target pages without opening registered target inputs."""

import copy
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment import management_listing as listing
from narwhal.deployment.management_access import AccessError
from narwhal.deployment.management_registry import ManagementRegistry
from tests.deployment.test_management_registry import registry_document


class ManagementListingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = registry_document()
        self.document["state_dir"] = str(self.root / "state")
        template = self.document["targets"][0]
        self.document["targets"] = [
            {**copy.deepcopy(template), "id": name} for name in ("c", "a", "b")
        ]

    def registry(self, document=None):
        return ManagementRegistry.model_validate_json(json.dumps(document or self.document))

    def cursor_path(self, token):
        return self.root / "state" / "target-listing" / f"cursor-{token}.json"

    def test_filters_inspect_grants_sorts_targets_and_exports_only_named_fields(self):
        hidden = copy.deepcopy(self.document["targets"][0])
        hidden.update(id="hidden", capabilities=["mutate"])
        self.document["targets"].append(hidden)
        target = self.document["targets"][0]
        target.update(
            kind="fleet",
            instance_dir=None,
            fleet_file="/private/fleet-with-secrets.json",
            adapter={"id": "ssh-v1", "settings_path": "/private/credentials.json"},
            actions=["fleet_profile"],
            recipes=[{"id": "recipe", "kind": "fleet", "path": "/private/recipe"}],
            queries=[{"id": "up", "expression": "private-expression", "kind": "instant"}],
            logs=[
                {"id": "second", "host_id": "host-z", "source": "/private/z.log"},
                {"id": "first", "host_id": "host-a", "source": "/private/a.log"},
            ],
        )
        with patch(
            "narwhal.deployment.management_access.read_input",
            side_effect=AssertionError("target read"),
        ):
            result = listing.list_targets(self.registry())
        self.assertEqual([target["id"] for target in result["targets"]], ["a", "b", "c"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["targets"][0]["host_ids"], ["local"])
        fleet = result["targets"][2]
        self.assertEqual(fleet["host_ids"], ["host-a", "host-z"])
        self.assertEqual(fleet["recipes"], [{"id": "recipe", "kind": "fleet"}])
        self.assertEqual(fleet["query_ids"], ["up"])
        self.assertEqual(fleet["log_ids"], ["second", "first"])
        self.assertEqual(
            set(fleet),
            {
                "id",
                "kind",
                "capabilities",
                "actions",
                "adapter_id",
                "recipes",
                "query_ids",
                "log_ids",
                "host_ids",
            },
        )
        rendered = json.dumps(result)
        for private in (
            "/private",
            "private-expression",
            "DEV_ROUTER_URL",
            "NARWHAL_ENGINE_API_KEY",
            "hidden",
        ):
            self.assertNotIn(private, rendered)

    def test_pages_survive_registry_reconstruction_and_replay_identically(self):
        first = listing.list_targets(self.registry(), limit=1)
        token = first["next_cursor"]
        second = listing.list_targets(self.registry(), limit=1, cursor=token)
        self.assertEqual(second, listing.list_targets(self.registry(), limit=1, cursor=token))
        last = listing.list_targets(self.registry(), limit=1, cursor=second["next_cursor"])
        self.assertEqual(
            [first["targets"][0]["id"], second["targets"][0]["id"], last["targets"][0]["id"]],
            ["a", "b", "c"],
        )
        self.assertIsNone(last["next_cursor"])
        state = self.root / "state"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        store = state / "target-listing"
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
        self.assertEqual(len(list(store.glob("snapshot-*.json"))), 1)
        self.assertEqual(len(list(store.glob("cursor-*.json"))), 2)
        for path in store.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_default_limit_is_twenty(self):
        template = self.document["targets"][0]
        self.document["targets"] = [
            {**template, "id": f"target-{number:02d}"} for number in range(21)
        ]
        result = listing.list_targets(self.registry())
        self.assertEqual(len(result["targets"]), 20)
        self.assertIsNotNone(result["next_cursor"])

    def test_registration_permissions_or_limit_changes_invalidate_the_cursor(self):
        token = listing.list_targets(self.registry(), limit=1)["next_cursor"]
        for change in ("permission", "path", "registry", "limit"):
            document = copy.deepcopy(self.document)
            limit = 1
            if change == "permission":
                document["targets"][0]["capabilities"] = []
            elif change == "path":
                document["targets"][0]["working_directory"] = "/different"
            elif change == "registry":
                document["registry_id"] = str(uuid4())
            else:
                limit = 2
            with self.subTest(change=change), self.assertRaises(AccessError) as error:
                listing.list_targets(self.registry(document), limit=limit, cursor=token)
            self.assertEqual(error.exception.code, "invalid_cursor")

    def test_invalid_cursor_is_rejected_before_any_storage_access(self):
        for cursor in ("", "../private", "/etc/passwd", "a" * 4096, 3, str(uuid4()).upper()):
            with (
                self.subTest(cursor=cursor),
                patch.object(listing, "directory", side_effect=AssertionError("storage read")),
            ):
                with self.assertRaises(AccessError) as error:
                    listing.list_targets(self.registry(), cursor=cursor)
                self.assertEqual(error.exception.code, "invalid_cursor")
        self.assertFalse((self.root / "state").exists())

    def test_limit_validation_precedes_storage(self):
        for limit in (0, 101, True, "20"):
            with self.subTest(limit=limit), self.assertRaises(AccessError) as error:
                listing.list_targets(self.registry(), limit=limit)
            self.assertEqual(error.exception.code, "invalid_input")
        self.assertFalse((self.root / "state").exists())

    def test_page_byte_cap_returns_fewer_targets_than_the_limit(self):
        template = self.document["targets"][0]
        self.document["targets"] = [{**template, "id": f"target-{number}"} for number in range(10)]
        with patch.object(listing, "MAX_PAGE_BYTES", 1100):
            result = listing.list_targets(self.registry(), limit=10)
            self.assertLess(len(result["targets"]), 10)
            combined = []
            while True:
                self.assertLessEqual(len(json.dumps(result).encode()), 1100)
                combined.extend(target["id"] for target in result["targets"])
                if result["next_cursor"] is None:
                    break
                result = listing.list_targets(
                    self.registry(), limit=10, cursor=result["next_cursor"]
                )
        self.assertEqual(combined, [f"target-{number}" for number in range(10)])

    def test_missing_changed_or_oversized_snapshot_rejects_resumption(self):
        token = listing.list_targets(self.registry(), limit=1)["next_cursor"]
        record = json.loads(self.cursor_path(token).read_text())
        snapshot = self.cursor_path(token).with_name(f"snapshot-{record['snapshot_id']}.json")
        original = snapshot.read_bytes()
        for payload in (b'{"private-marker": true}', b" " * (listing.MAX_SNAPSHOT_BYTES + 1), None):
            with self.subTest(payload_size=None if payload is None else len(payload)):
                if payload is None:
                    snapshot.unlink()
                else:
                    snapshot.write_bytes(payload)
                with self.assertRaises(AccessError) as error:
                    listing.list_targets(self.registry(), limit=1, cursor=token)
                self.assertEqual(error.exception.code, "invalid_cursor")
                self.assertNotIn("private-marker", str(error.exception))
                snapshot.write_bytes(original)
                snapshot.chmod(0o600)

    def test_cursor_file_and_snapshot_ids_cannot_redirect_reads(self):
        token = listing.list_targets(self.registry(), limit=1)["next_cursor"]
        path = self.cursor_path(token)
        record = json.loads(path.read_text())
        record["snapshot_id"] = "../../private"
        path.write_text(json.dumps(record))
        with (
            patch.object(listing, "_read", wraps=listing._read) as reads,
            self.assertRaises(AccessError) as error,
        ):
            listing.list_targets(self.registry(), limit=1, cursor=token)
        self.assertEqual(error.exception.code, "invalid_cursor")
        self.assertEqual(reads.call_count, 1)

    def test_unsafe_cursor_files_are_rejected_without_blocking(self):
        token = listing.list_targets(self.registry(), limit=1)["next_cursor"]
        path = self.cursor_path(token)
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(AccessError):
            listing.list_targets(self.registry(), limit=1, cursor=token)
        for kind in ("symlink", "fifo", "directory"):
            path.unlink()
            if kind == "symlink":
                path.symlink_to(self.root / "private")
            elif kind == "fifo":
                os.mkfifo(path, 0o600)
            else:
                path.mkdir(mode=0o700)
            started = time.monotonic()
            with self.subTest(kind=kind), self.assertRaises(AccessError) as error:
                listing.list_targets(self.registry(), limit=1, cursor=token)
            self.assertEqual(error.exception.code, "invalid_cursor")
            self.assertLess(time.monotonic() - started, 0.5)
            if kind == "directory":
                path.rmdir()
            else:
                path.unlink()
            path.write_bytes(original)
            path.chmod(0o600)

    def test_symlinked_state_or_listing_directory_is_rejected(self):
        alternate = self.root / "alternate"
        alternate.mkdir(mode=0o700)
        state = self.root / "state"
        state.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(AccessError) as error:
            listing.list_targets(self.registry())
        self.assertEqual(error.exception.code, "permission_denied")
        state.unlink()
        state.mkdir(mode=0o700)
        (state / "target-listing").symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(AccessError) as error:
            listing.list_targets(self.registry())
        self.assertEqual(error.exception.code, "permission_denied")
        self.assertEqual(list(alternate.iterdir()), [])

    def test_empty_and_unpermitted_registries_return_an_empty_final_page(self):
        for targets in ([], [{**self.document["targets"][0], "capabilities": []}]):
            self.document["targets"] = targets
            with self.subTest(targets=targets):
                self.assertEqual(
                    listing.list_targets(self.registry()), {"targets": [], "next_cursor": None}
                )
