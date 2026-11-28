"""The CRD chart: it installs the CRD that `fleet_resource` defines, keeps it when its release is
uninstalled, and follows the release version.

The rendering tests need helm, as `test_helm_chart` finds it, and skip without it.
"""

import subprocess
import tomllib
import unittest

import yaml

from narwhal.deployment import fleet_resource
from tests.deployment.test_helm_chart import HELM, ROOT

CHART = ROOT / "deploy" / "helm" / "narwhal-crds"


class Chart(unittest.TestCase):
    def test_the_chart_follows_the_release_version(self):
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(chart["version"], project["version"])
        self.assertEqual(chart["appVersion"], project["version"])
        self.assertEqual(chart["kubeVersion"], ">=1.30.0-0")

    def test_release_please_bumps_the_chart(self):
        config = (ROOT / "release-please-config.json").read_text()
        for field in ("version", "appVersion"):
            self.assertIn(
                f'{{"type": "yaml", "path": "deploy/helm/narwhal-crds/Chart.yaml", '
                f'"jsonpath": "$.{field}"}}',
                config,
            )

    def test_the_crd_lies_in_templates_so_an_upgrade_updates_it(self):
        self.assertFalse((CHART / "crds").exists())
        self.assertEqual(
            sorted(path.name for path in (CHART / "templates").iterdir()),
            [f"{fleet_resource.PLURAL}.yaml"],
        )


@unittest.skipUnless(HELM, "needs helm")
class Rendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert HELM is not None
        output = subprocess.run(
            [HELM, "template", "narwhal-crds", str(CHART), "--namespace", "example"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        cls.documents = [document for document in yaml.safe_load_all(output) if document]

    def test_the_chart_installs_the_fleet_crd(self):
        [crd] = self.documents
        labels = crd["metadata"].pop("labels")
        self.assertEqual(crd, fleet_resource.crd())
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        self.assertEqual(
            labels,
            {
                "app.kubernetes.io/name": "narwhal-crds",
                "app.kubernetes.io/part-of": "narwhal",
                "app.kubernetes.io/managed-by": "Helm",
                "app.kubernetes.io/version": chart["appVersion"],
                "helm.sh/chart": f"narwhal-crds-{chart['version']}",
            },
        )

    def test_uninstalling_the_release_keeps_the_crd_and_its_fleets(self):
        [crd] = self.documents
        annotations = crd["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/resource-policy"], "keep")
        self.assertEqual(annotations[fleet_resource.SCHEMA_SHA256], fleet_resource.schema_digest())


if __name__ == "__main__":
    unittest.main()
