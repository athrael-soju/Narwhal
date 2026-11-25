"""Checkpoint discovery binds complete file content across replicas."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from narwhal.deployment.launch_engine import checkpoint
from narwhal.deployment.launch_engine.checkpoint import (
    checkpoint_status,
    hash_regular_file,
    inspect_checkpoint,
)


class CheckpointManifestTests(unittest.TestCase):
    def test_standalone_remote_command_reads_model_dir_from_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            script = Path(checkpoint.__file__)
            result = subprocess.run(
                ["python3", "-c", script.read_text()],
                input=json.dumps({"model_dir": str(root)}),
                text=True,
                capture_output=True,
                check=True,
            )
            output = json.loads(result.stdout)
            self.assertEqual(output["file_count"], 1)
            # File status is host-local, so discovery records content only.
            self.assertNotIn("status_sha256", output)

    def test_the_tree_digest_keeps_its_manifest_version_2_value(self):
        # Discovery on the engine host and prepare must agree, across releases too.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            (root / "model.safetensors").write_bytes(b"weights")
            self.assertEqual(
                inspect_checkpoint(root)["model_tree_sha256"],
                "3213e532964e545274c2459da0ef2172da8155bacbc479fc3f8a1f42cbdc4b94",
            )

    def test_content_changes_and_missing_files_change_the_tree_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            (root / "model-00001.safetensors").write_bytes(b"weights")
            (root / "tokenizer.json").write_text("tokens")
            first = inspect_checkpoint(root)
            self.assertEqual(first["file_count"], 3)
            (root / "model-00001.safetensors").write_bytes(b"changed")
            second = inspect_checkpoint(root)
            self.assertNotEqual(first["model_tree_sha256"], second["model_tree_sha256"])
            (root / "tokenizer.json").unlink()
            third = inspect_checkpoint(root)
            self.assertNotEqual(second["model_tree_sha256"], third["model_tree_sha256"])

    def test_hub_download_metadata_is_excluded_and_file_symlinks_are_hashed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            (root / "source.safetensors").write_bytes(b"weights")
            (root / "linked.safetensors").symlink_to(root / "source.safetensors")
            metadata = root / ".cache/huggingface/download/config.json.metadata"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("a" * 40)
            first = inspect_checkpoint(root)
            metadata.write_text("b" * 40)
            second = inspect_checkpoint(root)
            self.assertEqual(first, second)
            self.assertNotIn(".cache", json.dumps(first["files"]))

    def test_root_model_card_is_excluded_from_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            (root / "model.safetensors").write_bytes(b"weights")
            (root / "README.md").write_text("first card")
            first = inspect_checkpoint(root)
            (root / "README.md").write_text("revised card")
            second = inspect_checkpoint(root)
            self.assertEqual(first, second)
            self.assertEqual(first["file_count"], 2)
            self.assertEqual(first["excluded_paths"], ["README.md", ".cache/huggingface/**"])
            (root / "config.json").write_text('{"changed":true}')
            self.assertNotEqual(
                first["model_tree_sha256"], inspect_checkpoint(root)["model_tree_sha256"]
            )

    def test_file_status_matches_the_hashed_files_until_one_is_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            weights = root / "model.safetensors"
            weights.write_bytes(b"weights")
            (root / "README.md").write_text("card")
            manifest = inspect_checkpoint(root)
            self.assertEqual(checkpoint_status(root), manifest["status_sha256"])
            # Excluded files stay out of the status as they stay out of the digest.
            (root / "README.md").write_text("revised card")
            self.assertEqual(checkpoint_status(root), manifest["status_sha256"])
            # Same name, size and bytes from another inode, as a copy and rename leaves it.
            replacement = root / "replacement"
            replacement.write_bytes(b"weights")
            replacement.replace(weights)
            self.assertEqual(
                inspect_checkpoint(root)["model_tree_sha256"], manifest["model_tree_sha256"]
            )
            self.assertNotEqual(checkpoint_status(root), manifest["status_sha256"])
            before = checkpoint_status(root)
            (root / "extra.safetensors").write_bytes(b"more")
            self.assertNotEqual(checkpoint_status(root), before)

    def test_a_link_out_of_the_directory_is_refused_unless_links_are_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "model"
            root.mkdir()
            (root / "config.json").write_text("{}")
            elsewhere = Path(folder) / "other.safetensors"
            elsewhere.write_bytes(b"other weights")
            (root / "model.safetensors").symlink_to(elsewhere)
            with self.assertRaisesRegex(ValueError, "resolves outside the model directory"):
                inspect_checkpoint(root)
            with self.assertRaisesRegex(ValueError, "resolves outside the model directory"):
                checkpoint_status(root)
            followed = inspect_checkpoint(root, contained=False)
            self.assertEqual(followed["file_count"], 2)
            self.assertEqual(checkpoint_status(root, contained=False), followed["status_sha256"])
            script = Path(checkpoint.__file__).read_text()
            for inputs, code in (({}, 1), ({"follow_links": True}, 0)):
                result = subprocess.run(
                    ["python3", "-c", script],
                    input=json.dumps({"model_dir": str(root), **inputs}),
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, code, result.stderr)

    def test_a_non_regular_file_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / "weights.gguf"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "not a regular file: weights.gguf"):
                hash_regular_file(fifo, "weights.gguf")

    def test_missing_config_blocks_inspection(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            self.assertRaisesRegex(ValueError, "config.json"),
        ):
            inspect_checkpoint(Path(folder))
