"""Exercise complete serving plans and launch guards with synthetic inputs and Docker mocks."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.engine_launch import selected_launch
from tools.launch_engine import build, check, load, prepare, start, validate_runtime
from tools.tests.test_engine_launch import launch_document


def runtime():
    return {
        "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
        "model_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "block_size": 128,
        "environment": {"VLLM_ROCM_USE_AITER": "1"},
        "extra_args": ["--max-model-len", "4096", "--enforce-eager"],
    }


class EngineLauncherTests(unittest.TestCase):
    def inputs(self, root):
        record = selected_launch(
            launch_document(), "engine-1", {"NARWHAL_FABRIC_INTERFACE": "fabric0"}
        )
        record["runtime"] = runtime()
        model = root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        source = root / "engine.json"
        source.write_text(json.dumps(record))
        env = {
            "NARWHAL_ENGINE_LAUNCH_CONFIG": str(source),
            "NARWHAL_MODEL_DIR": str(model),
            "NARWHAL_MODEL_CONFIG_SHA256": hashlib.sha256(b"{}").hexdigest(),
            "NARWHAL_ENGINE_IMAGE": "sha256:" + "a" * 64,
            "NARWHAL_ENGINE_PORT": "8000",
            "NARWHAL_NIXL_SIDE_CHANNEL_PORT": "5600",
            "NARWHAL_UCX_TCP_PORT_RANGE": "39000-39999",
            "NARWHAL_NODE_1_IP": "192.0.2.11",
            "NARWHAL_NODE_1_URL": "http://192.0.2.11:8000",
            "NARWHAL_ENGINE_MODEL_NAME": "synthetic",
            "NARWHAL_DEPLOYMENT_REVISION": "b" * 40,
            "NARWHAL_ENGINE_API_KEY": "engine-only-secret",
            "NARWHAL_NODE_1_SSH_PASSWORD": "management-only-secret",
        }
        return record, env

    def test_plan_supplies_model_devices_ports_and_connector_without_access_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            record, env = self.inputs(Path(folder))
            plan, values = build(record, env, Path(folder) / "launch")
            self.assertIn("vllm.entrypoints.openai.api_server", plan["args"])
            self.assertEqual(plan["connector"]["kv_role"], "kv_both")
            self.assertEqual(plan["connector"]["kv_connector_extra_config"]["backends"], ["UCX"])
            self.assertEqual(values["VLLM_NIXL_SIDE_CHANNEL_HOST"], env["NARWHAL_NODE_1_IP"])
            self.assertEqual(values["UCX_TLS"], "tcp,sm,self,rocm")
            self.assertEqual(values["ROCR_VISIBLE_DEVICES"], "0,1")
            self.assertIn("--no-enable-prefix-caching", plan["args"])
            self.assertEqual(values["VLLM_API_KEY"], "engine-only-secret")
            self.assertNotIn("management-only-secret", json.dumps([plan, values]))
            self.assertNotIn("engine-only-secret", json.dumps(plan))

    def test_extra_options_cannot_override_ports_credentials_or_connector(self):
        for options in (["--port", "99"], ["--api-key", "secret"], ["--kv-transfer-config", "{}"]):
            spec = runtime()
            spec["extra_args"] = options
            with self.assertRaises(ValueError):
                validate_runtime(spec)
        spec = runtime()
        spec["environment"]["VLLM_NIXL_SKIP_COMPATIBILITY_CHECK"] = "1"
        with self.assertRaises(ValueError):
            validate_runtime(spec)

    def test_start_requires_matching_image_check_and_retains_container_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = self.inputs(root)
            run = root / "launch"
            prepare(run, env)
            self.assertEqual((run / "container.env").stat().st_mode & 0o777, 0o600)
            plan = load(run)
            with patch("tools.launch_engine.docker") as mocked:
                with self.assertRaises(FileNotFoundError):
                    start(run, plan)
                mocked.assert_not_called()
            with patch(
                "tools.launch_engine.docker",
                side_effect=[json.dumps([{"Id": env["NARWHAL_ENGINE_IMAGE"]}]), "packages matched"],
            ) as mocked:
                check(run, plan)
                self.assertIn("--rm", mocked.call_args_list[1].args[0])
            with patch("tools.launch_engine.docker", side_effect=["c" * 64, "started"]) as mocked:
                start(run, load(run))
                self.assertEqual(mocked.call_args_list[0].args[0][0], "create")
                self.assertEqual(mocked.call_args_list[1].args[0], ["start", "c" * 64])
            self.assertEqual((run / "container.id").read_text().strip(), "c" * 64)
            with patch("tools.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "already has a container"):
                    start(run, load(run))
                mocked.assert_not_called()
            (run / "container.env").write_text("changed")
            with self.assertRaisesRegex(ValueError, "environment changed"):
                load(run)

    def test_wrong_image_and_changed_plan_stop_before_engine_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = self.inputs(root)
            run = root / "launch"
            prepare(run, env)
            plan = load(run)
            with (
                patch(
                    "tools.launch_engine.docker",
                    return_value=json.dumps([{"Id": "sha256:" + "d" * 64}]),
                ),
                self.assertRaisesRegex(ValueError, "image identity"),
            ):
                check(run, plan)
            self.assertFalse((run / "checked.json").exists())
            with patch(
                "tools.launch_engine.docker",
                side_effect=[json.dumps([{"Id": env["NARWHAL_ENGINE_IMAGE"]}]), "ok"],
            ):
                check(run, plan)
            plan["args"].append("--enforce-eager")
            (run / "launch.json").write_text(json.dumps(plan))
            with patch("tools.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "plan changed"):
                    start(run, plan)
                mocked.assert_not_called()
