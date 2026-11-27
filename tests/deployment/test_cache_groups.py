import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment.cache_groups import EVIDENCE, derive, signature, static_inputs
from narwhal.dev.cli import main
from tests.deployment.fixtures import launch_document, runtime

MODEL = "c" * 64


def layout(tp=2, page_bytes=4096, layout_name="HND"):
    return {
        "schema_version": 1,
        "sizing": "runtime_padded_page_upper_bound",
        "model_config_sha256": MODEL,
        "plan_sha256": "d" * 64,
        "ranks": [
            {
                "rank": rank,
                "kv_cache_layout": layout_name,
                "layers": [
                    {"layer": "b", "page_bytes": page_bytes, "block_tokens": 16, "extra_blocks": 0},
                    {"layer": "a", "page_bytes": page_bytes, "block_tokens": 16, "extra_blocks": 0},
                ],
            }
            for rank in reversed(range(tp))
        ],
    }


def transfer(payload=1_048_576):
    return {
        "schema": "narwhal.kv-transfer-measurement",
        "schema_version": 1,
        "series": "kv_transfer_total_mb",
        "prompt_tokens": 1024,
        "payload_bytes_per_handoff": payload,
        "handoffs": 8,
        "metrics_sha256": {"before.prom": "e" * 64, "after.prom": "f" * 64},
    }


class CacheGroupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        engine = launch_document()["engines"]["engine-1"]
        engine["runtime"] = runtime()
        self.engines = {f"engine-{n}": copy.deepcopy(engine) for n in (1, 2, 3, 10)}

    def write(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document))
        return path

    def launch_config(self):
        for role, engine in self.engines.items():
            observed = {"image_id": "sha256:" + "a" * 64, "model_sha256": MODEL}
            engine["sources"]["runtime"] = str(self.write(f"{role}-discovery.json", observed))
        return self.write(
            "launch.json",
            {"schema": "narwhal.engine-launch", "schema_version": 1, "engines": self.engines},
        )

    def layouts(self, **overrides):
        # Each call writes its own files, so a later call leaves earlier layouts as they were.
        self.calls = getattr(self, "calls", 0) + 1
        return {
            role: self.write(f"{role}-layout-{self.calls}.json", overrides.get(role, layout()))
            for role in self.engines
        }

    def test_matching_engines_share_one_group_led_by_the_lowest_numbered_role(self):
        launches = self.launch_config()
        document = derive(launches, self.layouts(), {}, self.root / "groups")
        self.assertEqual(json.loads((self.root / "groups" / EVIDENCE).read_text()), document)
        self.assertEqual(document["schema"], "narwhal.cache-groups")
        [group] = document["groups"]
        self.assertEqual(group["representative"], "engine-1")
        self.assertEqual(group["roles"], ["engine-1", "engine-2", "engine-3", "engine-10"])
        inputs, _ = static_inputs("engine-1", self.engines["engine-1"])
        self.assertEqual(group["signature"], signature(inputs))
        self.assertEqual(
            [rank["kv_cache_layout"] for rank in group["geometry"]["ranks"]], ["HND"] * 2
        )
        self.assertEqual(
            [layer["layer"] for layer in group["geometry"]["ranks"][0]["layers"]], ["a", "b"]
        )
        self.assertEqual(document["engines"]["engine-10"]["group"], group["id"])

    def test_static_inputs_and_live_geometry_each_split_a_group(self):
        self.engines["engine-3"]["tensor_parallel_size"] = 1
        self.engines["engine-3"]["gpu_ids"] = ["0"]
        launches = self.launch_config()
        layouts = self.layouts(
            **{
                "engine-3": layout(tp=1),
                "engine-2": layout(page_bytes=8192),
                "engine-10": layout(layout_name="NHD"),
            }
        )
        document = derive(launches, layouts, {}, self.root / "groups")
        self.assertEqual(
            [group["roles"] for group in document["groups"]],
            [["engine-1"], ["engine-2"], ["engine-3"], ["engine-10"]],
        )
        signatures = {role: row["signature"] for role, row in document["engines"].items()}
        self.assertEqual(signatures["engine-1"], signatures["engine-2"])
        self.assertNotEqual(signatures["engine-1"], signatures["engine-3"])

    def test_measured_handoffs_group_engines_that_measure_instead_of_capturing(self):
        for engine in self.engines.values():
            engine["runtime"] = {"backend": "sglang", "extra_args": []}
        launches = self.launch_config()
        transfers = {
            role: self.write(f"{role}-transfer.json", transfer(2 if role == "engine-3" else 1))
            for role in self.engines
        }
        document = derive(launches, {}, transfers, self.root / "groups")
        self.assertEqual(
            [group["roles"] for group in document["groups"]],
            [["engine-1", "engine-2", "engine-10"], ["engine-3"]],
        )
        self.assertEqual(document["groups"][0]["geometry"]["kind"], "transfer_record")

    def test_refusals_name_the_role_and_its_evidence(self):
        launches = self.launch_config()
        cases = (
            ({}, {}, "engine-1: its engine needs --cache-layout"),
            (
                self.layouts(),
                {"engine-1": self.root / "x"},
                "engine-1: its engine takes no --transfer-record",
            ),
            ({**self.layouts(), "engine-9": self.root / "x"}, {}, "engine-9: not a role of"),
            (
                self.layouts(**{"engine-2": layout(tp=1)}),
                {},
                "engine-2: the cache layout needs every one of its 2 TP ranks once",
            ),
            (
                self.layouts(**{"engine-2": {**layout(), "model_config_sha256": "0" * 64}}),
                {},
                "engine-2: the cache layout belongs to another model configuration",
            ),
            (
                self.layouts(**{"engine-2": {**layout(), "sizing": "uniform"}}),
                {},
                "engine-2: the cache layout records no",
            ),
            (
                {**self.layouts(), "engine-3": self.root / "missing.json"},
                {},
                "engine-3: read .*missing.json",
            ),
        )
        for index, (layouts, transfers, message) in enumerate(cases):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    derive(launches, layouts, transfers, self.root / f"groups-{index}")
                self.assertFalse((self.root / f"groups-{index}" / EVIDENCE).exists())

    def test_derive_refuses_earlier_groups(self):
        launches = self.launch_config()
        out = self.root / "groups"
        out.mkdir()
        (out / EVIDENCE).write_text("earlier")
        with self.assertRaises(FileExistsError):
            derive(launches, self.layouts(), {}, out)
        self.assertEqual((out / EVIDENCE).read_text(), "earlier")

    def test_deploy_command_prints_each_group_and_returns_a_result(self):
        launches = self.launch_config()
        layouts = self.layouts(**{"engine-2": layout(page_bytes=8192)})
        arguments = ["deploy", "cache-groups", "--out", str(self.root / "groups")]
        for role, path in layouts.items():
            arguments += ["--cache-layout", f"{role}={path}"]
        with (
            patch.dict("os.environ", {"NARWHAL_LAUNCH_CONFIG": str(launches)}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main([*arguments, "--format", "json"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [group["roles"] for group in result["data"]["groups"]],
            [["engine-1", "engine-3", "engine-10"], ["engine-2"]],
        )
        with (
            patch.dict("os.environ", {"NARWHAL_LAUNCH_CONFIG": str(launches)}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main([*arguments[:3], str(self.root / "again"), *arguments[4:]]), 0)
        lines = stdout.getvalue().splitlines()
        self.assertRegex(
            lines[0], r"^[0-9a-f]{16}: representative=engine-1; roles=engine-1,engine-3,engine-10$"
        )
        with (
            patch.dict("os.environ", {"NARWHAL_LAUNCH_CONFIG": str(launches)}),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            duplicate = [*arguments, "--cache-layout", f"engine-1={layouts['engine-1']}"]
            self.assertEqual(main([*duplicate[:3], str(self.root / "third"), *duplicate[4:]]), 2)
        self.assertIn("Give one --cache-layout for each role", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            main([*arguments[:4], "--cache-layout", "router=x"])
        self.assertIn("Use ROLE=PATH with an engine-<n> role", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
