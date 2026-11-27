"""The TensorRT-LLM cache capture: the in-engine hook and narwhal-engine capture-cache."""

import hashlib
import json
import os
import runpy
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import load
from narwhal.backends.trtllm import captures
from narwhal.deployment.fabric_budget import runtime_payload
from narwhal.deployment.launch_engine.runtime import digest
from tests.backends.contract import fixtures, prepared

HOOK = load("trtllm").launcher().cache_hook
# The engine's attention bytes per token and recurrent state per sequence, as measured on a
# hybrid model whose handoffs carried both.
BYTES_PER_TOKEN = 65536
STATE_BYTES = 78446592
TOKENS_PER_BLOCK = 32
MAX_SEQ_LEN = 32768

# A stand-in for the engine package: the creator module the hook patches, and the
# resource manager names it reads.
ENGINE_PACKAGE = {
    "tensorrt_llm/__init__.py": "",
    "tensorrt_llm/_torch/__init__.py": "",
    "tensorrt_llm/_torch/pyexecutor/__init__.py": "",
    "tensorrt_llm/_torch/pyexecutor/resource_manager.py": """
        import enum

        class ResourceManagerType(enum.Enum):
            KV_CACHE_MANAGER = "KV_CACHE_MANAGER"
            DRAFT_KV_CACHE_MANAGER = "DRAFT_KV_CACHE_MANAGER"

        class LinearCacheType(enum.Enum):
            RECURRENT_STATES = -2147483647
    """,
    "tensorrt_llm/_torch/pyexecutor/_util.py": """
        from .resource_manager import ResourceManagerType

        class KvCacheCreator:
            def __init__(self, manager, tp_rank=0, pp_size=1, draft=None):
                self.manager = manager
                self.draft = draft
                self._mapping = type("Mapping", (), {"tp_rank": tp_rank, "pp_size": pp_size})()
                self._max_seq_len = 32768
                self.builds = 0

            def build_managers(self, resources, estimating_kv_cache=False):
                self.builds += 1
                resources[ResourceManagerType.KV_CACHE_MANAGER] = self.manager
                resources[ResourceManagerType.DRAFT_KV_CACHE_MANAGER] = self.draft
    """,
}


