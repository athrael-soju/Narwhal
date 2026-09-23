"""Bind cache block grouping to explicit layout evidence and the pinned layout API."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from tests.deployment.fixtures import launcher_inputs
from tools.deployment.launch_engine import digest, load, prepare, registration_layout


class CacheRegistrationTests(unittest.TestCase):
    def inputs(self, root):
        _, env = launcher_inputs(root)
        run = root / "plan"
        prepare(run, env)
        (run / "checked.json").write_text(json.dumps({"plan_sha256": digest(run / "launch.json")}))
        return run, load(run)

    def test_installed_layout_property_sets_boolean_and_preserves_source(self):
        for name, expected in (
            ("LBNHC", False),
            ("LBHNC", False),
            ("LHBNC", False),
            ("BLHNC", True),
            ("BLNHC", True),
            ("BHLNC", True),
        ):
            with self.subTest(layout=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run, plan = self.inputs(root)
                log = root / "startup.log"
                log.write_text(
                    f"runtime init\nUsing {name} KV cache layout.\nUsing {name} KV cache layout.\n"
                )
                before = log.read_bytes()
                source = root / "layout.py"
                source.write_text("# Synthetic installed layout descriptor\n")
                module = ModuleType("vllm.v1.kv_cache_layout")
                module.KVCacheLayout = Enum("KVCacheLayout", {name: expected})
                module.KVCacheLayout.is_block_outermost = property(lambda item: item.value)

                def docker(command, directory, filename, module=module, source=source):
                    index = command.index("-c")
                    with (
                        patch.dict(sys.modules, {module.__name__: module}),
                        patch.object(sys, "argv", ["-c", command[index + 2]]),
                        patch("inspect.getfile", return_value=str(source)),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        exec(command[index + 1], {})
                    return output.getvalue()

                with patch("tools.deployment.launch_engine.docker", side_effect=docker) as called:
                    registration_layout(run, plan, log, False)
                    with self.assertRaisesRegex(ValueError, "capture exists"):
                        registration_layout(run, plan, log, False)
                self.assertEqual(called.call_count, 1)
                capture = run / "cache-registration.json"
                value = json.loads(capture.read_text())
                self.assertIs(value["cross_layers_blocks"], expected)
                self.assertEqual(value["input_sha256"], digest(log))
                self.assertEqual(value["module_sha256"], digest(source))
                self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                self.assertEqual(log.read_bytes(), before)

    def test_silent_or_conflicting_logs_require_explicit_layout_evidence(self):
        for content in (
            "use_mla: true\n",
            "Using LBNHC KV cache layout.\nUsing BLHNC KV cache layout.",
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run, plan = self.inputs(root)
                log = root / "startup.log"
                log.write_text(content)
                with patch("tools.deployment.launch_engine.docker") as docker:
                    with self.assertRaisesRegex(ValueError, "one resolved"):
                        registration_layout(run, plan, log, False)
                    docker.assert_not_called()
                self.assertFalse((run / "cache-registration.json").exists())

    def test_runtime_layout_requires_matching_inputs_and_every_rank(self):
        for failure in (None, "image", "rank", "layout"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run, plan = self.inputs(root)
                record = {
                    "image": plan["image"],
                    "model_config_sha256": plan["model_config_sha256"],
                    "launch_config_sha256": plan["launch_sha256"],
                    "ranks": [{"rank": rank, "kv_cache_layout": "LBNHC"} for rank in range(2)],
                }
                if failure == "image":
                    record["image"] = "sha256:" + "f" * 64
                elif failure == "rank":
                    record["ranks"].pop()
                elif failure == "layout":
                    record["ranks"][1].pop("kv_cache_layout")
                source = root / "cache-layout.json"
                source.write_text(json.dumps(record))
                with patch(
                    "tools.deployment.launch_engine.docker",
                    return_value='NARWHAL_CACHE_REGISTRATION={"cross_layers_blocks":false,"kv_cache_layout":"LBNHC"}',
                ) as docker:
                    if failure:
                        with self.assertRaises(ValueError):
                            registration_layout(run, plan, source, True)
                        docker.assert_not_called()
                    else:
                        registration_layout(run, plan, source, True)
                        self.assertEqual(docker.call_count, 1)
