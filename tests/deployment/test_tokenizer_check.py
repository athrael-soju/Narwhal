"""Execute generated runtime checks against controlled tokenizer files and package APIs."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from tests.deployment.fixtures import launcher_inputs
from tools.deployment.launch_engine import check, load, prepare


class TokenizerCheckTests(unittest.TestCase):
    def test_runtime_check_loads_the_effective_tokenizer_in_each_backend(self):
        for backend in ("native", "container"):
            for case in (
                "default",
                "default_missing",
                "explicit",
                "explicit_missing",
                "last_explicit",
                "custom_code",
                "trusted_custom_code",
                "unused_custom_tokenizer",
                "image_local",
            ):
                if case == "image_local" and backend == "native":
                    continue
                with (
                    self.subTest(backend=backend, case=case),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    self.check_selection(Path(folder), backend, case)

    def check_selection(self, root, backend, case):
        record, env = launcher_inputs(root)
        env["NARWHAL_MODEL_REVISION"] = "a" * 40
        model = root / "model"
        tokenizer = (root if backend == "native" else model) / "selected-tokenizer"
        tokenizer.mkdir()
        (tokenizer / "tokenizer.json").write_text("{}")
        selected = str(model) if backend == "native" else "/model"
        args = record["runtime"]["extra_args"]
        if case in {"default", "explicit_missing"}:
            (model / "tokenizer.json").write_text("{}")
        if case not in {"default", "default_missing"}:
            if case == "explicit_missing":
                tokenizer = model / "missing-tokenizer"
            elif case == "image_local":
                tokenizer = root / "image-tokenizer"
                tokenizer.mkdir()
                (tokenizer / "tokenizer.json").write_text("{}")
            selected = (
                str(tokenizer)
                if backend == "native"
                else "/opt/image-tokenizer"
                if case == "image_local"
                else "/model/" + tokenizer.name
            )
            if case == "last_explicit":
                args.extend(["--tokenizer", "/missing-first-tokenizer"])
            args.extend(["--tokenizer", selected])
        custom_metadata = json.dumps({"auto_map": {"AutoTokenizer": "custom.Tokenizer"}})
        if case in {"custom_code", "trusted_custom_code"}:
            (tokenizer / "tokenizer_config.json").write_text(custom_metadata)
        if case == "unused_custom_tokenizer":
            (model / "tokenizer_config.json").write_text(custom_metadata)
        if case == "trusted_custom_code":
            args.append("--trust-remote-code")
        Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(record))
        run = root / "launch"
        prepare(run, env, backend=backend)
        plan = load(run)
        modules = {}
        for name in (
            "vllm.config",
            "vllm.distributed.kv_transfer.kv_connector.factory",
            "vllm.version",
            "transformers",
        ):
            modules[name] = ModuleType(name)
        modules["vllm.config"].KVTransferConfig = Mock()
        modules["vllm.distributed.kv_transfer.kv_connector.factory"].KVConnectorFactory = Mock()
        modules[
            "vllm.distributed.kv_transfer.kv_connector.factory"
        ].KVConnectorFactory.get_connector_class.return_value = type("NixlConnector", (), {})
        modules["vllm.version"].__version__ = "0.29.0"
        modules["transformers"].AutoTokenizer = Mock()
        # Python 3.11's Path factory consults pathlib.Path while that name is patched.
        path_type = type(root)

        def runtime_path(value):
            path = path_type(value)
            if backend == "container":
                if value == "/opt/image-tokenizer":
                    return tokenizer
                for index, option in enumerate(plan["common"]):
                    if option == "--mount":
                        fields = dict(
                            item.split("=", 1)
                            for item in plan["common"][index + 1].split(",")
                            if "=" in item
                        )
                        if path.is_relative_to(fields["dst"]):
                            return path_type(fields["src"]) / path.relative_to(fields["dst"])
            return path

        def load_tokenizer(path, *, trust_remote_code, local_files_only):
            self.assertTrue(local_files_only)
            if not (runtime_path(path) / "tokenizer.json").is_file():
                raise OSError(f"selected tokenizer is unavailable: {path}")
            return object()

        tokenizer_api = modules["transformers"].AutoTokenizer.from_pretrained
        tokenizer_api.side_effect = load_tokenizer

        def execute(command):
            index = command.index("-c")
            with (
                patch.dict(sys.modules, modules),
                patch.object(sys, "argv", ["-c", *command[index + 2 :]]),
                patch("importlib.metadata.version", plan["expected_packages"].__getitem__),
                patch("pathlib.Path", side_effect=runtime_path),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                try:
                    exec(command[index + 1], {})
                except (ValueError, OSError) as error:
                    return subprocess.CompletedProcess(command, 1, output.getvalue(), str(error))
            return subprocess.CompletedProcess(command, 0, output.getvalue(), "")

        def native_execute(command, **kwargs):
            return execute(command)

        def container_execute(command, directory, log):
            if command[0] == "image":
                return json.dumps([{"Id": plan["image"]}])
            result = execute(command)
            if result.returncode:
                raise ValueError(result.stderr)
            return result.stdout

        failure = case in {"default_missing", "explicit_missing", "custom_code"}
        with (
            patch("tools.deployment.launch_engine.subprocess.run", side_effect=native_execute),
            patch("tools.deployment.launch_engine.docker", side_effect=container_execute),
        ):
            if failure:
                message = (
                    "Tokenizer metadata requires --trust-remote-code"
                    if case == "custom_code"
                    else "selected tokenizer is unavailable"
                )
                with self.assertRaisesRegex(ValueError, message):
                    check(run, plan)
            else:
                check(run, plan)
        self.assertEqual((run / "checked.json").exists(), not failure)
        if case == "custom_code":
            tokenizer_api.assert_not_called()
        else:
            tokenizer_api.assert_called_once_with(
                selected,
                trust_remote_code=case == "trusted_custom_code",
                local_files_only=True,
            )
        if "--tokenizer" in plan["args"]:
            last_index = len(plan["args"]) - 1 - plan["args"][::-1].index("--tokenizer")
            self.assertEqual(plan["args"][last_index + 1], selected)


if __name__ == "__main__":
    unittest.main()
