"""Keep package and citation versions aligned for release preparation."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.maintenance.release import (
    INITIAL_RELEASE_BRANCH,
    RELEASE_BRANCH,
    REPOSITORY,
    api,
    candidate,
    release_by_tag,
    release_pr,
    version,
)


class ReleaseMetadataTests(unittest.TestCase):
    def test_published_release_is_found_in_release_list(self):
        published = {"tag_name": "v0.1.0", "draft": False, "assets": []}
        with mock.patch(
            "tools.maintenance.release.command",
            return_value='[[{"tag_name": "v0.1.0", "draft": false, "assets": []}]]',
        ):
            self.assertEqual(release_by_tag("v0.1.0"), published)

    def test_history_clean_first_release_is_candidate(self):
        def git(*args):
            if args == ("git", "rev-parse", "HEAD"):
                return "root-sha"
            if args == ("git", "rev-parse", "origin/main"):
                return "root-sha"
            if args == ("git", "rev-list", "--parents", "-n", "1", "HEAD"):
                return "root-sha"
            return ""

        with (
            mock.patch("tools.maintenance.release.command", side_effect=git),
            mock.patch("tools.maintenance.release.api", return_value=[]),
            mock.patch("tools.maintenance.release.version", return_value="0.1.0"),
            mock.patch("tools.maintenance.release.validate", return_value="0.1.0"),
        ):
            self.assertEqual(candidate(Path(".")), ("0.1.0", None))

    def test_repository_api_uses_endpoint_without_trailing_slash(self):
        with mock.patch(
            "tools.maintenance.release.command", return_value='{"private": false}'
        ) as command:
            self.assertEqual(api(""), {"private": False})
        command.assert_called_once_with("gh", "api", f"repos/{REPOSITORY}")

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
