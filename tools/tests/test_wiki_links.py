"""Validate repository and flattened wiki links using local fixtures."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.check_links import dangling

ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/athrael-soju/Narwhal/blob/main/"
WIKI_URL = "https://github.com/athrael-soju/Narwhal/wiki/"


class WikiLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.wiki = self.root / "rendered"
        (self.repo / "docs").mkdir(parents=True)
        self.wiki.mkdir()
        (self.repo / "CONTRIBUTING.md").write_text("# Contributing\n## Source responsibilities\n")
        (self.repo / "docs/Next.md").write_text("# Next\n## Section\n")
        (self.wiki / "Next.md").write_text("# Next\n## Section\n")
        self.page = self.wiki / "Home.md"

    def check(self, text):
        self.page.write_text(text)
        return dangling([self.page], root=self.repo, wiki_root=self.wiki)

    def test_repository_relative_link_passes_source_but_fails_flattened_layout(self):
        text = "[Source](../CONTRIBUTING.md#source-responsibilities)"
        source = self.repo / "docs/Home.md"
        source.write_text(text)
        self.assertEqual(dangling([source], root=self.repo), [])
        # Require the target to be inside the rendered wiki, even if it exists elsewhere.
        (self.root / "CONTRIBUTING.md").write_text("## Source responsibilities\n")
        self.assertIn("outside generated wiki", self.check(text)[0])

    def test_canonical_repository_link_uses_source_anchor(self):
        self.assertEqual(
            self.check(f"[Source]({REPO_URL}CONTRIBUTING.md#source-responsibilities)"), []
        )
        self.assertIn(
            "missing anchor", self.check(f"[Source]({REPO_URL}CONTRIBUTING.md#absent)")[0]
        )
        self.assertIn("missing path", self.check(f"[Source]({REPO_URL}absent.md)")[0])

    def test_wiki_page_links_and_fragments_resolve_to_rendered_pages(self):
        self.assertEqual(self.check(f"[Next](Next#section) [Next]({WIKI_URL}Next#section)"), [])
        self.assertIn("missing anchor", self.check("[Next](Next#absent)")[0])
        (self.wiki / "Next.md").unlink()
        for target in ("Next#section", f"{WIKI_URL}Next#section"):
            with self.subTest(target=target):
                self.assertIn("missing path", self.check(f"[Next]({target})")[0])

    def test_markdown_and_html_assets_must_exist_in_rendered_layout(self):
        text = '![Diagram](diagram.svg) <img src="logo.png" alt="Logo">'
        self.assertEqual(len(self.check(text)), 2)
        (self.wiki / "diagram.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (self.wiki / "logo.png").write_bytes(b"fixture")
        self.assertEqual(self.check(text), [])

    def test_html_links_and_same_page_anchors(self):
        self.assertEqual(self.check('# Home\n<a href="Next#section">Next</a> [Top](#home)'), [])
        self.assertIn("missing anchor", self.check('<a href="Next#absent">Next</a>')[0])

    def test_fences_and_external_urls_are_skipped(self):
        self.assertEqual(
            self.check(
                '```markdown\n[Missing](absent) <img src="absent.png">\n```\n'
                "[External](https://example.invalid/page)"
            ),
            [],
        )

    def publisher_fixture(self, text):
        tools = self.repo / "tools"
        tools.mkdir()
        for name in ("publish_wiki.sh", "check_links.py"):
            shutil.copyfile(ROOT / "tools" / name, tools / name)
        (self.repo / "docs/wiki-pages.txt").write_text("Home.md\nNext.md\n")
        (self.repo / "docs/Home.md").write_text(text)
        (self.repo / "assets/architectures").mkdir(parents=True)
        (self.repo / "assets/architectures/diagram.svg").write_text("<svg/>")
        (self.repo / "assets/social-preview.png").write_bytes(b"fixture")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)

    def publish(self, mode):
        return subprocess.run(
            ["bash", "tools/publish_wiki.sh", mode],
            cwd=self.repo,
            env={**os.environ, "WIKI_URL": str(self.root / "missing.wiki.git")},
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_publisher_check_renders_pages_and_assets_offline(self):
        self.publisher_fixture(
            f"[Next](Next.md#section) [Source]({REPO_URL}CONTRIBUTING.md#source-responsibilities)\n"
            "![Diagram](../assets/architectures/diagram.svg)\n"
            '<img src="../assets/social-preview.png">\n'
        )
        result = self.publish("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("generated wiki links passed", result.stdout)

    def test_publisher_rejects_relative_repo_link_before_clone(self):
        self.publisher_fixture("[Source](../CONTRIBUTING.md#source-responsibilities)")
        for mode in ("--check", "--dry-run"):
            with self.subTest(mode=mode):
                result = self.publish(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside generated wiki", result.stdout)
                self.assertNotIn("could not clone", result.stderr)

    def test_publisher_rejects_link_to_page_outside_manifest(self):
        self.publisher_fixture("[Omitted](Omitted.md)")
        (self.repo / "docs/Omitted.md").write_text("# Omitted\n")
        result = self.publish("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing path", result.stdout)


if __name__ == "__main__":
    unittest.main()
