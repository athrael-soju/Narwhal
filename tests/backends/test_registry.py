import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from narwhal.backends import EngineBackend, load, names
from narwhal.config.loading import load as load_fleet
from narwhal.deployment.launch_engine.backend import EngineLauncher
from narwhal.engines.attestation import EngineIdentityReader
from narwhal.engines.connector import KvHandoff
from narwhal.engines.dialect import EngineDialect
from narwhal.engines.kv_events import KvEventDecoder
from narwhal.engines.metrics import EngineMetrics
from narwhal.runtime.fabric import FabricLifecycle
from narwhal.runtime.role_switch import RoleSwitcher
from narwhal.types import Role

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/maintenance"))
import check_backend_boundary  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_every_registered_backend_implements_each_interface(self):
        self.assertIn("vllm", names())
        for name in names():
            backend = load(name)
            with self.subTest(backend=name):
                self.assertIsInstance(backend, EngineBackend)
                self.assertEqual(backend.name, name)
                self.assertIsInstance(backend.dialect, EngineDialect)
                self.assertTrue(backend.connectors)
                for connector in backend.connectors.values():
                    self.assertIsInstance(connector, KvHandoff)
                self.assertIsInstance(backend.identity, EngineIdentityReader)
                self.assertIsInstance(backend.kv_events, KvEventDecoder)
                self.assertIsInstance(backend.metrics, EngineMetrics)
                self.assertIsInstance(backend.fabric, FabricLifecycle)
                self.assertIsInstance(backend.launcher(), EngineLauncher)
                if backend.role_switch is not None:
                    self.assertIsInstance(backend.role_switch, RoleSwitcher)

    def test_unknown_backend_and_connector_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown engine backend 'nope'.*vllm"):
            load("nope")
        with self.assertRaisesRegex(ValueError, "no connector 'nope'.*nixl"):
            load("vllm").connector("nope")

    def test_fleet_config_selects_and_validates_the_backend(self):
        fleet = json.loads((ROOT / "tests/data/fleet.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet.json"
            path.write_text(json.dumps(fleet))
            self.assertEqual(load_fleet(path).backend, "vllm")
            for engine, error in (
                ({"backend": "nope"}, "unknown engine backend 'nope'"),
                ({"connector": "nope"}, "backend 'vllm' has no connector 'nope'"),
                ({"dialect": "nope"}, "backend 'vllm' has no dialect 'nope'"),
            ):
                path.write_text(
                    json.dumps({**fleet, "engine": {**fleet.get("engine", {}), **engine}})
                )
                with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, error):
                    load_fleet(path)


class VllmBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = load("vllm")

    def test_handoff_bound_leaves_one_renewal_interval(self):
        nixl = self.backend.connector("nixl")
        for lease in (6, 7, 30, 60):
            self.assertEqual(nixl.handoff_bound(lease), lease - lease // 6)

    def test_transfer_totals_sum_ranks(self):
        text = (
            'vllm:nixl_xfer_time_seconds_count{rank="0"} 2\n'
            'vllm:nixl_xfer_time_seconds_sum{rank="0"} 0.5\n'
            'vllm:nixl_xfer_time_seconds_count{rank="1"} 3\n'
            'vllm:nixl_xfer_time_seconds_sum{rank="1"} 1.0\n'
        )
        self.assertEqual(self.backend.metrics.transfer_totals(text), (5.0, 1.5))
        self.assertIsNone(self.backend.metrics.transfer_totals("other 1\n"))

    def test_role_change_needs_no_engine_request(self):

        def refuse(request):
            raise AssertionError(request.url)

        async def switch():
            async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
                await self.backend.role_switch.switch(client, "http://stub-0", Role.DECODE)

        asyncio.run(switch())
        self.assertFalse(self.backend.role_switch.requires_idle)


class BoundaryTests(unittest.TestCase):
    def test_coupling_is_found_in_code_and_imports_but_not_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "module.py"
            for source, expected in (
                ("# vLLM closes idle connections.\nx = 1\n", False),
                ('PATH = "/reset_prefix_cache_vllm"\n', True),
                ("from ..backends.vllm import backend\n", True),
                ("from ..backends import load\n", False),
                ("nixl = True\n", True),
                ('message = f"{iid} vLLM {version}"\n', True),
            ):
                path.write_text(source)
                with self.subTest(source=source):
                    self.assertEqual(check_backend_boundary.coupled(path), expected)

    def test_repository_respects_the_boundary(self):
        self.assertEqual(check_backend_boundary.main(), 0)
