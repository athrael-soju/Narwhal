"""Keep declared attribution files present in the source archive."""

import tomllib
import unittest
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PackagingTests(unittest.TestCase):
    def test_source_archive_contains_mkdocs_site_inputs(self):
        lines = (ROOT / "MANIFEST.in").read_text().splitlines()
        direct = {
            name for line in lines if line.startswith("include ") for name in line.split()[1:]
        }
        recursive = [line.split()[1:] for line in lines if line.startswith("recursive-include ")]
        self.assertIn("mkdocs.yml", direct)
        for directory in ("docs/assets", "docs/stylesheets"):
            for path in (ROOT / directory).rglob("*"):
                if path.is_file():
                    relative = path.relative_to(ROOT)
                    with self.subTest(path=relative):
                        self.assertTrue(
                            any(
                                relative.is_relative_to(root)
                                and any(fnmatch(path.name, pattern) for pattern in patterns)
                                for root, *patterns in recursive
                            )
                        )

    def test_console_scripts_match_the_reviewed_public_surface(self):
        """The wheel exposes only the five router and engine commands."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(
            set(project["scripts"]),
            {
                "narwhal-attest",
                "narwhal-canary",
                "narwhal-check",
                "narwhal-profile",
                "narwhal-serve",
            },
        )

    def test_declared_license_files_exist_and_ship_in_source_manifest(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["license-files"], ["LICENSE", "NOTICE"])
        included = {
            name
            for line in (ROOT / "MANIFEST.in").read_text().splitlines()
            if line.startswith("include ")
            for name in line.split()[1:]
        }
        for name in (*project["license-files"], "CITATION.cff", "CONTRIBUTING.md"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())
                self.assertTrue((ROOT / name).read_text().strip())
                self.assertIn(name, included)


if __name__ == "__main__":
    unittest.main()
