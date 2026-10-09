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
                    artifacts.CONTROL_TARGETS,
                    artifacts.CONTROL_TOKEN_FILE,
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

    def rows(self) -> list[dict]:
        return [row["spec"] for row in self.dashboard["spec"]["layout"]["spec"]["rows"]]

    def test_console_views_sit_among_panels_of_the_shipped_dashboard(self):
        spec = self.dashboard["spec"]
        self.assertEqual(self.dashboard["metadata"]["name"], artifacts.FLEET_CONTROL_UID)
        self.assertEqual(self.dashboard["apiVersion"], self.shipped["apiVersion"])
        shipped = self.shipped["spec"]
        for name in ("panel-8", "panel-37", "panel-50", "panel-51"):
            self.assertEqual(spec["elements"][name], shipped["elements"][name])
        self.assertEqual(spec["variables"], shipped["variables"])
        self.assertEqual(spec["annotations"][: len(shipped["annotations"])], shipped["annotations"])
        self.assertEqual(spec["timeSettings"], shipped["timeSettings"])
        rows = self.rows()
        self.assertEqual(
            [(row["title"], row["collapse"]) for row in rows],
            [
                ("", False),
                ("Load and configuration", False),
                ("Controller", True),
                ("Request & Recovery", True),
            ],
        )
        self.assertTrue(rows[0]["hideHeader"])
        placed = [
            item["spec"]["element"]["name"]
            for row in rows
            for item in row["layout"]["spec"]["items"]
        ]
        self.assertEqual(sorted(placed), sorted(spec["elements"]))
        self.assertEqual(len(placed), len(set(placed)))
        for view in artifacts.CONSOLE_VIEWS:
            self.assertIn(f"console-{view}", placed)
        ids = [element["spec"]["id"] for element in spec["elements"].values()]
        self.assertEqual(len(ids), len(set(ids)))
        recovery = [item["spec"] for item in rows[-1]["layout"]["spec"]["items"]][:2]
        self.assertEqual(
            [(item["element"]["name"], item["x"], item["y"]) for item in recovery],
            [("panel-52", 0, 0), ("panel-12", 12, 0)],
        )
        self.assertEqual(spec["elements"]["panel-11"]["spec"]["id"], 11)

    def test_each_grid_row_fits_the_24_columns_without_overlap(self):
        for row in self.rows():
            cells = set()
            for item in row["layout"]["spec"]["items"]:
                box = item["spec"]
                self.assertLessEqual(box["x"] + box["width"], 24)
                for x in range(box["x"], box["x"] + box["width"]):
                    for y in range(box["y"], box["y"] + box["height"]):
                        self.assertNotIn((x, y), cells, box["element"]["name"])
                        cells.add((x, y))

    def test_derived_panels_reuse_the_shipped_queries(self):
        elements = self.dashboard["spec"]["elements"]
        shipped = self.shipped["spec"]["elements"]

        def exprs(panel):
            return [
                q["spec"]["query"]["spec"]["expr"] for q in panel["spec"]["data"]["spec"]["queries"]
            ]

        requests = exprs(shipped["panel-105"])
        refs = "ABCDEFGHI"
        self.assertEqual(
            exprs(elements["panel-requests"]),
            [requests[refs.index(ref)] for ref, _ in artifacts.REQUEST_STATS],
        )
        self.assertEqual(exprs(elements["panel-latency"]), exprs(shipped["panel-106"])[:2])
        outcomes = exprs(elements["panel-11"])
        self.assertEqual(outcomes[:2], exprs(shipped["panel-11"])[:2])
        for name in ("refused", "rejected", "failed", "expired"):
            self.assertIn(f"narwhal_{name}_total", outcomes[2])

    def test_fleet_control_annotations_read_the_control_metrics(self):
        names = {a["spec"]["name"]: a["spec"] for a in self.dashboard["spec"]["annotations"]}
        actions = names["Fleet control actions"]["query"]["spec"]
        self.assertEqual(actions["expr"], "narwhal_control_action_started_ms")
        self.assertTrue(actions["useValueForTime"])
        jobs = names["Load jobs"]["query"]["spec"]
        self.assertIn("narwhal_control_load_job_running", jobs["expr"])
        self.assertFalse(jobs["useValueForTime"])
        for name in ("Fleet control actions", "Load jobs"):
            self.assertEqual((names[name]["enable"], names[name]["hide"]), (True, True))

    def test_console_views_are_scripted_and_escape_their_url(self):
        for view in artifacts.CONSOLE_VIEWS:
            options = self.dashboard["spec"]["elements"][f"console-{view}"]["spec"]["vizConfig"]
            self.assertEqual(options["kind"], "text")
            self.assertEqual(options["spec"]["options"]["mode"], "html")
            content = options["spec"]["options"]["content"]
            self.assertIn(
                f'src="http://127.0.0.1:18020/console?view={view}&amp;theme=dark"', content
            )
            self.assertIn(f'sandbox="{artifacts.CONSOLE_SANDBOX}"', content)
            self.assertIn('referrerpolicy="no-referrer"', content)
            self.assertNotIn("$", content)
        escaped = artifacts.fleet_control_dashboard(self.shipped, "http://h/a&b")
        frame = escaped["spec"]["elements"]["console-engines"]["spec"]["vizConfig"]["spec"]
        self.assertIn(
            'src="http://h/a&amp;b?view=engines&amp;theme=dark"', frame["options"]["content"]
        )

    def test_the_console_page_offers_every_framed_view(self):
        page = (artifacts.BASE.parent / "fleet_control" / "console.html").read_text()
        for view in artifacts.CONSOLE_VIEWS:
            self.assertIn(f'id="view-{view}"', page)

    def test_control_target_needs_an_http_origin_and_the_token(self):
        self.assertIsNone(artifacts.control_target({}))
        target = artifacts.control_target(
            {
                artifacts.CONTROL_METRICS_URL_ENV: "http://127.0.0.1:8020",
                artifacts.CONTROL_TOKEN_ENV: "t" * 32,
            }
        )
        self.assertEqual(target, artifacts.ControlTarget("127.0.0.1:8020", "t" * 32))
        for env in (
            {artifacts.CONTROL_METRICS_URL_ENV: "http://127.0.0.1:8020"},
            {
                artifacts.CONTROL_METRICS_URL_ENV: "https://127.0.0.1:8020",
                "NARWHAL_CONTROL_TOKEN": "t",
            },
            {artifacts.CONTROL_METRICS_URL_ENV: "http://127.0.0.1", "NARWHAL_CONTROL_TOKEN": "t"},
        ):
            with (
                self.subTest(env=env),
                self.assertRaisesRegex(ValueError, artifacts.CONTROL_METRICS_URL_ENV),
            ):
                artifacts.control_target(env)

    def test_staging_writes_the_control_target_and_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mounts"
            contract = TargetContract("127.0.0.1:8000", ())
            artifacts.stage_artifacts(contract, root=root)
            self.assertEqual(json.loads((root / artifacts.CONTROL_TARGETS).read_text()), [])
            self.assertEqual((root / artifacts.CONTROL_TOKEN_FILE).read_text(), "")
            control = artifacts.ControlTarget("127.0.0.1:8020", "t" * 32)
            artifacts.stage_artifacts(contract, control=control, root=root)
            self.assertEqual(
                json.loads((root / artifacts.CONTROL_TARGETS).read_text()),
                [{"targets": ["127.0.0.1:8020"]}],
            )
            self.assertEqual((root / artifacts.CONTROL_TOKEN_FILE).read_text(), "t" * 32)

    def test_console_url_defaults_to_the_tunnel_port_and_refuses_unsafe_values(self):
        self.assertEqual(artifacts.console_url({}), "http://127.0.0.1:18020/console")
        for value in (
            "https://[::1]:8443/console",
            "http://control.example:18020/console",
            "/fleet-control/console",
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
            "//control.example/console",
            "fleet-control/console",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, artifacts.CONSOLE_URL_ENV),
            ):
                artifacts.console_url({artifacts.CONSOLE_URL_ENV: value})
