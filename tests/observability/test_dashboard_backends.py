import base64
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
RENDERED_SHA256 = "d56b59249c9f9679f49fc5f5e49b193ffde0c31a452bc8695f045a1d6a5eb4ac"


def engine_prefixes(dashboard: dict) -> set[str]:
    return {
        prefix
        for expression in _expressions(dashboard["spec"]["elements"])
        for prefix in re.findall(r"\b([a-z]+):[a-z_]+", expression)
    }


class BackendDashboardTests(unittest.TestCase):
    def test_rendering_is_pinned(self):
        text = json.dumps(artifacts.render_dashboard(SHIPPED), indent=2) + "\n"
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), RENDERED_SHA256)

    def test_every_panel_queries_every_backend_series(self):
        rendered = artifacts.render_dashboard(SHIPPED)
        self.assertEqual(rendered["spec"]["elements"].keys(), SHIPPED["spec"]["elements"].keys())
        self.assertNotIn("<<", json.dumps(rendered))
        self.assertEqual(engine_prefixes(rendered), set(names()))
        for name in names():
            for template in load_backend(name).metrics.dashboard_series.values():
                self.assertIn("@sel", template)

    def test_a_series_query_serves_whichever_backend_the_engines_run(self):
        [running] = [
            q["spec"]["query"]["spec"]["expr"]
            for q in SHIPPED["spec"]["elements"]["panel-7"]["spec"]["data"]["spec"]["queries"]
            if q["spec"]["query"]["spec"]["expr"].startswith("<<running{")
        ]
        rendered = artifacts.render_dashboard(SHIPPED)["spec"]["elements"]["panel-7"]
        selector = 'job="engines",iid=~"$iid"'
        union = " or ".join(
            f"({load_backend(name).metrics.dashboard_series['running'].replace('@sel', selector)})"
            for name in names()
        )
        exprs = [
            q["spec"]["query"]["spec"]["expr"] for q in rendered["spec"]["data"]["spec"]["queries"]
        ]
        self.assertIn(running.replace(f"<<running{{{selector}}}>>", f"({union})"), exprs)

    def test_engine_table_shows_each_engine_backend_icon(self):
        for name in names():
            with self.subTest(backend=name):
                table = artifacts.render_dashboard(SHIPPED)["spec"]["elements"]["panel-7"]
                overrides = table["spec"]["vizConfig"]["spec"]["fieldConfig"]["overrides"]
                [badge] = [o for o in overrides if o["matcher"]["options"] == "Backend"]
                self.assertEqual(
                    badge["properties"][0]["value"],
                    [
                        {
                            "type": "value",
                            "options": {
                                b: {
                                    "text": "data:image/png;base64,"
                                    + base64.b64encode(load_backend(b).icon).decode()
                                }
                                for b in names()
                            },
                        }
                    ],
                )
                exprs = [
                    q["spec"]["query"]["spec"]["expr"]
                    for q in table["spec"]["data"]["spec"]["queries"]
                ]
                self.assertIn('max by(iid, backend) (up{job="engines",iid=~"$iid"})', exprs)

    def test_a_panel_no_backend_can_fill_is_left_out(self):
        metrics = load_backend("vllm").metrics
        partial = SimpleNamespace(
            metrics=SimpleNamespace(
                dashboard_series={
                    key: value
                    for key, value in metrics.dashboard_series.items()
                    if key != "output_tokens"
                },
            ),
        )
        with mock.patch.object(artifacts, "load_backend", return_value=partial):
            rendered = artifacts.render_dashboard(SHIPPED)
        dropped = next(
            name
            for name, element in SHIPPED["spec"]["elements"].items()
            if "<<output_tokens{" in json.dumps(element)
        )
        self.assertNotIn(dropped, rendered["spec"]["elements"])
        placed = {
            item["spec"]["element"]["name"]
            for row in rendered["spec"]["layout"]["spec"]["rows"]
            for item in row["spec"]["layout"]["spec"]["items"]
        }
        self.assertEqual(placed, set(rendered["spec"]["elements"]))
        self.assertNotIn("<<", json.dumps(rendered))

    def test_staging_renders_the_dashboard_for_every_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "mounts"
            artifacts.stage_artifacts(make_targets.TargetContract("127.0.0.1:8000"), root=root)
            staged = json.loads((root / artifacts.NARWHAL_DASHBOARD).read_text())
        self.assertEqual(engine_prefixes(staged), set(names()))
