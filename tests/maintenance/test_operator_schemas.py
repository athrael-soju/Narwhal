"""The Prometheus Operator schemas `make chart-check` validates the chart's monitors with come
only from the pinned CRDs, and refuse fields the CRDs do not name."""

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "operator_schemas", ROOT / "tools" / "maintenance" / "operator_schemas.py"
)
assert SPEC is not None and SPEC.loader is not None
operator_schemas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator_schemas)

# A CRD as the release's stripped-down file holds one, with a bare `=` among its enum values.
CRDS = b"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: not-a-crd
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  names:
    kind: Widget
  versions:
    - name: v1
      served: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                matchType:
                  type: string
                  enum: ["!=", =, =~]
                extra:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
                  properties:
                    known:
                      type: string
    - name: v0
      served: false
      schema:
        openAPIV3Schema:
          type: object
"""


class OperatorSchemaTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.downloads: list[str] = []
        patcher = patch.object(operator_schemas, "SHA256", hashlib.sha256(CRDS).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)

    def urlopen(self, data: bytes):
        def opened(url, timeout):
            self.downloads.append(url)
            return io.BytesIO(data)

        return patch.object(operator_schemas.urllib.request, "urlopen", opened)

    def test_each_served_version_becomes_a_closed_schema(self):
        with self.urlopen(CRDS):
            [path] = operator_schemas.install(self.directory)
        self.assertEqual(path, self.directory / "example.com" / "widget_v1.json")
        schema = json.loads(path.read_text())
        self.assertIs(schema["additionalProperties"], False)
        spec = schema["properties"]["spec"]
        self.assertIs(spec["additionalProperties"], False)
        self.assertEqual(spec["properties"]["matchType"]["enum"], ["!=", "=", "=~"])
        # A field that keeps unknown fields stays open.
        self.assertNotIn("additionalProperties", spec["properties"]["extra"])

    def test_only_the_pinned_download_is_used(self):
        with self.urlopen(CRDS + b"# changed\n"), self.assertRaises(SystemExit) as raised:
            operator_schemas.install(self.directory)
        self.assertIn("not the pinned one", str(raised.exception))
        self.assertEqual(list(self.directory.iterdir()), [])
        with self.urlopen(CRDS):
            operator_schemas.install(self.directory)
        # The pinned download stays and is not fetched again.
        with self.urlopen(b"unused"):
            operator_schemas.install(self.directory)
        self.assertEqual(len(self.downloads), 2)
        # A changed download in the directory is fetched again.
        (self.directory / operator_schemas.BUNDLE).write_bytes(b"changed")
        with self.urlopen(CRDS):
            operator_schemas.install(self.directory)
        self.assertEqual(len(self.downloads), 3)

    def test_the_pin_names_a_release(self):
        self.assertIn(f"/download/{operator_schemas.VERSION}/", operator_schemas.URL)


if __name__ == "__main__":
    unittest.main()
