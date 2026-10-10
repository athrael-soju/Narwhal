import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load
from narwhal.config import EngineContract
from narwhal.config.validation import validate
from narwhal.diagnostics.check import transfer
from narwhal.diagnostics.check.report import Report
from narwhal.engines.attestation import (
    PROCESS_PATH,
    AttestationDocument,
    EngineIdentity,
    build_app,
)
from narwhal.engines.client import EngineClient, InferenceProbe, ProbeLeg
from narwhal.profiling import calibration
from narwhal.profiling.probe.instance import profile_instance
from narwhal.profiling.probe.pairing import PairedTransport, Pairing, origin
from narwhal.profiling.probe.sweep import Sweep
from narwhal.runtime.role_switch import place_pair
from narwhal.serving.app import create_app
from narwhal.types import Role
from tests.fixtures import bind_identity_profiles, fleet
from tools.measurement import simulated_engine as sim

LAUNCH = {"bootstrap": {"bootstrap_host": "127.0.0.1", "bootstrap_port": 1}}


class PairedEngines(unittest.IsolatedAsyncioTestCase):
    name = "sglang"
    connector = "mooncake"

    async def asyncSetUp(self):
        self.backend = load(self.name)
        rooms = sim.Rooms(2.0) if self.name == "sglang" else None
        self.engines = []
        for iid in ("e0", "e3"):
            engine = sim.SimulatedEngine(
                iid, prefill_s=0.001, token_interval_s=0.001, backend=self.name, rooms=rooms
            )
            await engine.start()
            self.addAsyncCleanup(engine.close)
            self.engines.append(engine)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.cfg.backend, self.cfg.connector = self.name, self.connector
        self.cfg.dialect = self.backend.dialect.name
        self.cfg.engine_contract = None
        self.cfg.engines = [
            replace(spec, url=engine.url)
            for spec, engine in zip(self.cfg.engines, self.engines, strict=True)
        ]
        self.client = EngineClient(
            kv=self.backend.connector(self.connector),
            dialect=self.backend.dialect,
            model=self.cfg.model,
            read_timeout_s=5,
        )
        self.addAsyncCleanup(self.client.aclose)
        launches = patch.object(
            transfer, "attested_launches", new=AsyncMock(return_value={"e0": LAUNCH, "e3": LAUNCH})
        )
        launches.start()
        self.addCleanup(launches.stop)


class SglangPreflightTests(PairedEngines):
    async def preflight(self) -> str:
        report, output = Report(), io.StringIO()
        with redirect_stdout(output):
            handoffs = await transfer.gate_produce(self.cfg, {"e0", "e3"}, self.client, report)
            self.assertEqual(set(handoffs), {"e0", "e3"})
            await transfer.gate_consume(
                self.cfg, {"e0", "e3"}, handoffs, self.client, report, mesh=True
            )
        self.assertEqual(report.failed, [])
        return output.getvalue()

    async def test_consume_moves_kv_through_each_switched_pair(self):
        self.assertEqual((await self.preflight()).count("moved KV"), 2)
        self.assertEqual([engine.transfers for engine in self.engines], [1, 1])
        # The last pair ran e3 -> e0.
        self.assertEqual([engine.role for engine in self.engines], ["decode", "prefill"])

    async def test_a_failed_prefill_leg_is_the_reported_cause(self):
        handoff = await self.client.start_handoff(
            "http://127.0.0.1:9", "/v1/completions", {"model": "m", "prompt": "p"}, {}
        )
        self.engines[1].role = "decode"
        with self.assertRaises(httpx.ConnectError):
            async for _ in self.client.decode_handoff(
                handoff,
                self.engines[1].url,
                "/v1/completions",
                {"model": "m", "prompt": "p", "max_tokens": 2},
                {},
                first_token_timeout_s=0.2,
            ):
                pass

    async def test_calibration_step_measures_each_pair(self):
        pairs = [("e0", "e3"), ("e3", "e0")]
        async with httpx.AsyncClient() as sizing:
            lockstep = calibration._Lockstep(
                self.cfg,
                self.client,
                sizing,
                self.backend.dialect,
                5.0,
                {"e0": 4096, "e3": 4096},
                {"e0": LAUNCH, "e3": LAUNCH},
                self.backend.role_switcher(self.connector),
            )
            for pair in pairs:
                await lockstep.step([pair], 128, 1, None)
        rows = list(lockstep.rows.values())
        self.assertEqual([row["status"] for row in rows], ["completed", "completed"])
        self.assertTrue(all(row["prefill_seconds"] > 0 for row in rows))

    async def test_profile_measures_both_roles_through_a_peer(self):
        target, peer = self.engines
        # A prefill time well above scheduling jitter keeps the fit within its error bound.
        target.prefill_s = peer.prefill_s = 0.05
        pairing = PairedTransport(httpx.AsyncHTTPTransport(), self.client.kv, self.backend.dialect)
        switcher = self.backend.role_switcher(self.connector)

        async def roles(role):
            own, other = (target.url, LAUNCH), (peer.url, LAUNCH)
            producer, consumer = (own, other) if role is Role.PREFILL else (other, own)
            async with httpx.AsyncClient() as control:
                await place_pair(switcher, control, producer, consumer)
            pairing.pairs[origin(target.url)] = Pairing(role, origin(peer.url), LAUNCH)

        sweep = Sweep(
            prefill_lens=(64, 128, 256),
            decode_concurrency=(1, 2),
            decode_tokens=8,
            prefill_repeats=1,
            decode_input_lens=(64, 128),
        )
        async with httpx.AsyncClient(transport=pairing, timeout=10) as client:
            profile = await profile_instance(
                client,
                "e0",
                target.url,
                self.cfg.model,
                self.backend.dialect,
                sweep,
                metrics=self.backend.metrics,
                max_model_len=4096,
                observation_timeout_s=10,
                roles=roles,
            )
        self.assertGreater(profile.tpot_intercept, 0)
        self.assertEqual(profile.kv_capacity_tokens, sim.MAX_MODEL_LEN)
        # The peer decoded each prefill sample; the target decoded each decode stream.
        self.assertEqual(peer.transfers, 3)
        self.assertEqual(target.transfers, 6)

    async def test_paired_transport_passes_other_routes_through(self):
        pairing = PairedTransport(httpx.AsyncHTTPTransport(), self.client.kv, self.backend.dialect)
        pairing.pairs[origin(self.engines[0].url)] = Pairing(
            Role.PREFILL, origin(self.engines[1].url), LAUNCH
        )
        async with httpx.AsyncClient(transport=pairing) as client:
            response = await client.post(
                f"{self.engines[0].url}/tokenize", json={"model": "m", "prompt": "abc"}
            )
        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(self.engines[1].transfers, 0)


