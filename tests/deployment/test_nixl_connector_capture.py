import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class NixlConnectorCaptureTests(unittest.TestCase):
    def test_connector_protocol_capture_reads_installed_constant_and_source_hash(self):
        from narwhal.backends.vllm.inspections import NIXL_CAPTURE, NIXL_CAPTURE_TAG

        for version in (17, 0, "1.4.1", True):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "metadata.py"
                source.write_text(f"NIXL_CONNECTOR_VERSION = {version!r}\n")
                module = ModuleType("vllm.distributed.kv_transfer.kv_connector.v1.nixl.metadata")
                module.__file__ = str(source)
                module.NIXL_CONNECTOR_VERSION = version
                with (
                    patch("importlib.import_module", return_value=module) as load,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    if version == 17:
                        exec(
                            compile(
                                NIXL_CAPTURE,
                                "src/narwhal/backends/vllm/inspections.py:NIXL_CAPTURE",
                                "exec",
                            ),
                            {},
                        )
                    else:
                        with self.assertRaisesRegex(ValueError, "positive integer"):
                            exec(
                                compile(
                                    NIXL_CAPTURE,
                                    "src/narwhal/backends/vllm/inspections.py:NIXL_CAPTURE",
                                    "exec",
                                ),
                                {},
                            )
                load.assert_called_once_with(module.__name__)
                if version == 17:
                    self.assertTrue(output.getvalue().startswith("\n" + NIXL_CAPTURE_TAG))
                    record = json.loads(output.getvalue().split(NIXL_CAPTURE_TAG, 1)[1])
                    self.assertEqual(record["nixl_connector_version"], 17)
                    self.assertEqual(
                        record["module_sha256"], hashlib.sha256(source.read_bytes()).hexdigest()
                    )
                else:
                    self.assertEqual(output.getvalue(), "")
