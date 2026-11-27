import contextlib
import hashlib
import io
import json
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def heredoc(guide: str, marker: str) -> str:
    body = re.search(rf"<<'{marker}'\n(.*?)\n[ \t]*{marker}\n", guide, re.S)
    assert body is not None, marker
    return textwrap.dedent(body.group(1))


class DeploymentHTTPProbeTests(unittest.TestCase):
    def test_connector_protocol_capture_reads_installed_constant_and_source_hash(self):
        from narwhal.backends.vllm.attestation import NIXL_CAPTURE, NIXL_CAPTURE_TAG

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
                                "src/narwhal/backends/vllm/attestation.py:NIXL_CAPTURE",
                                "exec",
                            ),
                            {},
                        )
                    else:
                        with self.assertRaisesRegex(ValueError, "positive integer"):
                            exec(
                                compile(
                                    NIXL_CAPTURE,
                                    "src/narwhal/backends/vllm/attestation.py:NIXL_CAPTURE",
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

    def test_exact_api_version_controls_gate_while_distribution_keeps_build_suffix(self):
        guide = (
            Path(__file__).resolve().parents[2] / "docs/deploy/03-Validate-Engines.md"
        ).read_text()
        script = heredoc(guide, "PY_ENGINE")
        for api_version in ("0.29.0", "0.29.1", "0.29.0+other"):
            with self.subTest(api_version=api_version), tempfile.TemporaryDirectory() as folder:
                run = Path(folder)
                plan = {
                    "endpoint": "http://engine.invalid:8000",
                    "expected_packages": {"vllm": "0.29.0+rocm100"},
                }
                data = json.dumps(plan).encode()
                (run / "launch.json").write_bytes(data)
                (run / "checked.json").write_text(
                    json.dumps(
                        {
                            "plan_sha256": hashlib.sha256(data).hexdigest(),
                            "engine_version": "0.29.0",
                        }
                    )
                )
                responses = [
                    b"",
                    json.dumps({"version": api_version}).encode(),
                    b'{"data": [{"id": "synthetic"}]}',
                    b"process_start_time_seconds 123\n",
                    b'{"choices": [{"text": " blue"}]}',
                ]
                with (
                    patch.dict(
                        "os.environ",
                        {"ENGINE_RUN": folder, "NARWHAL_ENGINE_MODEL_NAME": "synthetic"},
                        clear=True,
                    ),
                    patch(
                        "urllib.request.urlopen",
                        side_effect=[io.BytesIO(value) for value in responses],
                    ) as request,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if api_version == "0.29.0":
                        exec(
                            compile(script, "docs/deploy/03-Validate-Engines.md:PY_ENGINE", "exec"),
                            {},
                        )
                        self.assertEqual(request.call_count, 5)
                        self.assertTrue((run / "completion.json").exists())
                    else:
                        with self.assertRaisesRegex(AssertionError, "checked image expects"):
                            exec(
                                compile(
                                    script, "docs/deploy/03-Validate-Engines.md:PY_ENGINE", "exec"
                                ),
                                {},
                            )
                        self.assertEqual(request.call_count, 2)
                        self.assertFalse((run / "models.json").exists())
                self.assertEqual(
                    json.loads((run / "version.json").read_text())["version"], api_version
                )
