"""Deployment bundles supply the approved revision to an isolated clone."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class SourceBundleTests(unittest.TestCase):
    def test_documented_bundle_clones_the_selected_local_commit(self):
        root = Path(__file__).resolve().parents[2]
        guide = (root / "docs/Deploy.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)\n```", guide, re.S)
        preparation = next(block for block in blocks if "bundle create" in block)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        for selected in (revision, "0" * 40):
            with self.subTest(valid=selected == revision), tempfile.TemporaryDirectory() as folder:
                transfer = Path(folder) / "transfer"
                transfer.mkdir()
                (transfer / ".env.router").write_text("synthetic-private-value")
                env = {
                    "PATH": os.environ["PATH"],
                    "HOME": os.environ["HOME"],
                    "NARWHAL_ENV_DIR": str(transfer),
                    "NARWHAL_DEPLOYMENT_REVISION": selected,
                    "GIT_TERMINAL_PROMPT": "0",
                }
                result = subprocess.run(
                    ["bash", "--noprofile", "--norc"],
                    input=preparation,
                    cwd=root,
                    env=env,
                    text=True,
                    capture_output=True,
                )
                if selected != revision:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((transfer / "source.bundle").exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
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
