"""Monitoring mounts remain readable when deployment files use private permissions."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.observability import artifacts
from tools.observability.make_targets import TargetContract


class MonitoringArtifactTests(unittest.TestCase):
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
                artifacts.stage_artifacts(contract, root=root, source=source)
                expected = {
                    *artifacts.FILES.values(),
                    artifacts.FLEET_CONTROL_DASHBOARD,
                    "prometheus/targets/router.json",
                    "prometheus/targets/engines.json",
                }
                self.assertEqual(
                    {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}, expected
                )
                # Container UIDs need search/read on each mount root and its contents;
                # host ancestors retain the deployment account's access.
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
                # Staging restores readable modes on a prior private staging directory.
                for path in root.rglob("*"):
                    path.chmod(0o700 if path.is_dir() else 0o600)
                artifacts.stage_artifacts(contract, root=root, source=source)
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
                artifacts.stage_artifacts(TargetContract("127.0.0.1:8000", ()), root=root)
            self.assertEqual(external.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(external.iterdir()), [])


class FleetControlDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shipped = json.loads((artifacts.BASE / "grafana-narwhal.json").read_text())
        self.dashboard = artifacts.fleet_control_dashboard(
            self.shipped, "http://127.0.0.1:18020/console"
        )

    def test_console_frame_sits_beside_copies_of_the_shipped_panels(self):
        spec = self.dashboard["spec"]
        self.assertEqual(self.dashboard["metadata"]["name"], artifacts.FLEET_CONTROL_UID)
        self.assertEqual(self.dashboard["apiVersion"], self.shipped["apiVersion"])
        shipped = self.shipped["spec"]
        for panel in artifacts.CONSOLE_PANELS:
            name = f"panel-{panel}"
            self.assertEqual(spec["elements"][name], shipped["elements"][name])
        self.assertEqual(spec["variables"], shipped["variables"])
        self.assertEqual(spec["annotations"], shipped["annotations"])
        self.assertEqual(spec["timeSettings"], shipped["timeSettings"])
        items = [item["spec"] for item in spec["layout"]["spec"]["items"]]
        names = [item["element"]["name"] for item in items]
        self.assertEqual(
            names, ["panel-console", *(f"panel-{p}" for p in artifacts.CONSOLE_PANELS)]
        )
        console, *panels = items
        self.assertEqual(
            (console["x"], console["y"], console["width"]), (0, 0, artifacts.CONSOLE_WIDTH)
        )
        self.assertEqual(console["height"], sum(item["height"] for item in panels))
        for item in panels:
            self.assertEqual(item["x"], artifacts.CONSOLE_WIDTH)
            self.assertEqual(item["width"], 24 - artifacts.CONSOLE_WIDTH)
        ids = [element["spec"]["id"] for element in spec["elements"].values()]
        shipped_ids = {element["spec"]["id"] for element in shipped["elements"].values()}
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(spec["elements"]["panel-console"]["spec"]["id"], shipped_ids)

    def test_console_frame_is_scripted_and_escapes_its_url(self):
        options = self.dashboard["spec"]["elements"]["panel-console"]["spec"]["vizConfig"]
        self.assertEqual(options["kind"], "text")
        content = options["spec"]["options"]["content"]
        self.assertEqual(options["spec"]["options"]["mode"], "html")
        self.assertIn('src="http://127.0.0.1:18020/console"', content)
        self.assertIn(f'sandbox="{artifacts.CONSOLE_SANDBOX}"', content)
        self.assertIn('referrerpolicy="no-referrer"', content)
        self.assertNotIn("$", content)
        escaped = artifacts.fleet_control_dashboard(self.shipped, "http://h/a&b")
        frame = escaped["spec"]["elements"]["panel-console"]["spec"]["vizConfig"]["spec"]
        self.assertIn('src="http://h/a&amp;b"', frame["options"]["content"])

    def test_console_url_defaults_to_the_tunnel_port_and_refuses_unsafe_values(self):
        self.assertEqual(artifacts.console_url({}), "http://127.0.0.1:18020/console")
        for value in (
            "https://[::1]:8443/console",
            "http://control.example:18020/console",
        ):
            with self.subTest(value=value):
                self.assertEqual(artifacts.console_url({artifacts.CONSOLE_URL_ENV: value}), value)
        for value in (
            "",
            "javascript:alert(1)",
            "ftp://127.0.0.1/console",
            "http://user:secret@127.0.0.1/console",
            "http://127.0.0.1/console?x=1",
            "http://127.0.0.1/console#top",
            'http://127.0.0.1/"><script>',
            "http:///console",
            "http://127.0.0.1/con sole",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, artifacts.CONSOLE_URL_ENV),
            ):
                artifacts.console_url({artifacts.CONSOLE_URL_ENV: value})
