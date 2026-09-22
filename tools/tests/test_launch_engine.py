"""Exercise complete serving plans and launch guards with synthetic inputs and Docker mocks."""

import contextlib
import hashlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from tools.engine_launch import selected_launch
from tools.launch_engine import (
    build,
    check,
    digest,
    handshake_policy,
    load,
    prepare,
    start,
    validate_runtime,
)
from tools.tests.test_engine_launch import launch_document

IMAGE_CHECK_OUTPUT = 'NARWHAL_IMAGE_RUNTIME={"vllm_api_version": "0.29.0"}'


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
            self.assertIs(
                plan["connector"]["kv_connector_extra_config"]["enforce_handshake_compat"], True
            )
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

    def test_image_check_rejects_missing_custom_code_flag_before_container_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = self.inputs(root)
            (root / "model/tokenizer_config.json").write_text(
                json.dumps({"auto_map": {"AutoTokenizer": "tokenization_custom.CustomTokenizer"}})
            )
            run = root / "launch"
            prepare(run, env)
            with patch("tools.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "requires --trust-remote-code"):
                    check(run, load(run))
                mocked.assert_not_called()

    def test_handshake_policy_captures_installed_default_and_rejects_disabled_values(self):
        for explicit, default, passed in (
            (None, True, True),
            (True, True, True),
            (False, True, False),
            ("true", True, False),
            (None, False, False),
        ):
            with (
                self.subTest(explicit=explicit, default=default),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = Path(folder)
                _, env = self.inputs(root)
                run = root / "launch"
                prepare(run, env)
                plan = load(run)
                extra = plan["connector"]["kv_connector_extra_config"]
                if explicit is None:
                    extra.pop("enforce_handshake_compat")
                else:
                    extra["enforce_handshake_compat"] = explicit
                index = plan["args"].index("--kv-transfer-config") + 1
                plan["args"][index] = json.dumps(plan["connector"])
                (run / "launch.json").write_text(json.dumps(plan))
                (run / "checked.json").write_text(
                    json.dumps({"plan_sha256": digest(run / "launch.json")})
                )
                config_module = ModuleType("vllm.config")

                class Config:
                    def __init__(self, **values):
                        self.kv_connector_extra_config = values.get("kv_connector_extra_config", {})

                    def get_from_extra_config(self, key, default):
                        return self.kv_connector_extra_config.get(key, default)

                config_module.KVTransferConfig = Config
                worker_module = ModuleType(
                    "vllm.distributed.kv_transfer.kv_connector.v1.nixl.base_worker"
                )
                worker_module.NixlBaseConnectorWorker = type("Worker", (), {})
                source = (
                    "def __init__(self):\n    self.enforce_compat_hash = "
                    "self.kv_transfer_config.get_from_extra_config("
                    f"'enforce_handshake_compat', {default!r})\n"
                )
                modules = {
                    config_module.__name__: config_module,
                    worker_module.__name__: worker_module,
                }

                def execute(command, directory, log, modules=modules, source=source):
                    index = command.index("-c")
                    with (
                        patch.dict(sys.modules, modules),
                        patch.object(sys, "argv", ["-c", command[index + 2]]),
                        patch("inspect.getsource", return_value=source),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        exec(command[index + 1], {})
                    return output.getvalue()

                with patch("tools.launch_engine.docker", side_effect=execute) as docker:
                    if passed:
                        handshake_policy(run, plan)
                        capture = run / "handshake-policy.json"
                        record = json.loads(capture.read_text())
                        self.assertIs(record["enforce_handshake_compat"], True)
                        self.assertEqual(record["configured"], explicit is not None)
                        self.assertIs(record["installed_default"], default)
                        self.assertEqual(
                            record["source_sha256"], hashlib.sha256(source.encode()).hexdigest()
                        )
                        self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                        with self.assertRaisesRegex(ValueError, "capture exists"):
                            handshake_policy(run, plan)
                    else:
                        with self.assertRaisesRegex(ValueError, "boolean true"):
                            handshake_policy(run, plan)
                        self.assertFalse((run / "handshake-policy.json").exists())
                    self.assertEqual(docker.call_count, 1)

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
                side_effect=[json.dumps([{"Id": env["NARWHAL_ENGINE_IMAGE"]}]), IMAGE_CHECK_OUTPUT],
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
                side_effect=[json.dumps([{"Id": env["NARWHAL_ENGINE_IMAGE"]}]), IMAGE_CHECK_OUTPUT],
            ):
                check(run, plan)
            plan["args"].append("--enforce-eager")
            (run / "launch.json").write_text(json.dumps(plan))
            with patch("tools.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "plan changed"):
                    start(run, plan)
                mocked.assert_not_called()

    def test_image_check_executes_registered_connector_import_and_propagates_failure(self):
        module_name = "vllm.distributed.kv_transfer.kv_connector.v1.nixl"
        connector_module = ModuleType(module_name)
        connector_module.NixlConnector = type("NixlConnector", (), {"__module__": module_name})
        config_module = ModuleType("vllm.config")
        config_module.KVTransferConfig = Mock()
        factory_module = ModuleType("vllm.distributed.kv_transfer.kv_connector.factory")
        factory_module.KVConnectorFactory = Mock()
        version_module = ModuleType("vllm.version")
        version_module.__version__ = "0.29.0"
        resolver = factory_module.KVConnectorFactory.get_connector_class
        modules = {
            module_name: connector_module,
            config_module.__name__: config_module,
            factory_module.__name__: factory_module,
            version_module.__name__: version_module,
        }
        for missing_dependency in (False, True):
            with (
                self.subTest(missing_dependency=missing_dependency),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = Path(folder)
                _, env = self.inputs(root)
                run = root / "launch"
                prepare(run, env)
                plan = load(run)
                plan["expected_packages"]["vllm"] = "0.29.0+rocm100"
                (run / "launch.json").write_text(json.dumps(plan))
                resolver.reset_mock()
                resolver.side_effect = (
                    ModuleNotFoundError("connector dependency unavailable")
                    if missing_dependency
                    else lambda config: importlib.import_module(module_name).NixlConnector
                )

                def execute_check(command, directory, log, plan=plan):
                    if command[0] == "image":
                        return json.dumps([{"Id": plan["image"]}])
                    index = command.index("-c")
                    with (
                        patch.dict(sys.modules, modules),
                        patch.object(sys, "argv", ["-c", *command[index + 2 :]]),
                        patch("importlib.metadata.version", plan["expected_packages"].__getitem__),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        exec(command[index + 1], {})
                    return output.getvalue()

                with patch("tools.launch_engine.docker", side_effect=execute_check):
                    if missing_dependency:
                        with self.assertRaises(ModuleNotFoundError):
                            check(run, plan)
                    else:
                        check(run, plan)
                        marker = json.loads((run / "checked.json").read_text())
                        self.assertEqual(marker["vllm_api_version"], "0.29.0")
                        self.assertEqual(plan["expected_packages"]["vllm"], "0.29.0+rocm100")
                self.assertEqual((run / "checked.json").exists(), not missing_dependency)
                config_module.KVTransferConfig.assert_called_with(**plan["connector"])
                resolver.assert_called_once_with(config_module.KVTransferConfig.return_value)

    def test_image_check_rejects_distribution_build_suffix_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = self.inputs(root)
            run = root / "launch"
            prepare(run, env)
            plan = load(run)
            plan["expected_packages"]["vllm"] = "0.29.0+rocm100"

            def execute_check(command, directory, log):
                if command[0] == "image":
                    return json.dumps([{"Id": plan["image"]}])
                index = command.index("-c")
                with (
                    patch.object(sys, "argv", ["-c", *command[index + 2 :]]),
                    patch(
                        "importlib.metadata.version",
                        {"vllm": "0.29.0+other", "nixl": "1.0.0"}.__getitem__,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    exec(command[index + 1], {})

            with (
                patch("tools.launch_engine.docker", side_effect=execute_check),
                self.assertRaisesRegex(AssertionError, "image package versions differ"),
            ):
                check(run, plan)
            self.assertFalse((run / "checked.json").exists())