class Manager:
    """A hybrid cache manager: attention pages and one recurrent state per layer."""

    def __init__(self, layers=48, per_layer=STATE_BYTES // 48, linear=True):
        self.tokens_per_block = TOKENS_PER_BLOCK
        self.max_attention_window_vec = [MAX_SEQ_LEN] * 16 + [-2147483647] * layers
        if linear:
            self.linear_attention_metadata = type(
                "Metadata", (), {"all_recurrent_states_bytes": per_layer}
            )()

    def get_cache_bytes_per_token(self):
        return BYTES_PER_TOKEN


class MambaCacheManager(Manager):
    def __init__(self):
        super().__init__(layers=0, linear=False)


class HybridCacheManager(Manager):
    """Recurrent layers in the window vector, but no state size to read."""

    def __init__(self):
        super().__init__(linear=False)


class HookTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name, text in ENGINE_PACKAGE.items():
            path = self.root / "engine" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text))
        hooks = self.root / "hook"
        hooks.mkdir()
        shutil.copy(HOOK, hooks / "sitecustomize.py")
        (hooks / "launch_engine.py").write_text("# launcher\n")
        model = self.root / "config.json"
        model.write_text("{}")
        self.plan = {
            "image": "sha256:" + "a" * 64,
            "expected_packages": {"tensorrt_llm": "1.4.0rc3"},
            "revision": "b" * 40,
            "model_config_path": str(model),
            "model_config_sha256": digest(model),
            "launch_sha256": "d" * 64,
            "launcher_sha256": digest(hooks / "launch_engine.py"),
            "cache_capture_sha256": digest(HOOK),
        }
        self.plan_path = hooks / "launch.json"
        self.plan_path.write_text(json.dumps(self.plan))
        self.output = self.root / "narwhal-cache-layout.json"
        self.hook = hooks / "sitecustomize.py"
        saved_path, saved_finders = list(sys.path), list(sys.meta_path)

        def restore():
            sys.path[:] = saved_path
            sys.meta_path[:] = saved_finders
            for name in [name for name in sys.modules if name.startswith("tensorrt_llm")]:
                del sys.modules[name]

        self.addCleanup(restore)
        sys.path.insert(0, str(self.root / "engine"))

    def run_hook(self, enabled="1"):
        environment = {
            "NARWHAL_CAPTURE_CACHE": enabled,
            "NARWHAL_CACHE_PLAN": str(self.plan_path),
            "NARWHAL_CACHE_OUTPUT": str(self.output),
        }
        with patch.dict(os.environ, environment):
            runpy.run_path(str(self.hook), run_name="sitecustomize")
        import tensorrt_llm._torch.pyexecutor._util as util  # type: ignore[import-not-found]

        return util

    def environment(self):
        return patch.dict(
            os.environ,
            {"NARWHAL_CACHE_PLAN": str(self.plan_path), "NARWHAL_CACHE_OUTPUT": str(self.output)},
        )

    def rank_record(self, rank=0):
        return self.root / f"narwhal-cache-layout.rank{rank}.json"

    def test_the_serving_pass_records_attention_pages_and_recurrent_state(self):
        util = self.run_hook()
        creator = util.KvCacheCreator(Manager())
        with self.environment():
            creator.build_managers({}, True)
            self.assertFalse(self.rank_record().exists())
            creator.build_managers({}, False)
        record = json.loads(self.rank_record().read_text())
        self.assertEqual(creator.builds, 2)
        plan_sha256 = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
        self.assertEqual(record["plan_sha256"], plan_sha256)
        self.assertEqual(
            record["ranks"],
            [
                {
                    "rank": 0,
                    "layers": [
                        {
                            "layer": "attention",
                            "page_bytes": BYTES_PER_TOKEN * TOKENS_PER_BLOCK,
                            "block_tokens": TOKENS_PER_BLOCK,
                            "extra_blocks": 0,
                        },
                        {
                            "layer": "recurrent_state",
                            "page_bytes": STATE_BYTES,
                            "block_tokens": MAX_SEQ_LEN,
                            "extra_blocks": 0,
                        },
                    ],
                }
            ],
        )
        # A 1,312-token handoff: 41 pages of attention KV and one sequence's state.
        self.assertEqual(runtime_payload(record, 1, 1312), 41 * 2 * 1024**2 + STATE_BYTES)

    def test_a_restart_keeps_the_first_record_and_a_changed_layout_fails(self):
        util = self.run_hook()
        with self.environment():
            util.KvCacheCreator(Manager()).build_managers({})
            first = self.rank_record().read_text()
            util.KvCacheCreator(Manager()).build_managers({})
            self.assertEqual(self.rank_record().read_text(), first)
            with self.assertRaisesRegex(ValueError, "differs from this launch's earlier start"):
                util.KvCacheCreator(Manager(layers=24)).build_managers({})
        self.assertEqual(self.rank_record().read_text(), first)

    def test_each_rank_writes_its_own_record(self):
        util = self.run_hook()
        with self.environment():
            for rank in (0, 1):
                util.KvCacheCreator(Manager(), tp_rank=rank).build_managers({})
        for rank in (0, 1):
            record = json.loads(self.rank_record(rank).read_text())
            self.assertEqual(record["ranks"][0]["rank"], rank)

    def test_an_unsizable_cache_records_why_and_the_engine_keeps_starting(self):
        cases = (
            ({"manager": MambaCacheManager()}, "MambaCacheManager exposes no recurrent state"),
            ({"manager": HybridCacheManager()}, "HybridCacheManager exposes no recurrent state"),
            ({"manager": Manager(), "pp_size": 2}, "pipeline parallel size 1"),
            ({"manager": Manager(), "draft": Manager()}, "separate draft cache"),
        )
        util = self.run_hook()
        for rank, (arguments, reason) in enumerate(cases):
            with self.subTest(reason=reason), self.environment():
                creator = util.KvCacheCreator(tp_rank=rank, **arguments)
                creator.build_managers({})
                self.assertEqual(creator.builds, 1)
                record = json.loads(self.rank_record(rank).read_text())
                self.assertIn(reason, record["unsupported"])
                self.assertEqual(record["ranks"], [{"rank": rank, "layers": []}])

    def test_a_launch_that_differs_from_its_plan_stops_the_engine(self):
        util = self.run_hook()
        self.plan_path.write_text(json.dumps({**self.plan, "model_config_sha256": "0" * 64}))
        with self.environment(), self.assertRaisesRegex(ValueError, "model config differs"):
            util.KvCacheCreator(Manager()).build_managers({})
        self.assertFalse(self.rank_record().exists())

    def test_a_lookup_before_the_import_leaves_the_patch_in_place(self):
        import importlib.util

        environment = {
            "NARWHAL_CAPTURE_CACHE": "1",
            "NARWHAL_CACHE_PLAN": str(self.plan_path),
            "NARWHAL_CACHE_OUTPUT": str(self.output),
        }
        with patch.dict(os.environ, environment):
            runpy.run_path(str(self.hook), run_name="sitecustomize")
            import tensorrt_llm._torch.pyexecutor  # noqa: F401  # type: ignore[import-not-found]

            self.assertIsNotNone(importlib.util.find_spec("tensorrt_llm._torch.pyexecutor._util"))
            import tensorrt_llm._torch.pyexecutor._util as util  # type: ignore[import-not-found]

            util.KvCacheCreator(Manager()).build_managers({})
        self.assertTrue(self.rank_record().exists())

    def test_attention_only_caches_record_no_state(self):
        util = self.run_hook()
        manager = Manager(layers=0)
        del manager.linear_attention_metadata
        with self.environment():
            util.KvCacheCreator(manager).build_managers({})
        record = json.loads(self.rank_record().read_text())
        self.assertEqual([layer["layer"] for layer in record["ranks"][0]["layers"]], ["attention"])

    def test_the_hook_patches_nothing_unless_enabled(self):
        util = self.run_hook(enabled="0")
        util.KvCacheCreator(Manager()).build_managers({})
        self.assertFalse(self.rank_record().exists())


class CaptureTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run, _, _ = prepared(Path(folder.name), load("trtllm").launcher(), fixtures("trtllm"))
        self.plan = json.loads((self.run / "launch.json").read_text())
        (self.run / "checked.json").write_text(
            json.dumps({"plan_sha256": digest(self.run / "launch.json")})
        )
        (self.run / "container.id").write_text("c" * 64 + "\n")

    def record(self, rank, **changes):
        value = {
            "schema_version": 1,
            "sizing": "runtime_padded_page_upper_bound",
            "image": self.plan["image"],
            "expected_packages": self.plan["expected_packages"],
            "revision": self.plan["revision"],
            "model_config_sha256": self.plan["model_config_sha256"],
            "launch_config_sha256": self.plan["launch_sha256"],
            "plan_sha256": digest(self.run / "launch.json"),
            "launcher_sha256": self.plan["launcher_sha256"],
            "cache_capture_sha256": self.plan["cache_capture_sha256"],
            "ranks": [
                {
                    "rank": rank,
                    "layers": [
                        {
                            "layer": "attention",
                            "page_bytes": 2 * 1024**2,
                            "block_tokens": 32,
                            "extra_blocks": 0,
                        }
                    ],
                }
            ],
        }
        value.update(changes)
        return value

    def capture(self, tp, records):
        copied = []

        def fake_docker(command, directory, log):
            if command[0] == "inspect":
                return json.dumps({"Running": True})
            self.assertEqual(command[0], "cp")
            rank = int(command[1].rsplit(".rank", 1)[1].split(".")[0])
            copied.append(rank)
            Path(command[-1]).write_text(json.dumps(records[rank]))
            return ""

        with (
            patch.object(captures, "tensor_parallel_size", return_value=tp),
            patch.object(captures, "docker", side_effect=fake_docker),
        ):
            captures.capture_cache(self.run, self.plan)
        return copied

    def test_capture_merges_every_rank_of_the_running_container(self):
        self.assertEqual(self.capture(2, [self.record(0), self.record(1)]), [0, 1])
        layout = json.loads((self.run / "cache-layout.json").read_text())
        self.assertEqual((self.run / "cache-layout.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual([rank["rank"] for rank in layout["ranks"]], [0, 1])
        self.assertEqual(runtime_payload(layout, 2, 1024), 2 * 32 * 2 * 1024**2)
        self.assertEqual(list(self.run.glob("cache-layout.rank*")), [])

    def test_capture_refuses_a_record_from_another_plan_or_rank(self):
        for records, message in (
            ([self.record(0, image="sha256:" + "0" * 64)], "differs from the checked serving plan"),
            ([self.record(1)], "rank 0 cache record must describe that rank alone"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.capture(1, records)
                self.assertFalse((self.run / "cache-layout.json").exists())

    def test_capture_reports_a_rank_that_could_not_size_its_cache(self):
        record = self.record(0, unsupported="cache capture does not size a separate draft cache")
        with self.assertRaisesRegex(ValueError, "rank 0 could not size its cache: .*draft"):
            self.capture(1, [record])
        self.assertFalse((self.run / "cache-layout.json").exists())

    def test_a_native_launch_merges_the_records_in_its_launch_directory(self):
        self.plan.update(backend="native", python_executable="/engine/bin/python3")
        (self.run / "checked.json").write_text(
            json.dumps(
                {
                    "plan_sha256": digest(self.run / "launch.json"),
                    "backend": "native",
                    "python_executable": self.plan["python_executable"],
                    "expected_packages": self.plan["expected_packages"],
                }
            )
        )
        for rank in (0, 1):
            (self.run / f"cache-layout.rank{rank}.json").write_text(json.dumps(self.record(rank)))
        with (
            patch.object(captures, "tensor_parallel_size", return_value=2),
            patch.object(captures, "docker") as docker,
        ):
            captures.capture_cache(self.run, self.plan)
        docker.assert_not_called()
        layout = json.loads((self.run / "cache-layout.json").read_text())
        self.assertEqual([rank["rank"] for rank in layout["ranks"]], [0, 1])
        (self.run / "cache-layout.json").unlink()
        (self.run / "cache-layout.rank1.json").unlink()
        with (
            patch.object(captures, "tensor_parallel_size", return_value=2),
            self.assertRaisesRegex(ValueError, "rank 1 has not recorded its cache layout"),
        ):
            captures.capture_cache(self.run, self.plan)

    def test_capture_refuses_ranks_that_disagree(self):
        records = [self.record(0), self.record(1, sizing="other")]
        with self.assertRaisesRegex(ValueError, "ranks of one engine recorded different"):
            self.capture(2, records)

    def test_a_stopped_container_or_an_earlier_capture_stops_the_copy(self):
        (self.run / "cache-layout.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "capture exists"):
            self.capture(1, [self.record(0)])
        (self.run / "cache-layout.json").unlink()
        with (
            patch.object(captures, "docker", return_value=json.dumps({"Running": False})),
            self.assertRaisesRegex(ValueError, "serving container exited"),
        ):
            captures.capture_cache(self.run, self.plan)

    def test_the_plan_names_the_tensor_parallel_size(self):
        args = self.plan["args"]
        self.assertEqual(
            captures.tensor_parallel_size(self.plan),
            int(args[args.index("--tensor_parallel_size") + 1]),
        )


if __name__ == "__main__":
    unittest.main()