class VllmPreflightTests(PairedEngines):
    name = "vllm"
    connector = "nixl"

    async def test_consume_runs_the_descriptor_flow(self):
        self.assertEqual((await SglangPreflightTests.preflight(self)).count("moved KV"), 2)
        transfer.attested_launches.assert_not_awaited()


class RoundTests(unittest.TestCase):
    def test_switching_engines_serve_one_leg_per_round(self):
        pairs = [("a", "b"), ("b", "a"), ("a", "c"), ("c", "b")]
        rounds = calibration.exclusive_rounds(pairs, {"a": "a", "b": "b", "c": "c"})
        self.assertEqual(sorted(pair for members in rounds for pair in members), sorted(pairs))
        for members in rounds:
            engines = [iid for pair in members for iid in pair]
            self.assertEqual(len(engines), len(set(engines)))


class RoleBoundVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name), engines=("e0", "e3", "e5"))
        self.cfg.engine_contract = None
        self.router = create_app(self.cfg).state.router
        bind_identity_profiles(self.router)
        self.addAsyncCleanup(self.router.engines.aclose)
        self.router.scheduler.availability.roles_bound = True
        instances = self.router.monitor.instances
        instances["e0"].role, instances["e3"].role, instances["e5"].role = (
            Role.PREFILL,
            Role.DECODE,
            Role.DECODE,
        )
        self.verifier = self.router.verifier

    def test_a_decode_suspect_never_gets_a_standalone_probe(self):
        self.assertEqual(self.verifier.producers("e3", "e0"), ["e0"])
        self.router.scheduler.eject("e0", "test")
        self.assertEqual(self.verifier.producers("e3", "e0"), [""])

    async def test_a_decode_suspect_without_a_producer_defers(self):
        self.router.scheduler.eject("e0", "test")
        probe = AsyncMock()
        with patch.object(self.router.engines, "probe_inference", new=probe):
            self.assertFalse(await self.verifier._verify_path("e3", "http://e3", "e0"))
        probe.assert_not_awaited()
        self.assertEqual(self.verifier.probes[("e3", "verify_inference", "inconclusive")], 1)

    async def test_a_prefill_suspect_is_probed_ahead_of_a_decode_peer(self):
        probes = [
            InferenceProbe(ProbeLeg(), ProbeLeg(failed="stream")),
            InferenceProbe(ProbeLeg(failed="inference_status"), ProbeLeg(inconclusive=True)),
        ]
        probe = AsyncMock(side_effect=probes)
        with patch.object(self.router.engines, "probe_inference", new=probe):
            self.assertFalse(await self.verifier._verify_path("e0", "http://e0", ""))
        self.assertEqual(
            [call.kwargs["prefill_url"] for call in probe.await_args_list],
            ["http://e0", "http://e0"],
        )
        outcomes = self.verifier.probes
        self.assertEqual(outcomes[("e0", "verify_inference", "consumer_failed")], 1)
        self.assertEqual(outcomes[("e0", "verify_inference", "failed")], 1)


class ContractFieldTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.cfg.backend, self.cfg.connector, self.cfg.dialect = "sglang", "mooncake", "sglang"

    def problems(self):
        try:
            validate(self.cfg)
        except ValueError as exc:
            return str(exc)
        return ""

    def test_a_backend_attests_only_its_own_fields(self):
        contract = self.cfg.engine_contract
        self.assertTrue(contract.enforce_handshake_compat)
        self.assertIn("enforce_handshake_compat is not attested", self.problems())
        self.cfg.engine_contract = replace(
            contract,
            enforce_handshake_compat=None,
            connector_version=0,
            cross_layers_blocks=None,
            hybrid_kv_cache_manager=None,
            kv_role="",
            transfer_mode="",
        )
        self.assertEqual(self.cfg.contract_missing(), [])
        self.assertNotIn("engine_contract", self.problems())

    def test_fixed_roles_need_every_engine_pinned(self):
        self.cfg.connector = "nixl"
        self.assertIn("set pin on every engine", self.problems())
        self.cfg.engines = [replace(spec, pin=True) for spec in self.cfg.engines]
        self.assertNotIn("set pin on every engine", self.problems())


class ProcessRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_sidecar_reports_the_host_process_start(self):
        contract = EngineContract(
            engine_version="1.0", connector="mooncake", enforce_handshake_compat=None
        )
        app = build_app(
            AttestationDocument(contract, {}),
            "http://engine",
            EngineIdentity("1.0", 5.0),
            reader=load("sglang").identity,
            process=lambda: 5.0,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sidecar"
        ) as client:
            response = await client.get(PROCESS_PATH)
        self.assertEqual(response.json(), {"process_start_time_seconds": 5.0})


if __name__ == "__main__":
    unittest.main()
