"""Keep source artifacts verifiable after installation and source-archive rebuilds."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from narwhal import _source_bundle as bundle_helpers
from narwhal import provenance
from narwhal._source_bundle import BUNDLE, METADATA, create_bundle, package_files, verify_bundle


class SourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "provenance.py").write_text("# synthetic package\n")
        (self.root / "template.json").write_text('{"schema_version":1}\n')
        self.document, self.bundle = create_bundle(
            self.root, commit="a" * 40, version="1.0", verified=True
        )
        (self.root / METADATA).write_text(json.dumps(self.document))
        (self.root / BUNDLE).write_bytes(self.bundle)

    def test_bundle_bytes_are_deterministic_and_bind_resources_and_source(self):
        document, bundle = create_bundle(self.root, commit="a" * 40, version="1.0", verified=True)
        self.assertEqual((document, bundle), (self.document, self.bundle))
        self.assertEqual(verify_bundle(self.root, document, bundle), document)
        with (
            patch.object(provenance, "__file__", str(self.root / "provenance.py")),
            patch.object(provenance, "_version", return_value="1.0"),
        ):
            self.assertEqual(
                provenance.verified_source(),
                {
                    "commit": "a" * 40,
                    "distribution_version": "1.0",
                    "wheel_sha256": None,
                    "bundle_sha256": document["bundle_sha256"],
                },
            )
        (self.root / "template.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "differs"):
            verify_bundle(self.root, document, bundle)

    def test_dirty_missing_and_changed_artifacts_cannot_authorize_mutation(self):
        with (
            patch.object(provenance, "__file__", str(self.root / "provenance.py")),
            patch.object(provenance, "_version", return_value="1.0"),
        ):
            (self.root / BUNDLE).write_bytes(self.bundle + b"changed")
            with self.assertRaises(ValueError):
                provenance.verified_source()
            document, bundle = create_bundle(
                self.root, commit="a" * 40, version="1.0", verified=False
            )
            (self.root / METADATA).write_text(json.dumps(document))
            (self.root / BUNDLE).write_bytes(bundle)
            with self.assertRaisesRegex(ValueError, "clean verified"):
                provenance.verified_source()
            (self.root / METADATA).unlink()
            with self.assertRaises(ValueError):
                provenance.verified_source()

    def test_manifest_relabel_and_extra_package_file_are_rejected(self):
        modified = copy.deepcopy(self.document)
        modified["commit"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            verify_bundle(self.root, modified, self.bundle)
        (self.root / "extra.py").write_text("unexpected = True\n")
        with self.assertRaisesRegex(ValueError, "manifest"):
            verify_bundle(self.root, self.document, self.bundle)

    def test_untracked_and_ignored_package_files_are_rejected_before_reading(self):
        private = self.root / ".env"
        private.write_text("PRIVATE_TOKEN=must-not-enter-a-source-bundle")
        allowed = set(self.document["files"])
        with (
            patch.object(
                Path, "read_bytes", side_effect=AssertionError("Read before manifest check")
            ),
            self.assertRaisesRegex(ValueError, "outside the approved"),
        ):
            package_files(self.root, allowed=allowed)
        with self.assertRaisesRegex(ValueError, "outside the approved"):
            create_bundle(self.root, commit="a" * 40, version="1.0", verified=True, allowed=allowed)

    def test_source_archive_keeps_original_provenance_inside_a_different_git_checkout(self):
        path = Path(__file__).resolve().parents[2] / "_narwhal_build.py"
        spec = importlib.util.spec_from_file_location("narwhal_build_test", path)
        backend = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"setuptools": SimpleNamespace(build_meta=MagicMock())}):
            spec.loader.exec_module(backend)
        helpers = type(
            "Helpers",
            (),
            {"METADATA": METADATA, "BUNDLE": BUNDLE, "verify_bundle": staticmethod(verify_bundle)},
        )
        with (
            patch.object(backend, "PACKAGE", self.root),
            patch.object(backend, "_helpers", return_value=helpers),
            patch.object(
                backend.subprocess,
                "check_output",
                side_effect=AssertionError("Must preserve source archive identity"),
            ),
            backend._provenance(),
        ):
            self.assertEqual(json.loads((self.root / METADATA).read_bytes())["commit"], "a" * 40)
        self.assertTrue((self.root / BUNDLE).is_file())

    def test_backend_rejects_ignored_package_secrets_before_minting_provenance(self):
        path = Path(__file__).resolve().parents[2] / "_narwhal_build.py"
        spec = importlib.util.spec_from_file_location("narwhal_build_private_test", path)
        backend = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"setuptools": SimpleNamespace(build_meta=MagicMock())}):
            spec.loader.exec_module(backend)
        (self.root / METADATA).unlink()
        (self.root / BUNDLE).unlink()
        (self.root / ".env").write_text("PRIVATE_TOKEN=ignored-secret")
        project = self.root.parent / (self.root.name + "-project.toml")
        project.write_text('[project]\nversion="1.0"\n')
        self.addCleanup(project.unlink)
        original_read_text = Path.read_text

        def read_text(selected, *args, **kwargs):
            if selected == self.root / "pyproject.toml":
                return project.read_text()
            return original_read_text(selected, *args, **kwargs)

        with (
            patch.object(backend, "ROOT", self.root),
            patch.object(backend, "PACKAGE", self.root),
            patch.object(backend, "_helpers", return_value=bundle_helpers),
            patch.object(Path, "read_text", read_text),
            patch.object(
                backend.subprocess,
                "check_output",
                return_value="src/narwhal/provenance.py\0src/narwhal/template.json\0",
            ) as git,
            self.assertRaisesRegex(ValueError, "outside the approved"),
            backend._provenance(),
        ):
            self.fail("An ignored package secret entered the build")
        self.assertEqual(git.call_count, 1)
        self.assertFalse((self.root / METADATA).exists())
        self.assertFalse((self.root / BUNDLE).exists())
