import contextlib
import io
import itertools
import json
import math
import os
import re
import socket
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from narwhal.backends import EngineBackend, load, names
from narwhal.deployment.attestation_contract.document import engine_document
from narwhal.deployment.launch_engine.backend import EngineLauncher, cache_capture
from narwhal.deployment.launch_engine.check import check
from narwhal.deployment.launch_engine.plan import load as load_plan
from narwhal.deployment.launch_engine.start import start
from narwhal.deployment.native_engine import start_shared
from narwhal.engines.attestation import EngineIdentity, fetch_engine_identity
from narwhal.engines.client import EngineClient, InferenceProbe, ProbeLeg
from narwhal.engines.connector import KvConnector, PrefillResult, RendezvousConnector
from narwhal.engines.stream import sse_token_bearing
from narwhal.runtime.role_switch import RoleSwitcher, restore_roles
from narwhal.serving.completion import completion_body_error
from narwhal.types import Role
from tests.backends.contract import (
    CONTAINER_ID,
    Engines,
    Stages,
    fixtures,
    prepared,
    random_ids,
    running_container,
)
from tests.characterization.golden import assert_golden
from tests.characterization.test_launch import env_lines, placeholders, redacted
from tests.wire import engine_transports, serving_app
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
        self.assertTrue(backend.icon.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(backend.icon), 16384)
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

    def test_the_router_rejects_each_reserved_field(self):
        for dotted in self.backend.dialect.reserved_fields:
            parent, _, key = dotted.partition(".")
            body = {"model": "m", "prompt": PROMPT, parent: {key: 1} if key else 1}
            with self.subTest(field=dotted):
                self.assertEqual(
                    completion_body_error(body, self.backend.dialect.reserved_fields),
                    (f"{dotted} is reserved for Narwhal", parent),
                )

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
            if kv.concurrent:
                continue
            with self.subTest(connector=key):
                client = self.client(kv)
                result = await client.prefill(producer.url, "/v1/completions", body, {})
                self.assertIsInstance(result, PrefillResult)
                self.assertEqual((result.connector, result.producer_url), (key, producer.url))
                local = kv.decode_body(
                    {**body, kv.param_key: {}},
                    result=result,
                    rendezvous=None,
                    url=producer.url,
                    endpoint="/v1/completions",
                )
                self.assertNotIn(kv.param_key, local)
                remote = kv.decode_body(
                    body,
                    result=result,
                    rendezvous=None,
                    url=consumer.url,
                    endpoint="/v1/completions",
                )
                self.assertTrue(remote["stream"])
                tokens = 0
                async for batch in client.decode(consumer.url, "/v1/completions", body, {}, result):
                    tokens += sum(sse_token_bearing(e, self.backend.dialect) for e in batch)
                self.assertGreater(tokens, 0)

    def test_rendezvous_connectors_pair_each_request(self):
        body = {"model": "m", "prompt": PROMPT}
        for key, kv in self.backend.connectors.items():
            if not kv.concurrent:
                continue
            with self.subTest(connector=key):
                self.assertTrue(0 < kv.decode_wait_s < math.inf)
                self.assertEqual(kv.first_token_bound(None), kv.decode_wait_s)
                self.assertEqual(kv.first_token_bound(kv.decode_wait_s / 2), kv.decode_wait_s / 2)
                first, second = kv.rendezvous({}), kv.rendezvous({})
                self.assertNotEqual(first, second)
                decode = kv.decode_body(body, result=None, rendezvous=first, url="", endpoint="")
                for leg in (kv.prefill_body(body, first), decode):
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
        capture = cache_capture(launcher)
        self.assertTrue(capture is None or capture.cache_hook.is_file())
        for value in (
            launcher.side_channel,
            launcher.side_channel_port_env,
            launcher.api_key_env,
        ):
            self.assertTrue(value)
        env = launcher.engine_env("192.0.2.11", 5600, "key")
        self.assertEqual(launcher.side_channel_address(env), ("192.0.2.11", 5600))
        self.assertEqual(env[launcher.api_key_env], "key")
        self.assertNotIn(launcher.api_key_env, launcher.engine_env("192.0.2.11", 5600, ""))
        self.assertEqual(launcher.memory_fraction(fixture.memory_args), fixture.memory_fraction)
        self.assertIsNone(launcher.memory_fraction([]))
        self.assertTrue(launcher.publishes_kv_events(fixture.kv_event_args))
        self.assertFalse(launcher.publishes_kv_events(fixture.no_kv_event_args))

    def test_launcher_discovers_a_valid_runtime_for_each_connector(self):
        launcher, fixture = self.backend.launcher(), self.fixtures
        self.assertTrue(
            all(re.fullmatch(launcher.image_packages, name) for name in fixture.image_packages)
        )
        self.assertFalse(re.fullmatch(launcher.image_packages, "unrelated-package"))
        self.assertTrue(launcher.environment_prefixes)
        self.assertLessEqual(
            set(launcher.engine_env("192.0.2.11", 5600, "key")), launcher.managed_environment
        )
        observed = {
            "packages": fixture.image_packages,
            "model_dtype": "bfloat16",
            "max_model_len": 32768,
            "image_environment": {},
        }
        for connector in self.backend.connectors:
            with self.subTest(connector=connector):
                runtime = launcher.discovered_runtime(
                    observed,
                    lambda name, default: fixture.discovery_settings.get(name, default),
                    connector,
                )
                launcher.validate_runtime(runtime)
                self.assertGreater(launcher.sequence_limit(runtime), 0)

    def test_launcher_rejects_invalid_discovery_settings(self):
        launcher, fixture = self.backend.launcher(), self.fixtures
        observed = {"packages": fixture.image_packages, "image_environment": {}}
        for name, value, message in (
            ("MODEL_DTYPE", "float32", "set MODEL_DTYPE to bfloat16 or float16 in .env"),
            ("ENGINE_ARGS", "{}", "ENGINE_ARGS must be a JSON array"),
            ("ENGINE_ENV", "[]", "ENGINE_ENV must be a JSON object"),
        ):
            settings = {**fixture.discovery_settings, name: value}
            for connector in self.backend.connectors:
                with self.subTest(name=name, connector=connector):
                    with self.assertRaises(ValueError) as raised:
                        launcher.discovered_runtime(observed, settings.get, connector)
                    self.assertEqual(str(raised.exception), message)

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


