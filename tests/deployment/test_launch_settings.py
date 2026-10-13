"""Engine-neutral launch settings render through each backend's declared mapping."""

import copy
import tempfile
import unittest
from pathlib import Path

from narwhal.backends import load, names
from narwhal.deployment.launch_engine.plan import build, option, side_channel_port
from narwhal.deployment.launch_engine.settings import KV_SIDE_CHANNEL_PORT_ENV, NAMES
from tests.backends.contract import fixtures, launch_record
from tests.deployment.fixtures import launcher_inputs
from tools.deployment.prepare_host_env import ENGINE_FIELDS, select_values

NEUTRAL = {
    "context_length": 2048,
    "max_sequences": 12,
    "tokenizer": "/tokenizer",
    "prefix_caching": True,
}


class NeutralSettingsTests(unittest.TestCase):
    def built(self, name: str, changes: dict, *, extra: list[str] | None = None) -> dict:
        runtime = copy.deepcopy(fixtures(name).launch_runtime)
        runtime["extra_args"] = list(extra or [])
        runtime.update(changes)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        _, env = launcher_inputs(Path(folder.name))
        plan, _ = build(launch_record(runtime, None), env, Path(folder.name) / "launch")
        return plan

    def test_every_backend_declares_each_neutral_setting(self):
        for name in names():
            with self.subTest(backend=name):
                mapping = load(name).launcher().launch_settings
                self.assertEqual(set(mapping.settings), set(NAMES))
                for setting, rule in mapping.settings.items():
                    declared = (
                        rule.default is not None
                        or rule.fixed is not None
                        or rule.required
                        or rule.minimum is not None
                    )
                    self.assertTrue(declared, setting)

    def test_neutral_fields_render_as_each_backend_flags(self):
        for name in names():
            mapping = load(name).launcher().launch_settings.settings
            with self.subTest(backend=name):
                args = self.built(name, NEUTRAL)["args"]
                for setting in ("context_length", "max_sequences", "tokenizer"):
                    flag = mapping[setting].flag
                    self.assertEqual(option(args, flag), str(NEUTRAL[setting]), flag)
                for flag in mapping["prefix_caching"].on:
                    self.assertIn(flag, args)
                off = self.built(name, {"prefix_caching": False})
                self.assertIsNone(off["kv_events"])
                for flag in mapping["prefix_caching"].off:
                    self.assertIn(flag, off["args"])

    def test_a_launch_turns_off_cache_events_with_prefix_caching_on(self):
        for name in names():
            mapping = load(name).launcher().launch_settings
            with self.subTest(backend=name):
                on = self.built(name, {})
                self.assertIsNotNone(on["kv_events"])
                self.assertEqual(on["args"].count(mapping.kv_events_flag), 1)
                plan = self.built(name, {"kv_events": False})
                self.assertIsNone(plan["kv_events"])
                for flag in mapping.settings["prefix_caching"].off:
                    self.assertNotIn(flag, plan["args"])
                off = list(mapping.settings["kv_events"].off)
                published = [a for a in plan["args"] if a == mapping.kv_events_flag]
                self.assertEqual(len(published), 1 if off else 0)
                if off:
                    self.assertEqual(option(plan["args"], off[0]), off[1])

    def test_extra_args_reject_a_flag_a_neutral_setting_maps(self):
        for name in names():
            mapping = load(name).launcher().launch_settings.settings
            flag = str(mapping["context_length"].flag)
            for changes in ({}, {"context_length": 1024}):
                with (
                    self.subTest(backend=name, changes=changes),
                    self.assertRaisesRegex(ValueError, f"unsupported .*: {flag}$"),
                ):
                    self.built(name, changes, extra=[flag, "1024"])
            with self.subTest(backend=name), self.assertRaisesRegex(ValueError, "requires prefix"):
                self.built(name, {"prefix_caching": False, "kv_events": True})
            with self.subTest(backend=name), self.assertRaisesRegex(ValueError, "max_sequences"):
                self.built(name, {"max_sequences": 0})

    def test_sglang_fixes_the_page_size_and_caps_sequences(self):
        with self.assertRaisesRegex(ValueError, "runtime.block_size must be 1"):
            self.built("sglang", {"block_size": 16})
        with self.assertRaisesRegex(ValueError, "from 1 to 256"):
            self.built("sglang", {"max_sequences": 512})
        args = self.built("sglang", {"max_sequences": 64})["args"]
        self.assertEqual(option(args, "--max-running-requests"), "64")

    def test_the_side_channel_port_comes_from_the_neutral_variable(self):
        self.assertEqual(side_channel_port({KV_SIDE_CHANNEL_PORT_ENV: "5700"}), 5700)
        with self.assertRaises(KeyError):
            side_channel_port({"NARWHAL_SGLANG_BOOTSTRAP_PORT": "5600"})
        base = {"NARWHAL_DEPLOYMENT_REVISION": "a" * 40}
        base.update({f"NARWHAL_{field}": "synthetic" for field in ENGINE_FIELDS})
        fleet = {"engines": [], "engine": {}}
        values = select_values("engine", 1, fleet, {**base, KV_SIDE_CHANNEL_PORT_ENV: "5700"})
        self.assertEqual(values[KV_SIDE_CHANNEL_PORT_ENV], "5700")
        del base[KV_SIDE_CHANNEL_PORT_ENV]
        with self.assertRaisesRegex(ValueError, f"^{KV_SIDE_CHANNEL_PORT_ENV} is unset"):
            select_values("engine", 1, fleet, {**base, "NARWHAL_NIXL_SIDE_CHANNEL_PORT": "5600"})


if __name__ == "__main__":
    unittest.main()
