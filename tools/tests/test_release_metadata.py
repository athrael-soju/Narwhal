"""Keep package and citation versions aligned for release preparation."""

import tempfile
import unittest
from pathlib import Path

from tools.release import INITIAL_RELEASE_BRANCH, RELEASE_BRANCH, REPOSITORY, release_pr, version


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

    def test_first_release_pr_requires_its_branch_and_pending_label(self):
        pr = {
            "merged_at": "2026-09-21T00:00:00Z",
            "merge_commit_sha": "release-sha",
            "base": {"ref": "main"},
            "head": {"ref": INITIAL_RELEASE_BRANCH, "repo": {"full_name": REPOSITORY}},
            "labels": [{"name": "autorelease: pending"}],
        }
        self.assertTrue(release_pr(pr, "release-sha", "0.1.0"))
        self.assertFalse(release_pr(pr, "release-sha", "0.2.0"))
        self.assertFalse(release_pr(pr, "other-sha", "0.1.0"))
        pr["labels"] = []
        self.assertFalse(release_pr(pr, "release-sha", "0.1.0"))

    def test_later_releases_keep_the_release_please_branch(self):
        pr = {
            "merged_at": "2026-09-21T00:00:00Z",
            "merge_commit_sha": "release-sha",
            "base": {"ref": "main"},
            "head": {"ref": RELEASE_BRANCH, "repo": {"full_name": REPOSITORY}},
            "labels": [{"name": "autorelease: pending"}],
        }
        self.assertTrue(release_pr(pr, "release-sha", "0.2.0"))
        pr["head"]["ref"] = "feature/other-work"
        self.assertFalse(release_pr(pr, "release-sha", "0.2.0"))
