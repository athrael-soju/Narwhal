"""Checkpoint discovery binds complete file content across replicas."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.deployment.checkpoint_manifest import inspect_checkpoint


class CheckpointManifestTests(unittest.TestCase):
    def test_standalone_remote_command_reads_model_dir_from_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            script = Path(__file__).resolve().parents[2] / "tools/deployment/checkpoint_manifest.py"
            result = subprocess.run(
                ["python3", "-c", script.read_text()],
                input=json.dumps({"model_dir": str(root)}),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout)["file_count"], 1)

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
            self.assertNotIn(".cache", json.dumps(first))

    def test_missing_config_blocks_inspection(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            self.assertRaisesRegex(ValueError, "config.json"),
        ):
            inspect_checkpoint(Path(folder))
