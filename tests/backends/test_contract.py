import itertools
import json
import math
import socket
import unittest
from collections.abc import Mapping

import httpx

from narwhal.backends import EngineBackend, load, names
from narwhal.deployment.launch_engine.backend import EngineLauncher
from narwhal.engines.attestation import EngineIdentity, fetch_engine_identity
from narwhal.engines.client import EngineClient, ProbeLeg
from narwhal.engines.connector import KvConnector, PrefillResult, RendezvousConnector
from narwhal.engines.stream import sse_token_bearing
from narwhal.runtime.role_switch import RoleSwitcher
from narwhal.types import Role
from tests.backends.contract import fixtures
from tools.measurement import simulated_engine as sim

PROMPT = "contract probe"


class BackendContract(unittest.IsolatedAsyncioTestCase):
    name: str
    backend: EngineBackend

    def setUp(self):
        self.backend = load(self.name)
        self.fixtures = fixtures(self.name)

    async def engine(self, iid: str = "e0") -> sim.SimulatedEngine:
        engine = sim.SimulatedEngine(iid, prefill_s=0, token_interval_s=0.001, backend=self.name)
        await engine.start()
        self.addAsyncCleanup(engine.close)
        return engine

    def client(self, kv) -> EngineClient:
        client = EngineClient(kv=kv, dialect=self.backend.dialect, model="m")
        self.addAsyncCleanup(client.aclose)
        return client

    def test_descriptor_composes_its_parts(self):
        backend = self.backend
        self.assertEqual(backend.name, self.name)
        self.assertTrue(backend.label)
        self.assertIn(self.name, sim.PROTOCOLS)
        self.assertIn(backend.default_connector, backend.connectors)
        for key, kv in backend.connectors.items():
            with self.subTest(connector=key):
                self.assertEqual(kv.name, key)
                self.assertIsInstance(kv.contract_name, str)
                self.assertTrue(kv.contract_name)
                self.assertIsInstance(kv, KvConnector | RendezvousConnector)
                for lease in (6, 30, 600):
                    self.assertTrue(0 < kv.handoff_bound(lease) <= lease)
        for section, renamed in backend.renamed_fields.items():
            self.assertTrue(all(isinstance(v, str) for v in renamed.values()), section)

    def test_dialect_fields_and_request_identity(self):
        dialect = self.backend.dialect
        self.assertTrue(dialect.name)
        self.assertTrue(dialect.health_path.startswith("/"))
        for path in (dialect.tokenize_path, dialect.cache_reset_path):
            self.assertTrue(path is None or path.startswith("/"))
        for fields in (
            dialect.prefill_incompatible,
            dialect.engine_output_fields,
            dialect.reserved_fields,
        ):
            self.assertTrue(all(isinstance(name, str) and name for name in fields))
        self.assertGreater(dialect.keepalive_expiry_s, 0)
        headers, body = dialect.request_id("rid-1")
        self.assertIn("rid-1", [*headers.values(), *body.values()])
        self.assertEqual(bool(dialect.token_id_fields()), dialect.token_ids)
        self.assertIsInstance(dialect.decode_probe_extras(3), dict)
        cold = dialect.cold_probe_extras(), dialect.cold_probe_extras()
        self.assertTrue(cold[0] != cold[1] or not cold[0])

    async def test_health_and_tokenize_round_trip(self):
        engine = await self.engine()
        client = self.client(self.backend.connector(self.backend.default_connector))
        self.assertTrue(await client.healthy(engine.url))
        if self.backend.dialect.tokenize_path is None:
            return
        for body in (
            {"model": "m", "prompt": PROMPT},
            {"model": "m", "messages": [{"role": "user", "content": PROMPT}]},
        ):
            with self.subTest(body=list(body)):
                result = await client.tokenize(engine.url, body, 5, strict=True)
                self.assertEqual(result.count, len(PROMPT))
                self.assertIn(result.token_ids, (None, tuple(ord(c) for c in PROMPT)))

    async def test_identity_reads_the_live_engine(self):
        engine = await self.engine()
        # A backend whose engine publishes no process start reads it from its host.
        identity = await fetch_engine_identity(
            engine.url,
            reader=self.backend.identity,
            process=lambda: engine.process_start_time_seconds,
        )
        self.assertEqual(
            identity, EngineIdentity(sim.SIMULATED_VERSION, engine.process_start_time_seconds)
        )

    def test_identity_reads_attested_limits(self):
        identity, fixture = self.backend.identity, self.fixtures
        self.assertEqual(identity.sequence_limit(fixture.attestation), fixture.sequence_limit)
        self.assertEqual(identity.kv_lease(fixture.attestation), fixture.kv_lease)
        for missing in ({}, {"launch": {"args": []}}, None):
            self.assertIsNone(identity.sequence_limit(missing))
            self.assertIsNone(identity.kv_lease(missing))

    async def test_every_connector_hands_off_between_engines(self):
        producer, consumer = await self.engine("e0"), await self.engine("e1")
        for key, kv in self.backend.connectors.items():
            with self.subTest(connector=key):
                client = self.client(kv)
                result = await client.probe_inference(
                    consumer.url, prefill_url=producer.url, deadline_s=5, producer={}
                )
                self.assertEqual((result.prefill, result.decode), (ProbeLeg(), ProbeLeg()))

    async def test_descriptor_connectors_carry_the_producer_descriptor(self):
        producer, consumer = await self.engine("e0"), await self.engine("e1")
        body = {"model": "m", "prompt": PROMPT, "max_tokens": 2}
        for key, kv in self.backend.connectors.items():
            if not isinstance(kv, KvConnector):
                continue
            with self.subTest(connector=key):
                client = self.client(kv)
                result = await client.prefill(producer.url, "/v1/completions", body, {})
                self.assertIsInstance(result, PrefillResult)
                self.assertEqual((result.connector, result.producer_url), (key, producer.url))
                local = kv.decode_body(
                    {**body, kv.param_key: {}}, result, url=producer.url, endpoint="/v1/completions"
                )
                self.assertNotIn(kv.param_key, local)
                remote = kv.decode_body(body, result, url=consumer.url, endpoint="/v1/completions")
                self.assertTrue(remote["stream"])
                tokens = 0
                async for batch in client.decode(consumer.url, "/v1/completions", body, {}, result):
                    tokens += sum(sse_token_bearing(e, self.backend.dialect) for e in batch)
                self.assertGreater(tokens, 0)

    def test_rendezvous_connectors_pair_each_request(self):
        body = {"model": "m", "prompt": PROMPT}
        for key, kv in self.backend.connectors.items():
            if not isinstance(kv, RendezvousConnector):
                continue
            with self.subTest(connector=key):
                self.assertTrue(0 < kv.decode_wait_s < math.inf)
                first, second = kv.rendezvous({}), kv.rendezvous({})
                self.assertNotEqual(first, second)
                for leg in (kv.prefill_body(body, first), kv.decode_body(body, first)):
                    self.assertEqual({k: leg[k] for k in body}, body)
                    self.assertNotEqual(leg, body)

    def test_kv_event_batches_decode(self):
        decoder = self.backend.kv_events
        self.assertIsInstance(decoder.recomputes_final_token, bool)
        for payload, expected in self.fixtures.kv_batches:
            events = decoder.decode_batch(payload)
            self.assertEqual([None if e is None else type(e) for e in events], expected)
        with self.assertRaises(ValueError):
            decoder.decode_batch(self.fixtures.malformed_kv_batch)

    def test_metrics_mapping_reads_a_scrape(self):
        metrics, fixture = self.backend.metrics, self.fixtures
        self.assertEqual(metrics.kv_capacity(fixture.metrics), fixture.kv_capacity)
        self.assertEqual(metrics.cache_block_tokens(fixture.metrics), fixture.cache_block_tokens)
        self.assertEqual(metrics.prefix_cache_hits(fixture.metrics), fixture.prefix_cache_hits)
        self.assertEqual(metrics.transfer_totals(fixture.metrics), fixture.transfer_totals)
        for read in (
            metrics.kv_capacity,
            metrics.cache_block_tokens,
            metrics.prefix_cache_hits,
            metrics.transfer_totals,
        ):
            self.assertIsNone(read(""))
        self.assertIsInstance(metrics.dashboard_series, Mapping)
        self.assertTrue(all(isinstance(v, str) and v for v in metrics.dashboard_series.values()))
        if fixture.transfer_totals is not None:
            self.assertTrue(metrics.transfer_series)

    def test_fabric_release_schedule_and_bind_check(self):
        fabric = self.backend.fabric
        schedule = fabric.release_after_s
        self.assertTrue(all(0 < a < b for a, b in itertools.pairwise(schedule)))
        self.assertGreaterEqual(fabric.release_retry_s, 0)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        fabric.check_engine_bind("127.0.0.1", port)
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            with self.assertRaises(OSError):
                fabric.check_engine_bind("127.0.0.1", held.getsockname()[1])

    def test_launcher_offline_surface(self):
        launcher, fixture = self.backend.launcher(), self.fixtures
        self.assertIsInstance(launcher, EngineLauncher)
        self.assertTrue(launcher.runtime_script.is_file())
        self.assertTrue(launcher.cache_hook.is_file())
        for value in (
            launcher.side_channel,
            launcher.side_channel_port_env,
            launcher.api_key_env,
            launcher.args_field,
            launcher.version_field,
            launcher.checked_version_field,
        ):
            self.assertTrue(value)
        env = launcher.engine_env("192.0.2.11", 5600, "key")
        self.assertEqual(launcher.side_channel_address(env), ("192.0.2.11", 5600))
        self.assertEqual(env[launcher.api_key_env], "key")
        self.assertNotIn(launcher.api_key_env, launcher.engine_env("192.0.2.11", 5600, ""))
        self.assertIn("2", launcher.tensor_parallel_args(2))
        self.assertEqual(launcher.memory_fraction(fixture.memory_args), fixture.memory_fraction)
        self.assertIsNone(launcher.memory_fraction([]))
        self.assertTrue(launcher.publishes_kv_events(fixture.kv_event_args))
        self.assertFalse(launcher.publishes_kv_events(fixture.no_kv_event_args))

    async def test_role_switch_capability(self):
        switcher = self.backend.role_switch
        if switcher is None:
            return
        self.assertIsInstance(switcher, RoleSwitcher)
        self.assertIsInstance(switcher.requires_idle, bool)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text=json.dumps({"success": True}))
        )
        async with httpx.AsyncClient(transport=transport) as client:
            for role in (Role.DECODE, Role.PREFILL):
                await switcher.switch(client, "http://engine", role, {})


for _name in names():
    globals()[f"{_name.title()}ContractTests"] = type(
        f"{_name.title()}ContractTests", (BackendContract,), {"name": _name}
    )
del BackendContract, _name
