"""Deployment bundles supply the approved revision to an isolated clone."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.deploy_hosts import prepare_bundle


class SourceBundleTests(unittest.TestCase):
    def test_documented_bundle_clones_the_selected_local_commit(self):
        root = Path(__file__).resolve().parents[2]
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        for selected in (revision, "0" * 40):
            with self.subTest(valid=selected == revision), tempfile.TemporaryDirectory() as folder:
                transfer = Path(folder) / "transfer"
                transfer.mkdir()
                (transfer / ".env.router").write_text("synthetic-private-value")
                if selected != revision:
                    with self.assertRaisesRegex(ValueError, "Source preparation failed"):
                        prepare_bundle(root, selected, transfer / "source.bundle")
                    self.assertFalse((transfer / "source.bundle").exists())
                    continue
                prepare_bundle(root, selected, transfer / "source.bundle")
                self.assertEqual((transfer / "source.bundle").stat().st_mode & 0o777, 0o600)
                checkout = Path(folder) / "remote"
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--branch",
                        "deployment",
                        str(transfer / "source.bundle"),
                        str(checkout),
                    ],
                    check=True,
                    capture_output=True,
                )
                actual = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
                ).strip()
                self.assertEqual(actual, selected)
                self.assertFalse((checkout / ".env.router").exists())
                self.assertFalse((checkout / ".env").exists())
                self.assertFalse(any(transfer.glob("source.*/*")))


if __name__ == "__main__":
    unittest.main()
