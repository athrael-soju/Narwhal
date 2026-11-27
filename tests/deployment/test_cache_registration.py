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

from narwhal.backends.vllm.captures import registration_layout
from narwhal.deployment.launch_engine.plan import load, prepare
from narwhal.deployment.launch_engine.runtime import digest
from tests.deployment.fixtures import launcher_inputs, patched_docker


class CacheRegistrationTests(unittest.TestCase):
    def inputs(self, root):
        _, env = launcher_inputs(root)
        run = root / "plan"
        prepare(run, env)
        (run / "checked.json").write_text(json.dumps({"plan_sha256": digest(run / "launch.json")}))
        return run, load(run)

    def layout(self, run: Path, plan: dict, *names: str) -> Path:
        record = {
            "image": plan["image"],
            "model_config_sha256": plan["model_config_sha256"],
            "launch_config_sha256": plan["launch_sha256"],
            "ranks": [{"rank": rank, "kv_cache_layout": name} for rank, name in enumerate(names)],
        }
        source = run / "cache-layout.json"
        source.write_text(json.dumps(record))
        return source

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
                layout = self.layout(run, plan, name, name)
                before = layout.read_bytes()
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

                with patched_docker(side_effect=docker) as called:
                    registration_layout(run, plan, run)
                    with self.assertRaisesRegex(ValueError, "capture exists"):
                        registration_layout(run, plan, run)
                self.assertEqual(called.call_count, 1)
                capture = run / "cache-registration.json"
                value = json.loads(capture.read_text())
                self.assertIs(value["cross_layers_blocks"], expected)
                self.assertEqual(value["kv_cache_layout"], name)
                self.assertEqual(value["input_kind"], "runtime_cache_layout")
                self.assertEqual(value["input_sha256"], digest(layout))
                self.assertEqual(value["module_sha256"], digest(source))
                self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                self.assertEqual(layout.read_bytes(), before)

    def test_runtime_layout_requires_matching_inputs_and_every_rank(self):
        for failure in (None, "missing", "image", "rank", "layout", "differing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run, plan = self.inputs(root)
                source = self.layout(run, plan, "LBNHC", "LBNHC")
                record = json.loads(source.read_text())
                if failure == "missing":
                    source.unlink()
                elif failure == "image":
                    record["image"] = "sha256:" + "f" * 64
                elif failure == "rank":
                    record["ranks"].pop()
                elif failure == "layout":
                    record["ranks"][1].pop("kv_cache_layout")
                elif failure == "differing":
                    record["ranks"][1]["kv_cache_layout"] = "BLHNC"
                if failure != "missing":
                    source.write_text(json.dumps(record))
                with patched_docker(
                    return_value='NARWHAL_CACHE_REGISTRATION={"cross_layers_blocks":false,"kv_cache_layout":"LBNHC"}',
                ) as docker:
                    if failure:
                        with self.assertRaises(ValueError):
                            registration_layout(run, plan, run)
                        docker.assert_not_called()
                        self.assertFalse((run / "cache-registration.json").exists())
                    else:
                        registration_layout(run, plan, run)
                        self.assertEqual(docker.call_count, 1)


if __name__ == "__main__":
    unittest.main()
