"""Exercise immutable remote transfers and filesystem boundaries without SSH."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from narwhal.deployment.ssh_files import CHUNK_BYTES, SSHFiles, file_dispatch


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "remote"
        self.name = f"operations/{uuid4()}/source.bundle"

    def put(self, content, *, offset=0, chunk=None, name=None):
        return file_dispatch(
            {
                "op": "put",
                "root": str(self.root),
                "path": name or self.name,
                "offset": offset,
                "total_size": len(content),
                "data": base64.b64encode(content if chunk is None else chunk).decode(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    def test_chunk_retry_and_final_retry_preserve_exact_bytes(self):
        content = b"a" * CHUNK_BYTES + b"remaining source"
        first = self.put(content, chunk=content[:CHUNK_BYTES])
        self.assertFalse(first["complete"])
        self.assertFalse((self.root / self.name).exists())
        self.assertEqual(self.put(content, chunk=content[:CHUNK_BYTES]), first)
        result = self.put(content, offset=CHUNK_BYTES, chunk=content[CHUNK_BYTES:])
        self.assertTrue(result["complete"])
        self.assertEqual((self.root / self.name).read_bytes(), content)
        self.assertEqual(self.put(content, offset=CHUNK_BYTES, chunk=content[CHUNK_BYTES:]), result)
        self.assertEqual((self.root / self.name).stat().st_mode & 0o777, 0o600)

    def test_changed_retry_cannot_overwrite_existing_artifact(self):
        self.put(b"original")
        with self.assertRaises(ValueError):
            self.put(b"different")
        self.assertEqual((self.root / self.name).read_bytes(), b"original")

    def test_retry_recovers_interruption_between_publication_and_partial_unlink(self):
        content = b"verified source bundle"
        with (
            patch("narwhal.deployment.ssh_files.os.unlink", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            self.put(content)
        final = self.root / self.name
        partial = final.with_name(final.name + ".upload-" + hashlib.sha256(content).hexdigest())
        self.assertEqual(final.stat().st_nlink, 2)
        self.assertEqual(final.stat().st_ino, partial.stat().st_ino)
        self.assertTrue(self.put(content)["complete"])
        self.assertEqual(final.stat().st_nlink, 1)
        self.assertFalse(partial.exists())
        self.assertEqual(base64.b64decode(self.read()["data"]), content)

    def test_recovery_rejects_unrelated_or_conflicting_partial_links(self):
        content = b"original"
        self.put(content)
        final = self.root / self.name
        partial = final.with_name(final.name + ".upload-" + hashlib.sha256(content).hexdigest())
        unrelated = final.with_name("unrelated")
        os.link(final, unrelated)
        with self.assertRaises(ValueError):
            self.put(content)
        partial.write_bytes(content)
        partial.chmod(0o600)
        conflicting = final.with_name("conflicting")
        os.link(partial, conflicting)
        with self.assertRaisesRegex(ValueError, "same file"):
            self.put(content)
        self.assertEqual(final.stat().st_nlink, 2)
        self.assertEqual(partial.stat().st_nlink, 2)
        self.assertNotEqual(final.stat().st_ino, partial.stat().st_ino)
        self.assertEqual(unrelated.read_bytes(), content)
        self.assertEqual(conflicting.read_bytes(), content)

    def test_recovery_rejects_an_additional_hardlink(self):
        content = b"original"
        self.put(content)
        final = self.root / self.name
        partial = final.with_name(final.name + ".upload-" + hashlib.sha256(content).hexdigest())
        unrelated = final.with_name("unrelated")
        os.link(final, partial)
        os.link(final, unrelated)
        with self.assertRaises(ValueError):
            self.put(content)
        self.assertEqual(final.stat().st_nlink, 3)
        self.assertTrue(partial.exists())
        self.assertTrue(unrelated.exists())

    def test_recovery_verifies_digest_before_removing_partial_link(self):
        self.put(b"original")
        changed = b"modified"
        final = self.root / self.name
        partial = final.with_name(final.name + ".upload-" + hashlib.sha256(changed).hexdigest())
        os.link(final, partial)
        with self.assertRaisesRegex(ValueError, "digest"):
            self.put(changed)
        self.assertEqual(final.read_bytes(), b"original")
        self.assertEqual(final.stat().st_nlink, 2)
        self.assertTrue(partial.exists())

    def test_recovery_preserves_partial_replaced_during_digest_verification(self):
        content = b"original"
        self.put(content)
        final = self.root / self.name
        partial = final.with_name(final.name + ".upload-" + hashlib.sha256(content).hexdigest())
        os.link(final, partial)
        original_pread = os.pread

        def replace_partial(*args):
            value = original_pread(*args)
            partial.unlink()
            partial.write_bytes(b"replacement")
            partial.chmod(0o600)
            return value

        with (
            patch("narwhal.deployment.ssh_files.os.pread", side_effect=replace_partial),
            self.assertRaises(ValueError),
        ):
            self.put(content)
        self.assertEqual(final.read_bytes(), content)
        self.assertEqual(partial.read_bytes(), b"replacement")

    def test_gap_and_oversized_chunk_do_not_publish_file(self):
        with self.assertRaises(ValueError):
            self.put(b"abc", offset=1, chunk=b"bc")
        with self.assertRaises(ValueError):
            self.put(b"x" * (CHUNK_BYTES + 1))
        self.assertFalse((self.root / self.name).exists())

    def test_bad_digest_cannot_publish_partial_upload(self):
        with self.assertRaises(ValueError):
            self.put(b"abc", chunk=b"xyz")
        self.assertFalse((self.root / self.name).exists())

    def test_empty_file_is_published_and_read(self):
        self.put(b"")
        self.assertEqual(self.read()["data"], "")
        self.assertTrue(self.read()["complete"])

    def read(self, *, maximum=32, name=None):
        return file_dispatch(
            {
                "op": "read_file",
                "root": str(self.root),
                "path": name or self.name,
                "offset": 0,
                "max_bytes": maximum,
            }
        )

    def test_read_exposes_bounded_bytes_and_total_size(self):
        self.put(b"0123456789")
        result = self.read(maximum=4)
        self.assertEqual(base64.b64decode(result["data"]), b"0123")
        self.assertEqual(result["size"], 10)
        self.assertFalse(result["complete"])

    def test_same_size_replacement_between_pages_is_rejected(self):
        content = b"a" * CHUNK_BYTES + b"b"
        self.put(content, chunk=content[:CHUNK_BYTES])
        self.put(content, offset=CHUNK_BYTES, chunk=content[CHUNK_BYTES:])
        count = 0

        def rpc(host, request):
            nonlocal count
            if count:
                path = self.root / self.name
                replacement = path.with_name("replacement")
                replacement.write_bytes(b"c" * len(content))
                replacement.chmod(0o600)
                replacement.replace(path)
            count += 1
            return file_dispatch(request)

        files = SSHFiles(
            SimpleNamespace(rpc=rpc), SimpleNamespace(assert_current=Mock()), str(self.root)
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            files.read("host", self.name, max_bytes=len(content))

    def test_traversal_absolute_and_unscoped_paths_are_rejected(self):
        for name in (
            "/etc/passwd",
            "operations/../secret",
            "arbitrary/file",
            "operations/not-a-uuid/file",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.put(b"bad", name=name)

    def test_symlink_parent_and_destination_are_rejected(self):
        self.put(b"original")
        final = self.root / self.name
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"outside")
        outside.chmod(0o600)
        final.unlink()
        final.symlink_to(outside)
        with self.assertRaises(OSError):
            self.put(b"changed")
        with self.assertRaises(OSError):
            self.read()
        self.assertEqual(outside.read_bytes(), b"outside")
        final.unlink()
        parent = final.parent
        parent.rmdir()
        parent.symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            self.put(b"changed")

    def test_hardlinked_and_public_files_are_rejected(self):
        self.put(b"original")
        final = self.root / self.name
        link = final.with_name("linked")
        os.link(final, link)
        with self.assertRaises(ValueError):
            self.read()
        link.unlink()
        final.chmod(0o644)
        with self.assertRaises(ValueError):
            self.read()


if __name__ == "__main__":
    unittest.main()
