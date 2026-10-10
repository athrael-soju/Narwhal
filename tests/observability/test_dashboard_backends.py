import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from narwhal.backends import load as load_backend
from narwhal.backends import names
from tools.observability import artifacts, make_targets
from tools.observability.start.readiness import _expressions

SHIPPED = json.loads((artifacts.BASE / "grafana-narwhal.json").read_text())
# The vLLM rendering, pinned so a dashboard change is deliberate.
VLLM_SHA256 = "23417a1f3583f0ef12e775e73a6f81d33bfe33962afc5bd6161e5a9cf274ae5e"


def engine_prefixes(dashboard: dict) -> set[str]:
    return {
        prefix
        for expression in _expressions(dashboard["spec"]["elements"])
        for prefix in re.findall(r"\b([a-z]+):[a-z_]+", expression)
    }


class BackendDashboardTests(unittest.TestCase):
    def test_vllm_rendering_is_pinned(self):
        text = json.dumps(artifacts.render_dashboard(SHIPPED, "vllm"), indent=2) + "\n"
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), VLLM_SHA256)

    def test_every_backend_fills_every_panel_with_its_own_series(self):
        for name in names():
            with self.subTest(backend=name):
                rendered = artifacts.render_dashboard(SHIPPED, name)
                self.assertEqual(
                    rendered["spec"]["elements"].keys(), SHIPPED["spec"]["elements"].keys()
                )
                self.assertNotIn("<<", json.dumps(rendered))
                self.assertEqual(engine_prefixes(rendered), {name})
                for template in load_backend(name).metrics.dashboard_series.values():
                    self.assertIn("@sel", template)

    def test_a_panel_the_backend_cannot_fill_is_left_out(self):
        metrics = load_backend("vllm").metrics
        partial = SimpleNamespace(
            label="Engine",
            metrics=SimpleNamespace(
                dashboard_series={
                    key: value
                    for key, value in metrics.dashboard_series.items()
                    if key != "output_tokens"
                },
                dashboard_text=metrics.dashboard_text,
            ),
        )
        with mock.patch.object(artifacts, "load_backend", return_value=partial):
            rendered = artifacts.render_dashboard(SHIPPED, "partial")
        dropped = next(
            name
            for name, element in SHIPPED["spec"]["elements"].items()
            if "<<output_tokens{" in json.dumps(element)
        )
        self.assertNotIn(dropped, rendered["spec"]["elements"])
        placed = {
            item["spec"]["element"]["name"] for item in rendered["spec"]["layout"]["spec"]["items"]
        }
        self.assertEqual(placed, set(rendered["spec"]["elements"]))
        self.assertNotIn("<<", json.dumps(rendered))

    def test_staging_renders_the_dashboard_for_the_fleet_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            fleet = Path(folder) / "fleet.json"
            fleet.write_text(
                json.dumps(
                    {
                        "engine": {"backend": "sglang"},
                        "engines": [{"iid": "e0", "url": "http://192.0.2.1:8000"}],
                    }
                )
            )
            contract = make_targets.load_contract(fleet, "http://127.0.0.1:8000")
            self.assertEqual(contract.backend, "sglang")
            root = Path(folder) / "mounts"
            artifacts.stage_artifacts(contract, root=root)
            staged = json.loads((root / artifacts.NARWHAL_DASHBOARD).read_text())
        self.assertEqual(engine_prefixes(staged), {"sglang"})

    def test_targets_default_to_vllm_and_reject_an_unknown_backend(self):
        fleet = {"engines": [{"iid": "e0", "url": "http://192.0.2.1:8000"}]}
        self.assertEqual(make_targets.build_targets(fleet, "http://127.0.0.1:8000").backend, "vllm")
        with self.assertRaisesRegex(ValueError, "engine.backend 'other'"):
            make_targets.build_targets(
                {**fleet, "engine": {"backend": "other"}}, "http://127.0.0.1:8000"
            )
