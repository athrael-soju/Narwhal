"""Keep immutable evidence scoped, private and bounded across server restarts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from narwhal.deployment.management_registry import ManagementTarget
from narwhal.diagnostics.management_artifacts import (
    MAX_EXPORT_BYTES,
    MAX_READ_BYTES,
    ArtifactError,
    ArtifactStore,
)

REGISTRY_ID = "10000000-0000-4000-8000-000000000001"


def target_document(root: Path, **changes: object) -> dict:
    document = {
        "id": "target-a",
        "kind": "dev",
        "working_directory": str(root),
        "artifact_root": str(root / "exports"),
        "fleet_file": None,
        "instance_dir": str(root / "dev"),
        "adapter": {"id": "local-dev-v1", "settings_path": None},
        "credential_env": ["TOKEN"],
    }
    document.update(changes)
    return document


class ManagementArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.target_with()
        self.store = ArtifactStore(REGISTRY_ID, self.target)

    def target_with(self, **changes):
        return ManagementTarget.model_validate_json(
            json.dumps(target_document(self.root, **changes))
        )

    def scope(self, registry_id=REGISTRY_ID, target_id="target-a"):
        return self.target.artifact_root / ".management-artifacts" / registry_id / target_id

    def artifact(self, artifact_id):
        return self.scope() / artifact_id

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(ArtifactError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), raised.exception.message)
        self.assertNotIn(str(self.root), raised.exception.message)

    def test_export_survives_restart_and_retains_exact_bytes_and_private_modes(self):
        self.assertFalse(self.target.artifact_root.exists())
        content = '{"status":"ready","name":"närwhal"}\n'.encode()
        reference = self.store.export(content, "router-state", complete=False)
        self.assertEqual(
            set(reference),
            {"artifact_id", "kind", "state", "sha256", "size_bytes", "observed_at", "complete"},
        )
        UUID(reference["artifact_id"])
        self.assertEqual(reference["kind"], "router-state")
        self.assertEqual(reference["state"], "created")
        self.assertEqual(reference["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(reference["size_bytes"], len(content))
        self.assertFalse(reference["complete"])
        self.assertTrue(reference["observed_at"].endswith("Z"))
        restarted = ArtifactStore(REGISTRY_ID, self.target)
        read = restarted.read(reference["artifact_id"])
        self.assertEqual(read["text"].encode(), content)
        self.assertEqual(read["sha256"], reference["sha256"])
        self.assertTrue(read["complete"])
        self.assertEqual(read["offset"], 0)
        self.assertIsNone(read["next_offset"])
        for path in (self.target.artifact_root, *self.target.artifact_root.rglob("*")):
            metadata = path.stat()
            self.assertEqual(metadata.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700 if path.is_dir() else 0o600)

    def test_text_jsonl_empty_and_utf8_pagination_obey_byte_boundaries(self):
        reference = self.store.export("aé🐋z".encode(), "text")
        artifact_id = reference["artifact_id"]
        first = self.store.read(artifact_id, max_bytes=2)
        self.assertEqual(first["text"], "a")
        self.assertEqual(first["next_offset"], 1)
        self.assertFalse(first["complete"])
        second = self.store.read(artifact_id, offset=1, max_bytes=3)
        self.assertEqual(second["text"], "é")
        self.assertEqual(second["next_offset"], 3)
        third = self.store.read(artifact_id, offset=3, max_bytes=4)
        self.assertEqual(third["text"], "🐋")
        self.assertEqual(third["next_offset"], 7)
        last = self.store.read(artifact_id, offset=7, max_bytes=1)
        self.assertEqual(last["text"], "z")
        self.assertIsNone(last["next_offset"])
        self.assertTrue(last["complete"])
        eof = self.store.read(artifact_id, offset=8, max_bytes=1)
        self.assertEqual(eof["text"], "")
        self.assertTrue(eof["complete"])
        for content in (b"", b'{"first":1}\n{"second":2}\n', b"plain\ttext\r\n"):
            with self.subTest(content=content):
                artifact = self.store.export(content, "evidence")
                self.assertEqual(self.store.read(artifact["artifact_id"])["text"].encode(), content)

    def test_invalid_offsets_limits_and_identifiers_do_not_create_storage(self):
        for artifact_id in ("../secret", "not-a-uuid", "../../", "", str(uuid4()) + "/../x", 1):
            with self.subTest(artifact_id=artifact_id):
                self.assert_error("invalid_input", self.store.read, artifact_id)
        valid_id = str(uuid4())
        for options in (
            {"offset": -1},
            {"offset": True},
            {"offset": "1"},
            {"offset": 1.0},
            {"max_bytes": 0},
            {"max_bytes": MAX_READ_BYTES + 1},
            {"max_bytes": True},
            {"max_bytes": 1.0},
        ):
            with self.subTest(options=options):
                self.assert_error("invalid_input", self.store.read, valid_id, **options)
        self.assertFalse(self.target.artifact_root.exists())
        self.assert_error("invalid_input", ArtifactStore, "../registry", self.target)
        artifact_id = self.store.export("é🐋".encode(), "text")["artifact_id"]
        for options in (
            {"offset": 1},
            {"offset": 3},
            {"offset": 7},
            {"max_bytes": 1},
            {"offset": 2, "max_bytes": 3},
        ):
            with self.subTest(options=options):
                self.assert_error("invalid_input", self.store.read, artifact_id, **options)

    def test_default_read_size_is_bounded_and_whole_export_limit_is_enforced(self):
        content = b"x" * (MAX_READ_BYTES + 1)
        reference = self.store.export(content, "text")
        read = self.store.read(reference["artifact_id"])
        self.assertEqual(len(read["text"].encode()), MAX_READ_BYTES)
        self.assertEqual(read["next_offset"], MAX_READ_BYTES)
        self.assertFalse(read["complete"])
        oversized = b"x" * (MAX_EXPORT_BYTES + 1)
        self.assert_error("artifact_too_large", self.store.export, oversized, "text")

    def test_invalid_utf8_binary_and_argument_types_are_rejected_before_writing(self):
        for content in (b"\xff", b"binary\x00content", b"\x01\x02\x03"):
            with self.subTest(content=content):
                self.assert_error("unsupported_media_type", self.store.export, content, "text")
        self.assert_error("invalid_input", self.store.export, "text", "text")
        self.assert_error("invalid_input", self.store.export, b"text", 1)
        self.assert_error("invalid_input", self.store.export, b"text", "text", complete=1)
        self.assertFalse(self.target.artifact_root.exists())

    def test_other_targets_and_registries_cannot_read_or_copy_artifact_references(self):
        reference = self.store.export(b"target-a evidence", "text")
        artifact_id = reference["artifact_id"]
        other_registry_id = str(uuid4())
        other_target = self.target_with(id="target-b")
        stores = (
            (ArtifactStore(other_registry_id, self.target), other_registry_id, self.target.id),
            (ArtifactStore(REGISTRY_ID, other_target), REGISTRY_ID, other_target.id),
        )
        for store, registry_id, target_id in stores:
            with self.subTest(registry_id=registry_id, target_id=target_id):
                self.assert_error("artifact_missing", store.read, artifact_id)
                store.export(b"separate export", "text")
                copied = self.scope(registry_id, target_id) / artifact_id
                shutil.copytree(self.artifact(artifact_id), copied)
                self.assert_error("permission_denied", store.read, artifact_id)

    def test_rebinding_resources_or_credential_references_rejects_old_exports(self):
        artifact_id = self.store.export(b"old target", "text")["artifact_id"]
        for changes in (
            {"instance_dir": str(self.root / "other-dev")},
            {"working_directory": str(self.root / "other-working-directory")},
            {"fleet_file": str(self.root / "different-fleet.json")},
            {"adapter": {"id": "local-dev-v1", "settings_path": str(self.root / "adapter.json")}},
            {"endpoints": {"router_env": "DIFFERENT_ROUTER"}},
            {"credential_env": ["OTHER_TOKEN"]},
        ):
            with self.subTest(changes=changes):
                rebound = ArtifactStore(REGISTRY_ID, self.target_with(**changes))
                self.assert_error("permission_denied", rebound.read, artifact_id)
        changed_grants = self.target_with(
            capabilities=["inspect", "measure"], actions=["dev_verify"], freshness_s=10
        )
        self.assertEqual(
            ArtifactStore(REGISTRY_ID, changed_grants).read(artifact_id)["text"], "old target"
        )

    def test_request_content_revocation_blocks_exports_made_under_broader_policy(self):
        enabled = ArtifactStore(REGISTRY_ID, self.target_with(allow_request_content=True))
        permissive_export = enabled.export(b"request content", "text")
        self.assert_error("permission_denied", self.store.read, permissive_export["artifact_id"])
        restricted_export = self.store.export(b"redacted", "text")
        self.assertEqual(enabled.read(restricted_export["artifact_id"])["text"], "redacted")

    def test_missing_metadata_bytes_or_artifact_never_resolves_to_live_evidence(self):
        self.assert_error("artifact_missing", self.store.read, str(uuid4()))
        self.assertFalse(self.target.artifact_root.exists())
        for name in ("record.json", "content"):
            with self.subTest(name=name):
                artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
                (self.artifact(artifact_id) / name).unlink()
                self.assert_error("artifact_missing", self.store.read, artifact_id)

    def test_changed_bytes_length_metadata_or_identity_are_rejected(self):
        for content in (b"tampered", b"changed content is longer"):
            with self.subTest(content=content):
                artifact_id = self.store.export(b"original", "text")["artifact_id"]
                (self.artifact(artifact_id) / "content").write_bytes(content)
                self.assert_error("artifact_changed", self.store.read, artifact_id)
        for changes in ({"artifact_id": str(uuid4())}, {"size_bytes": 1}, {"version": True}):
            with self.subTest(changes=changes):
                artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
                path = self.artifact(artifact_id) / "record.json"
                record = json.loads(path.read_text())
                record.update(changes)
                path.write_text(json.dumps(record))
                self.assert_error("artifact_changed", self.store.read, artifact_id)
        artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
        (self.artifact(artifact_id) / "record.json").write_bytes(b"not JSON")
        self.assert_error("artifact_changed", self.store.read, artifact_id)

    def test_repeated_identifier_cannot_overwrite_an_existing_export(self):
        identifier = uuid4()
        with patch("narwhal.diagnostics.management_artifacts.uuid4", return_value=identifier):
            reference = self.store.export(b"original", "text")
            self.assert_error("artifact_changed", self.store.export, b"replacement", "text")
        self.assertEqual(self.store.read(reference["artifact_id"])["text"], "original")

    def test_symlinks_at_any_directory_or_file_component_are_rejected(self):
        external = self.root / "private-other"
        external.mkdir(mode=0o700)
        self.target.artifact_root.symlink_to(external, target_is_directory=True)
        self.assert_error("permission_denied", self.store.export, b"evidence", "text")
        self.target.artifact_root.unlink()
        ancestor = self.root / "ancestor-link"
        ancestor.symlink_to(external, target_is_directory=True)
        via_ancestor = ArtifactStore(
            REGISTRY_ID, self.target_with(artifact_root=str(ancestor / "exports"))
        )
        self.assert_error("permission_denied", via_ancestor.export, b"evidence", "text")
        self.assertFalse((external / "exports").exists())
        for name in ("record.json", "content"):
            with self.subTest(file=name):
                artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
                path = self.artifact(artifact_id) / name
                saved = external / name
                path.rename(saved)
                path.symlink_to(saved)
                self.assert_error("permission_denied", self.store.read, artifact_id)
        artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
        path = self.artifact(artifact_id)
        saved = external / artifact_id
        path.rename(saved)
        path.symlink_to(saved, target_is_directory=True)
        self.assert_error("permission_denied", self.store.read, artifact_id)

    def test_private_namespace_symlinks_and_parent_traversal_are_rejected(self):
        external = self.root / "private-other"
        external.mkdir(mode=0o700)
        self.target.artifact_root.mkdir(mode=0o700)
        namespace = self.target.artifact_root / ".management-artifacts"
        namespace.symlink_to(external, target_is_directory=True)
        self.assert_error("permission_denied", self.store.export, b"evidence", "text")
        self.assertEqual(list(external.iterdir()), [])
        traversing = ArtifactStore(
            REGISTRY_ID,
            self.target_with(artifact_root=str(self.root / "missing" / ".." / "exports")),
        )
        self.assert_error("permission_denied", traversing.export, b"evidence", "text")

    def test_insecure_root_and_file_modes_are_never_repaired(self):
        self.target.artifact_root.mkdir(mode=0o755)
        self.target.artifact_root.chmod(0o755)
        self.assert_error("permission_denied", self.store.export, b"evidence", "text")
        self.assertEqual(stat.S_IMODE(self.target.artifact_root.stat().st_mode), 0o755)
        self.target.artifact_root.chmod(0o700)
        for name in ("record.json", "content"):
            with self.subTest(name=name):
                artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
                path = self.artifact(artifact_id) / name
                path.chmod(0o640)
                self.assert_error("permission_denied", self.store.read, artifact_id)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.scope().chmod(0o750)
        self.assert_error("permission_denied", self.store.export, b"evidence", "text")
        self.assertEqual(stat.S_IMODE(self.scope().stat().st_mode), 0o750)

    def test_wrong_owner_nonregular_and_hardlinked_exports_are_rejected(self):
        self.target.artifact_root.mkdir(mode=0o700)
        observed = self.target.artifact_root.stat()
        other_owner = SimpleNamespace(st_mode=observed.st_mode, st_uid=os.getuid() + 1)
        with patch("narwhal.diagnostics.management_artifacts.os.fstat", return_value=other_owner):
            self.assert_error("permission_denied", self.store.export, b"evidence", "text")
        artifact_id = self.store.export(b"evidence", "text")["artifact_id"]
        content = self.artifact(artifact_id) / "content"
        content.unlink()
        os.mkfifo(content, mode=0o600)
        self.assert_error("permission_denied", self.store.read, artifact_id)
        content.unlink()
        content.mkdir(mode=0o700)
        self.assert_error("permission_denied", self.store.read, artifact_id)
        content.rmdir()
        secret = self.root / "credentials.txt"
        secret.write_bytes(b"not an export")
        secret.chmod(0o600)
        os.link(secret, content)
        self.assert_error("permission_denied", self.store.read, artifact_id)

    def test_export_creates_only_final_root_and_private_namespace(self):
        root = self.root / "missing-parent" / "exports"
        store = ArtifactStore(REGISTRY_ID, self.target_with(artifact_root=str(root)))
        self.assert_error("artifact_missing", store.export, b"evidence", "text")
        self.assertFalse(root.parent.exists())

    def test_error_messages_exclude_private_paths_and_data(self):
        private_root = self.root / "private-token-marker"
        private_root.mkdir(mode=0o755)
        private_root.chmod(0o755)
        store = ArtifactStore(REGISTRY_ID, self.target_with(artifact_root=str(private_root)))
        with self.assertRaises(ArtifactError) as failure:
            store.export(b"secret-content-marker", "text")
        self.assertNotIn("private-token-marker", str(failure.exception))
        self.assertNotIn("secret-content-marker", str(failure.exception))
