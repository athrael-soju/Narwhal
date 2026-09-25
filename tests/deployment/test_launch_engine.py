"""Exercise complete serving plans and launch guards with synthetic inputs and Docker mocks."""

import contextlib
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from tests.deployment.fixtures import launcher_inputs, runtime
from tools.deployment.launch_engine import (
    build,
    check,
    digest,
    handshake_policy,
    load,
    prepare,
    start,
    start_shared,
    validate_runtime,
    validate_shared_gpu,
    validate_shared_runs,
)

IMAGE_CHECK_OUTPUT = (
    'NARWHAL_TOKENIZER_READY=1\nNARWHAL_IMAGE_RUNTIME={"vllm_api_version": "0.29.0"}'
)


class EngineLauncherTests(unittest.TestCase):
    def test_native_backend_uses_existing_engine_plan_without_an_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
            env.pop("NARWHAL_ENGINE_IMAGE")
            env["NARWHAL_MODEL_REVISION"] = "a" * 40
            run = root / "launch"
            prepare(run, env, backend="native")
            plan = load(run)
            self.assertEqual(plan["backend"], "native")
            model_arg = plan["args"][plan["args"].index("--model") + 1]
            self.assertEqual(model_arg, env["NARWHAL_MODEL_DIR"])
            self.assertEqual(plan["connector"]["kv_connector"], "NixlConnector")
            self.assertFalse(plan["common"])
            output = (
                '{"vllm": "0.29.0", "nixl": "1.0.0"}\n'
                "NARWHAL_TOKENIZER_READY=1\n"
                'NARWHAL_IMAGE_RUNTIME={"vllm_api_version": "0.29.0"}\n'
            )
            with (
                patch("tools.deployment.launch_engine.docker") as docker,
                patch("tools.deployment.launch_engine.subprocess.run") as invoke,
            ):
                invoke.return_value = subprocess.CompletedProcess([], 0, output, "")
                check(run, plan)
                docker.assert_not_called()
                self.assertEqual(invoke.call_args.args[0][0], sys.executable)
            checked = json.loads((run / "checked.json").read_text())
            self.assertEqual(checked["backend"], "native")
            self.assertNotIn("image_id", checked)

    def test_native_check_rejects_model_file_changed_after_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
            model = root / "model" / "weights.gguf"
            model.write_bytes(b"first")
            env["NARWHAL_MODEL_PATH"] = str(model)
            record = json.loads(Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).read_text())
            record["runtime"]["expected_packages"]["vllm-gguf-plugin"] = "0.0.5"
            Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(record))
            env.pop("NARWHAL_ENGINE_IMAGE")
            env["NARWHAL_MODEL_REVISION"] = "a" * 40
            run = root / "launch"
            prepare(run, env, backend="native")
            model.write_bytes(b"changed")
            with patch("tools.deployment.launch_engine.subprocess.run") as invoke:
                with self.assertRaisesRegex(ValueError, "model file changed"):
                    check(run, load(run))
                invoke.assert_not_called()

    def test_native_gguf_keeps_snapshot_path_for_sidecar_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
            blobs = root / "blobs"
            blobs.mkdir()
            weights = blobs / "weights"
            weights.write_bytes(b"gguf")
            snapshot = root / "snapshot"
            snapshot.mkdir()
            model = snapshot / "weights.gguf"
            model.symlink_to(weights)
            (snapshot / "mmproj-F16.gguf").write_bytes(b"projector")
            env["NARWHAL_MODEL_PATH"] = str(model)
            env["NARWHAL_MODEL_REVISION"] = "a" * 40
            record = json.loads(Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).read_text())
            record["runtime"]["expected_packages"]["vllm-gguf-plugin"] = "0.0.5"
            Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(record))
            plan, _ = build(record, env, root / "launch", backend="native")
            self.assertEqual(plan["model_path"], str(model))
            self.assertEqual(plan["args"][plan["args"].index("--model") + 1], str(model))

    def test_qwen_linear_convolution_requires_ds_layout(self):
        from tools.deployment.launch_engine import requires_ds_conv_state_layout

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "text_config": {
                            "linear_conv_kernel_dim": 4,
                            "layer_types": ["linear_attention", "full_attention"],
                        }
                    }
                )
            )
            self.assertTrue(requires_ds_conv_state_layout(root))

    def test_plan_supplies_model_devices_ports_and_connector_without_access_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            record, env = launcher_inputs(Path(folder))
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

    def test_launch_ports_and_shared_budget_are_bound_to_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            record, env = launcher_inputs(Path(folder))
            for name in ("NARWHAL_ATTEST_PORT", "NARWHAL_NIXL_SIDE_CHANNEL_PORT"):
                changed = {**env, name: env["NARWHAL_ENGINE_PORT"]}
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "distinct"):
                    build(record, changed, Path(folder) / "launch")
            record["gpu_visibility_env"] = "CUDA_VISIBLE_DEVICES"
            record["gpu_ids"] = ["GPU-0"]
            record["tensor_parallel_size"] = 1
            record["environment"] = {"CUDA_VISIBLE_DEVICES": "GPU-0", "UCX_NET_DEVICES": "fabric0"}
            record["transfer"]["gpu_tls"] = "cuda"
            record["shared_device"] = {
                "group": "node-1:GPU-0",
                "gpu_uuid": "GPU-0",
                "device_allowance": 0.9,
                "gpu_memory_utilization": 0.4,
            }
            record["runtime"]["extra_args"].extend(["--gpu-memory-utilization", "0.4"])
            plan, _ = build(record, env, Path(folder) / "launch")
            self.assertEqual(plan["shared_device"], record["shared_device"])
            self.assertEqual(plan["ucx_tls"], "tcp,sm,self,cuda")
            self.assertEqual(plan["attestation_port"], 8010)
            self.assertEqual(plan["side_channel_port"], 5600)
            self.assertIn("0.4", plan["args"])
            record["runtime"]["extra_args"][-1] = "0.5"
            with self.assertRaisesRegex(ValueError, "differs from shared GPU budget"):
                build(record, env, Path(folder) / "launch")
            record["runtime"]["extra_args"][-1] = "0.4"
            record["transfer"]["gpu_tls"] = "cuda_copy"
            plan, values = build(record, env, Path(folder) / "launch")
            self.assertEqual(plan["ucx_tls"], "tcp,sm,self,cuda_copy")
            self.assertEqual(values["UCX_TLS"], plan["ucx_tls"])

    def test_image_check_rejects_missing_custom_code_flag_before_container_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
            (root / "model/tokenizer_config.json").write_text(
                json.dumps({"auto_map": {"AutoTokenizer": "tokenization_custom.CustomTokenizer"}})
            )
            run = root / "launch"
            prepare(run, env)
            with patch("tools.deployment.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "requires --trust-remote-code"):
                    check(run, load(run))
                mocked.assert_not_called()

    def test_image_check_rejects_convolutional_layout_before_model_load(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
            (root / "model/config.json").write_text(
                json.dumps(
                    {
                        "text_config": {
                            "linear_attn_config": {
                                "kda_layers": [1, 2],
                                "short_conv_kernel_size": 4,
                            }
                        }
                    }
                )
            )
            env["NARWHAL_MODEL_CONFIG_SHA256"] = digest(root / "model/config.json")
            run = root / "launch"
            prepare(run, env)
            with patch("tools.deployment.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "requires VLLM_SSM_CONV_STATE_LAYOUT=DS"):
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
                _, env = launcher_inputs(root)
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

                with patch("tools.deployment.launch_engine.docker", side_effect=execute) as docker:
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
            _, env = launcher_inputs(root)
            run = root / "launch"
            prepare(run, env)
            self.assertEqual((run / "container.env").stat().st_mode & 0o777, 0o600)
            plan = load(run)
            with patch("tools.deployment.launch_engine.docker") as mocked:
                with self.assertRaises(FileNotFoundError):
                    start(run, plan)
                mocked.assert_not_called()
            with patch(
                "tools.deployment.launch_engine.docker",
                side_effect=[json.dumps([{"Id": env["NARWHAL_ENGINE_IMAGE"]}]), IMAGE_CHECK_OUTPUT],
            ) as mocked:
                check(run, plan)
                self.assertIn("--rm", mocked.call_args_list[1].args[0])
            with patch(
                "tools.deployment.launch_engine.docker", side_effect=["c" * 64, "started"]
            ) as mocked:
                start(run, load(run))
                self.assertEqual(mocked.call_args_list[0].args[0][0], "create")
                self.assertEqual(mocked.call_args_list[1].args[0], ["start", "c" * 64])
            self.assertEqual((run / "container.id").read_text().strip(), "c" * 64)
            with patch("tools.deployment.launch_engine.docker") as mocked:
                with self.assertRaisesRegex(ValueError, "already has a container"):
                    start(run, load(run))
                mocked.assert_not_called()
            (run / "container.env").write_text("changed")
            with self.assertRaisesRegex(ValueError, "environment changed"):
                load(run)

    def test_wrong_image_and_changed_plan_stop_before_engine_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
            run = root / "launch"
            prepare(run, env)
            plan = load(run)
            with (
                patch(
                    "tools.deployment.launch_engine.docker",
                    return_value=json.dumps([{"Id": "sha256:" + "d" * 64}]),
                ),
                self.assertRaisesRegex(ValueError, "image identity"),
            ):
                check(run, plan)
            self.assertFalse((run / "checked.json").exists())
            with patch(
                "tools.deployment.launch_engine.docker",
                side_effect=[json.dumps([{"Id": env["NARWHAL_ENGINE_IMAGE"]}]), IMAGE_CHECK_OUTPUT],
            ):
                check(run, plan)
            plan["args"].append("--enforce-eager")
            (run / "launch.json").write_text(json.dumps(plan))
            with patch("tools.deployment.launch_engine.docker") as mocked:
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
        transformers_module = ModuleType("transformers")
        transformers_module.AutoTokenizer = Mock()
        resolver = factory_module.KVConnectorFactory.get_connector_class
        modules = {
            module_name: connector_module,
            config_module.__name__: config_module,
            factory_module.__name__: factory_module,
            version_module.__name__: version_module,
            transformers_module.__name__: transformers_module,
        }
        for missing_dependency in (False, True):
            with (
                self.subTest(missing_dependency=missing_dependency),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = Path(folder)
                _, env = launcher_inputs(root)
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

                with patch("tools.deployment.launch_engine.docker", side_effect=execute_check):
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
                if not missing_dependency:
                    transformers_module.AutoTokenizer.from_pretrained.assert_called_once_with(
                        "/model", trust_remote_code=False, local_files_only=True
                    )
                    transformers_module.AutoTokenizer.from_pretrained.reset_mock()

    def test_image_check_rejects_distribution_build_suffix_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, env = launcher_inputs(root)
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
                patch("tools.deployment.launch_engine.docker", side_effect=execute_check),
                self.assertRaisesRegex(AssertionError, "image package versions differ"),
            ):
                check(run, plan)
            self.assertFalse((run / "checked.json").exists())


class SharedEngineStartTests(unittest.TestCase):
    def fixture(self, root: Path, count: int = 2):
        selected = []
        for number in range(1, count + 1):
            run = root / f"engine-{number}"
            run.mkdir()
            (run / "launch.json").write_text("{}")
            (run / "checked.json").write_text(json.dumps({"image_id": "sha256:test"}))
            (run / "container.env").write_text("CUDA_VISIBLE_DEVICES=GPU-test\n")
            selected.append(
                (
                    run,
                    {
                        "role": f"engine-{number}",
                        "endpoint": f"http://127.0.0.1:{8000 + number}",
                        "attestation_port": 8100 + number,
                        "side_channel_port": 5600 + number,
                        "args": ["--gpu-memory-utilization", "0.2"],
                        "ucx_tls": "tcp,sm,self,cuda_copy",
                        "shared_device": {
                            "group": "kimchi:GPU-test",
                            "gpu_uuid": "GPU-test",
                            "device_allowance": 0.9,
                            "gpu_memory_utilization": 0.2,
                        },
                    },
                )
            )
        return selected

    def test_shared_start_rejects_visibility_on_another_gpu_for_both_backends(self):
        for backend in ("container", "native"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as folder:
                selected = self.fixture(Path(folder))
                for run, plan in selected:
                    if backend == "native":
                        plan.update(backend="native", python_executable=sys.executable)
                        (run / "engine.env").write_text("CUDA_VISIBLE_DEVICES=GPU-test\n")
                        (run / "checked.json").write_text(
                            json.dumps(
                                {
                                    "backend": "native",
                                    "python_executable": sys.executable,
                                    "plan_sha256": digest(run / "launch.json"),
                                }
                            )
                        )
                env_file = "engine.env" if backend == "native" else "container.env"
                (selected[1][0] / env_file).write_text("CUDA_VISIBLE_DEVICES=GPU-another\n")
                plans = dict(selected)
                with (
                    patch("tools.deployment.launch_engine.load", side_effect=plans.__getitem__),
                    patch("tools.deployment.launch_engine.require_checked"),
                    patch("tools.deployment.launch_engine.run_runtime_script") as inspect,
                    self.assertRaisesRegex(ValueError, "engine-2:.*differs from shared GPU"),
                ):
                    validate_shared_runs([run for run, _ in selected], backend=backend)
                inspect.assert_not_called()

    def test_native_cuda_ordinal_resolves_through_recorded_runtime_environment(self):
        expected = "GPU-11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "torch.py").write_text(
                "import os\n"
                "from types import SimpleNamespace\n"
                "assert os.environ['NARWHAL_CAPTURE_CACHE'] == '0'\n"
                "assert os.environ['CUDA_DEVICE_ORDER'] == 'FASTEST_FIRST'\n"
                "def properties(index):\n"
                "    assert index == 0\n"
                "    value = '11' if os.environ['CUDA_VISIBLE_DEVICES'] == '1' else '22'\n"
                "    uuid = SimpleNamespace(bytes=bytes.fromhex(value * 16))\n"
                "    return SimpleNamespace(uuid=uuid)\n"
                "cuda = SimpleNamespace(device_count=lambda: 1, get_device_properties=properties)\n"
            )
            selected = self.fixture(root)
            for run, plan in selected:
                plan.update(backend="native", python_executable=sys.executable)
                plan["shared_device"]["gpu_uuid"] = expected
                (run / "checked.json").write_text(
                    json.dumps(
                        {
                            "backend": "native",
                            "python_executable": sys.executable,
                            "plan_sha256": digest(run / "launch.json"),
                        }
                    )
                )
                (run / "engine.env").write_text(f"CUDA_VISIBLE_DEVICES=1\nPYTHONPATH={runtime}\n")
            plans = dict(selected)
            with (
                patch("tools.deployment.launch_engine.load", side_effect=plans.__getitem__),
                patch.dict(os.environ, {"CUDA_DEVICE_ORDER": "FASTEST_FIRST"}),
            ):
                self.assertEqual(
                    validate_shared_runs([run for run, _ in selected], backend="native"), selected
                )
                (selected[1][0] / "engine.env").write_text(
                    f"CUDA_VISIBLE_DEVICES=0\nPYTHONPATH={runtime}\n"
                )
                with self.assertRaisesRegex(ValueError, "engine-2:.*resolved to.*GPU-2222"):
                    validate_shared_runs([run for run, _ in selected], backend="native")

    def test_container_cuda_selection_queries_the_serving_image(self):
        expected = "GPU-11111111-1111-1111-1111-111111111111"
        for selection in ("1", "GPU-1111"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as folder:
                run, plan = self.fixture(Path(folder))[0]
                plan.update(common=["--env-file", str(run / "container.env")], image="sha256:test")
                plan["shared_device"]["gpu_uuid"] = expected
                (run / "container.env").write_text(f"CUDA_VISIBLE_DEVICES={selection}\n")
                cuda = Mock()
                cuda.device_count.return_value = 1
                cuda.get_device_properties.return_value.uuid.bytes = bytes.fromhex("11" * 16)
                torch = ModuleType("torch")
                torch.cuda = cuda

                def inspect(command, directory, log, run=run, torch=torch):
                    index = command.index("-c")
                    self.assertEqual(command[:2], ["run", "--rm"])
                    self.assertIn(str(run / "container.env"), command)
                    self.assertEqual(command[index - 1], "sha256:test")
                    with (
                        patch.dict(sys.modules, {"torch": torch}),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        exec(command[index + 1], {})
                    return output.getvalue()

                with patch("tools.deployment.launch_engine.docker", side_effect=inspect):
                    validate_shared_gpu(run, plan)
                    cuda.get_device_properties.assert_called_once_with(0)
                    cuda.device_count.return_value = 2
                    with self.assertRaisesRegex(ValueError, "exactly one visible CUDA device"):
                        validate_shared_gpu(run, plan)

    def test_preflight_checks_ports_and_total_budget_before_start(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = self.fixture(Path(folder), 4)
            plans = dict(selected)
            with (
                patch("tools.deployment.launch_engine.load", side_effect=plans.__getitem__),
                patch("tools.deployment.launch_engine.require_checked"),
            ):
                self.assertEqual(validate_shared_runs([run for run, _ in selected]), selected)
                selected[3][1]["side_channel_port"] = 8001
                with self.assertRaisesRegex(ValueError, "collides"):
                    validate_shared_runs([run for run, _ in selected])
                selected[3][1]["side_channel_port"] = 5604
                selected[3][1]["shared_device"]["gpu_memory_utilization"] = 0.4
                with self.assertRaisesRegex(ValueError, "above allowance"):
                    validate_shared_runs([run for run, _ in selected])

    def test_eight_shared_launches_fit_allowance(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = self.fixture(Path(folder), 8)
            for _, plan in selected:
                plan["shared_device"]["gpu_memory_utilization"] = 0.1
            plans = dict(selected)
            with (
                patch("tools.deployment.launch_engine.load", side_effect=plans.__getitem__),
                patch("tools.deployment.launch_engine.require_checked"),
            ):
                self.assertEqual(validate_shared_runs([run for run, _ in selected]), selected)

    def test_sequential_start_records_live_process_and_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = self.fixture(Path(folder))
            events = []
            readings = iter((1000, 6100, 6100, 11200))

            def memory(uuid):
                self.assertEqual(uuid, "GPU-test")
                return {"used_mib": next(readings), "total_mib": 30000}

            def start_container(run, plan):
                events.append(("start", plan["role"]))
                (run / "container.id").write_text("c" * 64)

            def ready(run, plan, cid, seconds):
                events.append(("ready", plan["role"]))
                self.assertEqual(cid, "c" * 64)

            def inspect(command, run, log):
                if command[2] == "{{json .State}}":
                    return json.dumps({"Running": True, "Pid": 1234})
                if command[2] == "{{json .Config.Cmd}}":
                    return json.dumps(dict(selected)[run]["args"])
                return "sha256:test"

            with (
                patch("tools.deployment.launch_engine.validate_shared_runs", return_value=selected),
                patch("tools.deployment.launch_engine.gpu_memory", side_effect=memory),
                patch("tools.deployment.launch_engine.start", side_effect=start_container),
                patch("tools.deployment.launch_engine.wait_ready", side_effect=ready),
                patch("tools.deployment.launch_engine.docker", side_effect=inspect),
            ):
                start_shared([run for run, _ in selected], 30)
            self.assertEqual(
                events,
                [
                    ("start", "engine-1"),
                    ("ready", "engine-1"),
                    ("start", "engine-2"),
                    ("ready", "engine-2"),
                ],
            )
            for run, plan in selected:
                record = json.loads((run / "shared-start.json").read_text())
                self.assertEqual(record["status"], "running")
                self.assertEqual(record["process_id"], 1234)
                self.assertEqual(record["image_id"], "sha256:test")
                self.assertEqual(record["vllm_args"], plan["args"])
                self.assertEqual(record["budget_mib"], 6000)

    def test_pressure_failure_preserves_original_cause_if_sensor_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            selected = self.fixture(Path(folder))
            readings = iter((1000, 6100, 29000))
            started = []

            def memory(uuid):
                try:
                    used = next(readings)
                except StopIteration as error:
                    raise ValueError("sensor offline") from error
                return {"used_mib": used, "total_mib": 30000}

            def start_container(run, plan):
                started.append(plan["role"])
                (run / "container.id").write_text("c" * 64)

            def inspect(command, run, log):
                if command[2] == "{{json .State}}":
                    return json.dumps({"Running": True, "Pid": 1234})
                if command[2] == "{{json .Config.Cmd}}":
                    return json.dumps(dict(selected)[run]["args"])
                return "sha256:test"

            with (
                patch("tools.deployment.launch_engine.validate_shared_runs", return_value=selected),
                patch("tools.deployment.launch_engine.gpu_memory", side_effect=memory),
                patch("tools.deployment.launch_engine.start", side_effect=start_container),
                patch("tools.deployment.launch_engine.wait_ready"),
                patch("tools.deployment.launch_engine.docker", side_effect=inspect),
                self.assertRaisesRegex(ValueError, "engine-2:.*29000/30000.*free GPU memory"),
            ):
                start_shared([run for run, _ in selected], 30)
            self.assertEqual(started, ["engine-1"])
            failure = json.loads((selected[1][0] / "shared-start.json").read_text())
            self.assertEqual(failure["status"], "failed")
            self.assertIn("free GPU memory", failure["error"])
            self.assertEqual(failure["gpu_before"]["used_mib"], 29000)
            self.assertEqual(failure["gpu_after_error"], "sensor offline")

    def test_docker_log_reader_keeps_stderr(self):
        from tools.deployment.launch_engine import docker

        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "tools.deployment.launch_engine.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "", "CUDA IPC failed\n"),
            ),
        ):
            self.assertEqual(
                docker(["logs", "container"], Path(folder), "launch.log", include_stderr=True),
                "CUDA IPC failed",
            )
