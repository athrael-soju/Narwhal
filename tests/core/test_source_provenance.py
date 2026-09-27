"""Keep source artifacts verifiable after installation and source-archive rebuilds."""

import copy
import hashlib
import importlib.util
import io
import json
import tarfile
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

    def test_tracked_fixture_link_is_frozen_as_regular_bundle_and_installed_file(self):
        repository = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "narwhal_build_fixture_test", repository / "_narwhal_build.py"
        )
        backend = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"setuptools": SimpleNamespace(build_meta=MagicMock())}):
            spec.loader.exec_module(backend)
        allowed, links = backend._tracked_sources()
        resource = repository / "src/narwhal/fleet.example.json"
        target = repository / "config/fleet.example.json"
        if resource.is_symlink():
            self.assertEqual(links, {"fleet.example.json": target})
        else:
            # Source distributions retain the fixture as an ordinary package file.
            self.assertEqual(links, {})
            self.assertEqual(resource.read_bytes(), target.read_bytes())
        document, bundle = create_bundle(
            resource.parent,
            commit="a" * 40,
            version="1.0",
            verified=True,
            allowed=allowed,
            resource_links=links,
        )
        content = target.read_bytes()
        self.assertEqual(document["files"][resource.name], hashlib.sha256(content).hexdigest())
        installed = self.root / "installed"
        installed.mkdir()
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
            member = archive.getmember(resource.name)
            self.assertTrue(member.isfile())
            self.assertEqual(archive.extractfile(member).read(), content)
            for member in archive:
                if member.name == "manifest.json":
                    continue
                path = installed / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
        self.assertEqual(verify_bundle(installed, document, bundle), document)
        (installed / resource.name).unlink()
        (installed / resource.name).symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            verify_bundle(installed, document, bundle)

    def test_declared_fixture_link_cannot_escape_to_untracked_content(self):
        spec = importlib.util.spec_from_file_location(
            "narwhal_build_escape_test", Path(__file__).resolve().parents[2] / "_narwhal_build.py"
        )
        backend = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"setuptools": SimpleNamespace(build_meta=MagicMock())}):
            spec.loader.exec_module(backend)
        repository = self.root / "repository"
        package = repository / "src/narwhal"
        package.mkdir(parents=True)
        target = repository / "config/fleet.example.json"
        target.parent.mkdir()
        target.write_text('{"approved":true}')
        outside = self.root / "private.json"
        outside.write_text('{"private_token":"must-not-enter-bundle"}')
        resource = package / "fleet.example.json"
        resource.symlink_to(outside)
        tracked = (
            "120000 " + "a" * 40 + " 0\tsrc/narwhal/fleet.example.json\0"
            "100644 " + "b" * 40 + " 0\tconfig/fleet.example.json\0"
        )
        with (
            patch.object(backend, "ROOT", repository),
            patch.object(backend, "_git", return_value=tracked),
        ):
            allowed, links = backend._tracked_sources()
            with (
                patch.object(Path, "read_bytes", side_effect=AssertionError("Read private data")),
                self.assertRaisesRegex(ValueError, "symlinks"),
            ):
                create_bundle(
                    package,
                    commit="a" * 40,
                    version="1.0",
                    verified=True,
                    allowed=allowed,
                    resource_links=links,
                )
            resource.unlink()
            resource.symlink_to(target)
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "tracked regular target"):
                backend._tracked_sources()
            target.unlink()
            target.write_text("{}")
            with (
                patch.object(backend, "_git", return_value=tracked.split("\0", 1)[0] + "\0"),
                self.assertRaisesRegex(ValueError, "tracked regular target"),
            ):
                backend._tracked_sources()

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
                return_value=(
                    "100644 " + "a" * 40 + " 0\tsrc/narwhal/provenance.py\0"
                    "100644 " + "b" * 40 + " 0\tsrc/narwhal/template.json\0"
                ),
            ) as git,
            self.assertRaisesRegex(ValueError, "outside the approved"),
            backend._provenance(),
        ):
            self.fail("An ignored package secret entered the build")
        self.assertEqual(git.call_count, 1)
        self.assertFalse((self.root / METADATA).exists())
        self.assertFalse((self.root / BUNDLE).exists())
