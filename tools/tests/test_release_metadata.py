"""Keep package and citation versions aligned for release preparation."""

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tools.release import (
    INITIAL_RELEASE_BRANCH,
    RELEASE_BRANCH,
    REPOSITORY,
    api,
    candidate,
    pypi_status,
    release_by_tag,
    release_pr,
    version,
)


class ReleaseMetadataTests(unittest.TestCase):
    def test_pypi_status_accepts_only_identical_distributions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            dist.mkdir()
            wheel = dist / "narwhal_inference-0.3.0-py3-none-any.whl"
            source = dist / "narwhal_inference-0.3.0.tar.gz"
            wheel.write_bytes(b"wheel")
            source.write_bytes(b"source")
            files = [
                {
                    "filename": path.name,
                    "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                }
                for path in (wheel, source)
            ]
            with mock.patch("tools.release.validate", return_value="0.3.0"):
                with mock.patch(
                    "tools.release.urllib.request.urlopen",
                    return_value=io.BytesIO(json.dumps({"urls": files}).encode()),
                ):
                    self.assertEqual(pypi_status(root, dist), "matching")
                files[0]["digests"]["sha256"] = "0" * 64
                with (
                    mock.patch(
                        "tools.release.urllib.request.urlopen",
                        return_value=io.BytesIO(json.dumps({"urls": files}).encode()),
                    ),
                    self.assertRaisesRegex(ValueError, "different distribution files"),
                ):
                    pypi_status(root, dist)

    def test_pypi_status_allows_a_new_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            dist.mkdir()
            for name in (
                "narwhal_inference-0.3.0-py3-none-any.whl",
                "narwhal_inference-0.3.0.tar.gz",
            ):
                (dist / name).write_bytes(b"artifact")
            missing = urllib.error.HTTPError("https://pypi.org/", 404, "Not Found", {}, None)
            with (
                mock.patch("tools.release.validate", return_value="0.3.0"),
                mock.patch("tools.release.urllib.request.urlopen", side_effect=missing),
            ):
                self.assertEqual(pypi_status(root, dist), "missing")

    def test_published_release_is_found_in_release_list(self):
        published = {"tag_name": "v0.1.0", "draft": False, "assets": []}
        with mock.patch(
            "tools.release.command",
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
            mock.patch("tools.release.command", side_effect=git),
            mock.patch("tools.release.api", return_value=[]),
            mock.patch("tools.release.version", return_value="0.1.0"),
            mock.patch("tools.release.validate", return_value="0.1.0"),
        ):
            self.assertEqual(candidate(Path(".")), ("0.1.0", None))

    def test_repository_api_uses_endpoint_without_trailing_slash(self):
        with mock.patch("tools.release.command", return_value='{"private": false}') as command:
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
