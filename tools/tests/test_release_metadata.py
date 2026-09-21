"""Keep package and citation versions aligned for release preparation."""

import tempfile
import unittest
from pathlib import Path

from tools.release import version


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_uses_package_and_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
            (root / "CITATION.cff").write_text('version: "0.1.0"\n')
            self.assertEqual(version(root), "0.1.0")

    def test_citation_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
            (root / "CITATION.cff").write_text('version: "0.2.0"\n')
            with self.assertRaisesRegex(ValueError, "Package and citation versions disagree"):
                version(root)