class BackendCharacterization(unittest.IsolatedAsyncioTestCase):
    """Golden records of each step a backend takes part in, recorded at the engine boundary."""

    name: str
    backend: EngineBackend

    def setUp(self):
        self.backend = load(self.name)
        self.fixtures = fixtures(self.name)
        self.launcher = self.backend.launcher()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.replacements: dict[str, str] = {}

    def golden(self, aspect: str, record: object) -> None:
        assert_golden(self, f"contract_{self.name}_{aspect}", random_ids(record), self.replacements)

    def inherited(self) -> contextlib.AbstractContextManager:
        # A native engine inherits the caller's environment; this one holds only an engine key.
        return patch.dict(os.environ, {self.launcher.api_key_env: "inherited-key"}, clear=True)

    def launched(self, native: bool) -> dict:
        """Prepare, check and start one launch, recording each step."""
        root = self.root / ("native" if native else "container")
        root.mkdir()
        run, plan, printed = prepared(root, self.launcher, self.fixtures, native=native)
        self.replacements = {**placeholders(self.root, plan), **self.replacements}
        stages = Stages(plan, self.fixtures.check_output(plan))
        record: dict = {
            "prepare": {
                "plan": redacted(plan),
                "env": env_lines(run / ("engine.env" if native else "container.env")),
                "hook_files": sorted(path.name for path in (run / "hook").iterdir()),
                "printed": printed,
            }
        }
        with (
            self.inherited() if native else contextlib.nullcontext(),
            patch("narwhal.deployment.stages.run", side_effect=stages),
            contextlib.redirect_stdout(io.StringIO()) as checked,
        ):
            check(run, plan)
        record["check"] = {
            "calls": stages.calls,
            "printed": checked.getvalue(),
            "checked": redacted(json.loads((run / "checked.json").read_text())),
        }
        stages.calls = []
        if native:
            record["start"] = self.started_native(run)
            return record
        with (
            patch("narwhal.deployment.stages.run", side_effect=stages),
            contextlib.redirect_stdout(io.StringIO()) as started,
        ):
            start(run, load_plan(run))
        record["start"] = {"calls": stages.calls, "printed": started.getvalue()}
        record["attestation"] = self.attested(run, load_plan(run))
        return record

    def started_native(self, run: Path) -> dict:
        checked = json.loads((run / "checked.json").read_text())
        live = SimpleNamespace(
            version=checked["engine_version"],
            process_start_time_seconds=1234.5,
        )
        readings = iter((1000, 6100))
        prefix = "narwhal.deployment.native_engine."
        with (
            self.inherited(),
            patch(prefix + "validate_shared_runs", return_value=[(run, load_plan(run))]),
            patch(prefix + "_ports_free"),
            patch(prefix + "gpu_memory") as memory,
            patch(prefix + "subprocess.Popen", return_value=Mock(pid=101)) as popen,
            patch(
                prefix + "process_identity",
                return_value={"pid": 101, "boot_id": "boot", "start_ticks": 10},
            ),
            patch(prefix + "_wait_ready"),
            patch(
                prefix + "fetch_engine_identity", new_callable=AsyncMock, return_value=live
            ) as fetch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            memory.side_effect = lambda _: {"used_mib": next(readings), "total_mib": 30000}
            start_shared([run], ready_seconds=30)
        return {
            "argv": popen.call_args.args[0],
            "env": dict(sorted(popen.call_args.kwargs["env"].items())),
            "identity_request": {
                "endpoint": fetch.await_args.args[0],
                "headers": fetch.await_args.kwargs["headers"],
            },
            "record": redacted(json.loads((run / "shared-start.json").read_text())),
        }

    def attested(self, run: Path, plan: dict) -> dict:
        self.fixtures.attestation_evidence(run, plan, CONTAINER_ID)
        log = run / "startup.log"
        log.write_text(self.fixtures.startup_log)
        checked = json.loads((run / "checked.json").read_text())
        with running_container(checked["image_id"]):
            document = engine_document(run, log)
        document["sources"] = {
            field: re.sub(r"sha256:[0-9a-f]{64}$", "sha256:<digest>", source)
            for field, source in document["sources"].items()
        }
        return redacted(document)

    def launches(self) -> dict:
        return {"container": self.launched(False), "native": self.launched(True)}

    def test_launch_command_and_environment(self):
        launches = self.launches()
        self.golden(
            "launch",
            {
                mode: {"prepare": record["prepare"], "start": record["start"]}
                for mode, record in launches.items()
            },
        )

    def test_runtime_check_result(self):
        launches = self.launches()
        self.golden("runtime_check", {mode: record["check"] for mode, record in launches.items()})

    def test_attestation_document(self):
        self.golden("attestation", self.launched(False)["attestation"])

    async def test_handoff_request_bodies_and_order(self):
        engines = Engines(self.name, self.fixtures.handoff_random_fields)
        launch = self.fixtures.attestation["launch"]
        producer, consumer = "http://stub-0", "http://stub-1"
        switcher = self.backend.role_switcher(self.backend.default_connector)
        assert switcher is not None
        async with httpx.AsyncClient(transport=httpx.MockTransport(engines)) as control:
            await switcher.place(control, (producer, launch), (consumer, launch))
        record: dict = {"placement": engines.take()}
        router, client = serving_app(self, engines, urls=(producer, consumer), backend=self.name)
        router.attested("p", self.fixtures.attestation)
        for case, endpoint, body in (
            ("serving_completions", "/v1/completions", {"prompt": PROMPT, "max_tokens": 4}),
            (
                "serving_chat_stream",
                "/v1/chat/completions",
                {
                    "messages": [{"role": "user", "content": PROMPT}],
                    "max_completion_tokens": 4,
                    "stream": True,
                },
            ),
        ):
            response = await client.post(endpoint, json={"model": "stub", **body})
            self.assertEqual(response.status_code, 200, response.text)
            record[case] = engines.handoff()
        kv = self.backend.connector(self.backend.default_connector)
        engine = EngineClient(
            kv=kv, dialect=self.backend.dialect, model="m", **engine_transports(engines)
        )
        self.addAsyncCleanup(engine.aclose)
        body = {"model": "m", "prompt": PROMPT, "max_tokens": 4}
        handoff = await engine.start_handoff(producer, "/v1/completions", body, {}, producer=launch)
        async for _ in engine.decode_handoff(handoff, consumer, "/v1/completions", body, {}):
            pass
        record["paired_handoff"] = engines.handoff()
        probe = await engine.probe_inference(
            consumer, prefill_url=producer, deadline_s=5, producer=launch
        )
        self.assertEqual(probe, InferenceProbe(ProbeLeg(), ProbeLeg()))
        record["inference_probe"] = engines.handoff()
        self.golden("handoff", record)

    async def test_role_switch_calls(self):
        engines = Engines(self.name, ())
        launch = self.fixtures.attestation["launch"]
        switcher = self.backend.role_switcher(self.backend.default_connector)
        assert switcher is not None
        a, b, c = "http://a", "http://b", "http://c"
        record: dict = {
            "requires_idle": switcher.requires_idle,
            "connectors": None if switcher.connectors is None else sorted(switcher.connectors),
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(engines)) as client:
            await switcher.place(client, (a, launch), (b, launch))
            record["place"] = engines.take()
            await switcher.place(client, (a, launch), (b, launch))
            record["place_again"] = engines.take()
            await switcher.switch(client, a, Role.DECODE, launch)
            record["switch_producer_to_decode"] = engines.take()
            await switcher.place(client, (a, launch), (b, launch))
            record["place_after_producer_switch"] = engines.take()
            roles = {base: await switcher.role(client, base) for base in (a, b, c)}
            record["roles"] = {
                base: None if role is None else role.value for base, role in roles.items()
            }
            record["role_reads"] = engines.take()
            await restore_roles(
                switcher,
                client,
                {a: (Role.DECODE, launch), b: (Role.PREFILL, launch), c: (Role.DECODE, launch)},
            )
            record["restore"] = engines.take()
        self.golden("role_switch", record)


for _name in names():
    globals()[f"{_name.title()}ContractTests"] = type(
        f"{_name.title()}ContractTests", (BackendContract,), {"name": _name}
    )
    globals()[f"{_name.title()}CharacterizationTests"] = type(
        f"{_name.title()}CharacterizationTests", (BackendCharacterization,), {"name": _name}
    )
del BackendContract, BackendCharacterization, _name
