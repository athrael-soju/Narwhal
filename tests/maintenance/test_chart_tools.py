"""The chart tools installer keeps only a binary whose SHA-256 equals its pin."""

import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "chart_tools", ROOT / "tools" / "maintenance" / "chart_tools.py"
)
assert SPEC is not None and SPEC.loader is not None
chart_tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chart_tools)


def archive(member: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        stream.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class ChartToolsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.binary = b"pinned tool"
        self.archive = archive("tool", self.binary)
        tool = {
            "version": "v1.0.0",
            "url": "https://tools.example/tool-{version}-{arch}.tar.gz",
            "member": "tool",
            "sha256": {"amd64": hashlib.sha256(self.archive).hexdigest()},
            "binary_sha256": {"amd64": hashlib.sha256(self.binary).hexdigest()},
        }
        self.downloads: list[str] = []
        for target, value in (
            (chart_tools, "TOOLS"),
            (chart_tools.platform, "machine"),
            (chart_tools.sys, "platform"),
        ):
            replacement = {
                "TOOLS": {"tool": tool},
                "machine": lambda: "x86_64",
                "platform": "linux",
            }[value]
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def urlopen(self, url, timeout):
        self.downloads.append(url)
        return io.BytesIO(self.archive)

    def install(self):
        with patch.object(chart_tools.urllib.request, "urlopen", self.urlopen):
            chart_tools.install(self.directory)

    def test_a_changed_binary_is_replaced_and_a_pinned_one_kept(self):
        (self.directory / "tool").write_bytes(b"another build that reports v1.0.0")
        self.install()
        self.assertEqual((self.directory / "tool").read_bytes(), self.binary)
        self.assertEqual(len(self.downloads), 1)
        self.install()
        self.assertEqual(len(self.downloads), 1)

    def test_an_archive_off_its_pin_is_refused(self):
        self.archive = archive("tool", b"tampered")
        with self.assertRaisesRegex(SystemExit, "not the pinned one"):
            self.install()
        self.assertFalse((self.directory / "tool").exists())

    def test_a_plain_binary_is_installed_only_with_e2e_and_only_on_its_pin(self):
        plain = b"plain tool"
        tool = {
            "version": "v2.0.0",
            "url": "https://tools.example/plain-{version}-{arch}",
            "member": None,
            "binary_sha256": {"amd64": hashlib.sha256(plain).hexdigest()},
        }
        with patch.object(chart_tools, "E2E_TOOLS", {"plain": tool}):
            self.install()
            self.assertFalse((self.directory / "plain").exists())
            served = {"data": b"tampered plain tool"}

            def urlopen(url, timeout):
                return io.BytesIO(served["data"] if "plain" in url else self.archive)

            with (
                patch.object(chart_tools.urllib.request, "urlopen", urlopen),
                self.assertRaisesRegex(SystemExit, "differs from the pinned binary"),
            ):
                chart_tools.install(self.directory, e2e=True)
            self.assertFalse((self.directory / "plain").exists())
            served["data"] = plain
            with patch.object(chart_tools.urllib.request, "urlopen", urlopen):
                chart_tools.install(self.directory, e2e=True)
        self.assertEqual((self.directory / "plain").read_bytes(), plain)


if __name__ == "__main__":
    unittest.main()
