"""Check that the link checker resolves Markdown, image-link and HTML targets."""

import tempfile
import unittest
from pathlib import Path

from tools.maintenance.check_links import dangling


class DanglingLinkTest(unittest.TestCase):
    def check(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target.md").write_text("# Target\n\n## Reading the dashboard\n")
            page = root / "page.md"
            page.write_text(source)
            return dangling([page], root)

    def test_badge_link_with_missing_anchor_is_reported(self) -> None:
        bad = self.check(
            "[![Read the dashboard](https://img.shields.io/badge/docs-Read-0f766e)]"
            "(target.md#read-the-dashboard)\n"
        )
        self.assertEqual(len(bad), 1)
        self.assertIn("target.md#read-the-dashboard (missing anchor)", bad[0])

    def test_badge_link_with_existing_anchor_passes(self) -> None:
        bad = self.check(
            "[![Reading the dashboard](https://img.shields.io/badge/docs-Reading-0f766e)]"
            "(target.md#reading-the-dashboard)\n"
        )
        self.assertEqual(bad, [])

    def test_plain_link_with_missing_anchor_is_reported(self) -> None:
        self.assertEqual(len(self.check("[Read](target.md#read-the-dashboard)\n")), 1)


if __name__ == "__main__":
    unittest.main()
