"""Monitoring mounts remain readable when deployment files use private permissions."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from narwhal.observability import artifacts
from narwhal.observability.make_targets import TargetContract
from tests.fixtures import ROOT


class MonitoringArtifactTests(unittest.TestCase):
    def test_repository_asset_paths_match_the_packaged_source(self):
        for relative in ("compose.yml", *artifacts.FILES):
            legacy = ROOT / "tools" / "observability" / relative
            self.assertEqual(legacy.read_bytes(), (artifacts.BASE / relative).read_bytes())

    def test_private_checkout_produces_readable_mounts_without_exposing_role_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            source = checkout / "tools" / "observability"
            root = checkout / "runs" / "observability" / "mounts"
            contract = TargetContract("127.0.0.1:8000", (("e1", "192.0.2.1:8002"),))
            previous_umask = os.umask(0o077)
            try:
                for relative in artifacts.FILES:
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes((artifacts.BASE / relative).read_bytes())
                secret = checkout / ".env.router"
                secret.write_text("SYNTHETIC_SECRET=private\n")
                original_modes = {path: path.stat().st_mode for path in checkout.rglob("*")}
                artifacts.stage_artifacts(contract, root, source)
                expected = {
                    *artifacts.FILES.values(),
                    "prometheus/targets/router.json",
                    "prometheus/targets/engines.json",
                }
                self.assertEqual(
                    {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}, expected
                )
                # A bind mount exposes its root directly. Container UIDs need search/read
                # within each mount; host ancestors retain the deployment account's access.
                for mount in root.iterdir():
                    for path in [mount, *mount.rglob("*")]:
                        mode = path.stat().st_mode
                        required = 0o005 if path.is_dir() else 0o004
                        self.assertEqual(mode & required, required, str(path))
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    json.loads((root / "prometheus/targets/engines.json").read_text()),
                    [{"targets": ["192.0.2.1:8002"], "labels": {"iid": "e1"}}],
                )
                for relative, target in artifacts.FILES.items():
                    self.assertEqual((root / target).read_bytes(), (source / relative).read_bytes())
                for path, mode in original_modes.items():
                    self.assertEqual(path.stat().st_mode, mode, str(path))
                # Repair a prior private staging directory during the same startup command.
                for path in root.rglob("*"):
                    path.chmod(0o700 if path.is_dir() else 0o600)
                artifacts.stage_artifacts(contract, root, source)
                for path in root.rglob("*"):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o755 if path.is_dir() else 0o644)
            finally:
                os.umask(previous_umask)

    def test_rejects_symlinked_mount_before_changing_external_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mounts"
            root.mkdir()
            external = Path(temporary) / "private"
            external.mkdir(mode=0o700)
            (root / "prometheus").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "real directory"):
                artifacts.stage_artifacts(TargetContract("127.0.0.1:8000", ()), root)
            self.assertEqual(external.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(external.iterdir()), [])
