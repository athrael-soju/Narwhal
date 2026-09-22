"""Execute the documented HTTP gate with distinct distribution and API versions."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class DeploymentHTTPProbeTests(unittest.TestCase):
    def test_transfer_mode_capture_uses_resolved_class_and_preserves_evidence(self):
        guide = (Path(__file__).resolve().parents[2] / "docs/Deploy.md").read_text()
        script = guide.split("<<'PY_TRANSFER_MODE'\n", 1)[1].split("\nPY_TRANSFER_MODE", 1)[0]
        cases = (
            (["NixlPullConnector"], "pull"),
            (["NixlPushConnector"], "push"),
            (["NixlConnector"], None),
            (["NixlPullConnector", "NixlPushConnector"], None),
            ([], None),
        )
        for names, mode in cases:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as folder:
                run = Path(folder)
                data = json.dumps(
                    {"connector": {"kv_connector": "NixlConnector", "kv_role": "kv_both"}}
                ).encode()
                (run / "launch.json").write_bytes(data)
                checked = {
                    "plan_sha256": hashlib.sha256(data).hexdigest(),
                    "image_id": "sha256:" + "a" * 64,
                }
                (run / "checked.json").write_text(json.dumps(checked))
                log = run / "image-check.log"
                log.write_text(
                    "startup diagnostic\n"
                    + "\n".join(
                        json.dumps(
                            {
                                "connector": "vllm.distributed.kv_transfer.kv_connector."
                                "v1.nixl.connector." + name
                            }
                        )
                        for name in names
                    )
                )
                original = log.read_bytes()
                with (
                    patch.dict("os.environ", {"ENGINE_RUN": folder}),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if mode is None:
                        with self.assertRaises(SystemExit):
                            exec(compile(script, "docs/Deploy.md:PY_TRANSFER_MODE", "exec"), {})
                        self.assertFalse((run / "transfer-mode.json").exists())
                    else:
                        exec(compile(script, "docs/Deploy.md:PY_TRANSFER_MODE", "exec"), {})
                        output = run / "transfer-mode.json"
                        record = json.loads(output.read_text())
                        self.assertEqual(record["transfer_mode"], mode)
                        self.assertEqual(record["kv_role"], "kv_both")
                        self.assertEqual(
                            record["source_sha256"], hashlib.sha256(original).hexdigest()
                        )
                        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                        retained = output.read_bytes()
                        with self.assertRaises(FileExistsError):
                            exec(compile(script, "docs/Deploy.md:PY_TRANSFER_MODE", "exec"), {})
                        self.assertEqual(output.read_bytes(), retained)
                        checked["plan_sha256"] = "0" * 64
                        (run / "checked.json").write_text(json.dumps(checked))
                        with self.assertRaisesRegex(SystemExit, "belonging to this launch plan"):
                            exec(compile(script, "docs/Deploy.md:PY_TRANSFER_MODE", "exec"), {})
                self.assertEqual(log.read_bytes(), original)

    def test_connector_protocol_capture_reads_installed_constant_and_source_hash(self):
        guide = (Path(__file__).resolve().parents[2] / "docs/Deploy.md").read_text()
        script = guide.split("<<'PY_NIXL_VERSION'\n", 1)[1].split("\nPY_NIXL_VERSION", 1)[0]
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
                        exec(compile(script, "docs/Deploy.md:PY_NIXL_VERSION", "exec"), {})
                    else:
                        with self.assertRaisesRegex(SystemExit, "positive integer"):
                            exec(compile(script, "docs/Deploy.md:PY_NIXL_VERSION", "exec"), {})
                load.assert_called_once_with(module.__name__)
                if version == 17:
                    record = json.loads(output.getvalue())
                    self.assertEqual(record["nixl_connector_version"], 17)
                    self.assertEqual(
                        record["module_sha256"], hashlib.sha256(source.read_bytes()).hexdigest()
                    )
                else:
                    self.assertEqual(output.getvalue(), "")

    def test_exact_api_version_controls_gate_while_distribution_keeps_build_suffix(self):
        guide = (Path(__file__).resolve().parents[2] / "docs/Deploy.md").read_text()
        script = guide.split("python3 - <<'PY_ENGINE'\n", 1)[1].split("\nPY_ENGINE", 1)[0]
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
                            "vllm_api_version": "0.29.0",
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
                        exec(compile(script, "docs/Deploy.md:PY_ENGINE", "exec"), {})
                        self.assertEqual(request.call_count, 5)
                        self.assertTrue((run / "completion.json").exists())
                    else:
                        with self.assertRaisesRegex(AssertionError, "checked image expects"):
                            exec(compile(script, "docs/Deploy.md:PY_ENGINE", "exec"), {})
                        self.assertEqual(request.call_count, 2)
                        self.assertFalse((run / "models.json").exists())
                self.assertEqual(
                    json.loads((run / "version.json").read_text())["version"], api_version
                )
