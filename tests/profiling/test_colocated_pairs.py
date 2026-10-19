import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load as load_backend
from narwhal.backends.sglang.connector import FAKE_BOOTSTRAP_HOST
from narwhal.backends.sglang.dialect import SglangDialect
from narwhal.config.model import SharedDeviceAllocation
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.probe import fleet as fleet_probe
from narwhal.profiling.probe import neighbours as neighbours_probe
from narwhal.profiling.probe import pairing as pairing_probe
from narwhal.profiling.probe.neighbours import ColocatedWorkload, NeighbourLoad
from narwhal.profiling.probe.pairing import PairedTransport
from narwhal.profiling.store import ProfileStore
from narwhal.types import Role
from tests.fixtures import fleet, profile

WORKLOAD = ColocatedWorkload(200, 200, 32, 32, 4)
GROUP = SharedDeviceAllocation("gpu-0", "uuid", 0.2, 0.2)


class Body(httpx.AsyncByteStream):
    """A response body read off the wire, so the client closes it after reading."""

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self.payload


class SglangGroup:
    """Four SGLang engines on one GPU that refuse a switch or cache reset while busy."""

    def __init__(self, hosts):
        # Each engine launches as prefill.
        self.roles = dict.fromkeys(hosts, Role.PREFILL)
        self.inflight = dict.fromkeys(hosts, 0)
        self.received = []
        self.busy_controls = []

    def counts(self):
        return tuple(
            sum(role is wanted for role in self.roles.values())
            for wanted in (Role.PREFILL, Role.DECODE)
        )

    async def handle(self, request):
        host, path = request.url.host, request.url.path
        if path == "/server_info":
            return httpx.Response(
                200, json={"internal_states": [{"disaggregation_mode": self.roles[host].value}]}
            )
        if path in ("/pd_role_switch", "/flush_cache"):
            if any(self.inflight.values()):
                self.busy_controls.append((host, path, dict(self.inflight)))
            if path == "/pd_role_switch":
                self.roles[host] = Role(json.loads(request.content)["new_role"])
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200)
        if path == "/health":
            return httpx.Response(200)
        body = json.loads(request.content)
        role = self.roles[host]
        self.received.append(
            {
                "host": host,
                "role": role,
                "prompt": body["prompt"],
                "fake": body.get("bootstrap_host") == FAKE_BOOTSTRAP_HOST,
                "room": body.get("bootstrap_room"),
            }
        )
        self.inflight[host] += 1
        try:
            await asyncio.sleep(0.002)
        finally:
            self.inflight[host] -= 1
        tokens = 1 if role is Role.PREFILL else body["max_tokens"]
        usage = {"choices": [{"text": "x"}], "usage": {"completion_tokens": tokens}}
        return httpx.Response(200, stream=Body(usage))


def split_fleet(root, prefill):
    cfg = fleet(root, engines=("e0", "e1", "e2", "e3"))
    cfg.backend, cfg.connector = "sglang", "mooncake"
    cfg.engines = [
        replace(
            spec,
            shared_device=GROUP,
            role=Role.PREFILL if index < prefill else Role.DECODE,
        )
        for index, spec in enumerate(cfg.engines)
    ]
    cfg.profiles_path = root / f"profiles-{prefill}p.json"
    return cfg


class ColocatedPairsTests(unittest.IsolatedAsyncioTestCase):
    async def profile_split(self, prefill, *, peerless=True):
        with tempfile.TemporaryDirectory() as folder:
            cfg = split_fleet(Path(folder), prefill)
            hosts = {httpx.URL(spec.url).host: spec for spec in cfg.engines}
            group = SglangGroup(hosts)
            phases = []
            targets = []

            async def measured(client, iid, url, model, dialect, sweep, *args, roles, **kwargs):
                targets.append((iid, len(group.received)))
                for role in (Role.PREFILL, Role.DECODE, Role.PREFILL):
                    await roles(role)
                    phases.append((iid, role, group.roles[httpx.URL(url).host], group.counts()))
                    for _ in range(2):
                        response = await client.post(
                            f"{url}/v1/completions",
                            json={"model": model, "prompt": "measured", "max_tokens": 4},
                        )
                        response.raise_for_status()
                    await asyncio.sleep(0.04)
                targets[-1] = (iid, targets[-1][1], len(group.received))
                return profile(iid)

            launches = {
                spec.iid: {
                    "bootstrap": {"bootstrap_host": f"10.0.0.{index}", "bootstrap_port": 8998}
                }
                for index, spec in enumerate(cfg.engines)
            }
            transport = httpx.MockTransport(group.handle)
            real_client = httpx.AsyncClient
            fields = None if not peerless else SglangDialect.peerless_fields

            def client(*args, **kwargs):
                kwargs.setdefault("transport", transport)
                return real_client(*args, **kwargs)

            def mock_transport(*args, **kwargs):
                return transport

            with (
                patch.object(fleet_probe.httpx, "AsyncClient", side_effect=client),
                patch.object(fleet_probe.httpx, "AsyncHTTPTransport", side_effect=mock_transport),
                patch.object(pairing_probe, "attested_launches", AsyncMock(return_value=launches)),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=16384)
                ),
                patch.object(
                    neighbours_probe, "make_prompt", AsyncMock(return_value=("neighbour", 32))
                ),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(return_value=GenerationEvidence("sha256:" + "a" * 64, {}, 100.0)),
                ),
                patch.object(fleet_probe, "profile_instance", side_effect=measured),
                patch.object(
                    SglangDialect,
                    "peerless_fields",
                    autospec=True,
                    side_effect=fields or (lambda self: None),
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = await fleet_probe.run(cfg, None, colocated_workload=WORKLOAD)
            self.assertEqual(code, 0)
            evidence = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            stored = ProfileStore(cfg.profiles_path).all_profiles()
            return cfg, group, phases, targets, evidence, stored

    async def assert_split(self, prefill, *, peerless):
        cfg, group, phases, targets, evidence, stored = await self.profile_split(
            prefill, peerless=peerless
        )
        split = (prefill, len(cfg.engines) - prefill)
        self.assertEqual([iid for iid, *_ in targets], [spec.iid for spec in cfg.engines])
        # Every phase keeps the split's role counts, with the target in the phase's role.
        self.assertEqual(len(phases), 3 * len(cfg.engines))
        for iid, wanted, live, counts in phases:
            self.assertIs(live, wanted, iid)
            self.assertEqual(counts, split, (iid, wanted))
        # No neighbour leg reaches the target while it is measured.
        hosts = {spec.iid: httpx.URL(spec.url).host for spec in cfg.engines}
        for iid, start, end in targets:
            on_target = [row for row in group.received[start:end] if row["host"] == hosts[iid]]
            self.assertTrue(on_target)
            self.assertEqual({row["prompt"] for row in on_target}, {"measured"}, iid)
        # The engines switch and reset their caches only with no request in flight.
        self.assertEqual(group.busy_controls, [])
        neighbour_rows = [row for row in group.received if row["prompt"] == "neighbour"]
        self.assertTrue(neighbour_rows)
        for spec in cfg.engines:
            row = evidence["engines"][spec.iid]
            load = row["colocated_load"]
            self.assertGreater(load["paused_s"], 0)
            self.assertEqual(row["peer"] in hosts, True)
            partner = next(peer for peer in cfg.engines if peer.iid == row["peer"])
            self.assertIsNot(partner.role, spec.role)
        self.assertEqual(
            {(row.colocated_prefill_engines, row.colocated_decode_engines) for row in stored},
            {split},
        )
        return neighbour_rows, evidence

    async def test_peerless_neighbours_serve_alone_at_each_split(self):
        for prefill in (1, 2, 3):
            with self.subTest(prefill=prefill):
                rows, evidence = await self.assert_split(prefill, peerless=True)
                self.assertTrue(all(row["fake"] for row in rows))
                self.assertEqual(len({row["room"] for row in rows}), len(rows))
                for row in evidence["engines"].values():
                    self.assertTrue(row["colocated_load"]["peerless"])
                    for peer in row["colocated_load"]["peers"].values():
                        self.assertNotIn("withheld_s", peer)
                        self.assertGreater(peer["completed"], 0)

    async def test_neighbours_pair_among_themselves_and_withhold_without_a_partner(self):
        for prefill in (1, 2, 3):
            with self.subTest(prefill=prefill):
                rows, evidence = await self.assert_split(prefill, peerless=False)
                self.assertFalse(any(row["fake"] for row in rows))
                withheld = {
                    iid
                    for row in evidence["engines"].values()
                    for iid, peer in row["colocated_load"]["peers"].items()
                    if "withheld_s" in peer
                }
                # At 2P:2D every neighbour has a partner besides the target in every phase.
                self.assertEqual(bool(withheld), prefill != 2)


class NeighbourPauseTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, handler, peers, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        dialect = load_backend("sglang").dialect
        return NeighbourLoad(client, peers, "m", dialect, 4.0, WORKLOAD, 1.0, **kwargs)

    async def test_pause_drains_requests_and_resume_serves_the_new_role(self):
        inflight = {"now": 0}
        release = asyncio.Event()
        outputs = []

        async def handler(request):
            body = json.loads(request.content)
            outputs.append(body["max_tokens"])
            inflight["now"] += 1
            try:
                await release.wait()
            finally:
                inflight["now"] -= 1
            return httpx.Response(200, json={"usage": {"completion_tokens": body["max_tokens"]}})

        load = await self.load(handler, [("q", "http://q", Role.DECODE)])
        with (
            patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("p", 8))),
            patch.object(neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)),
        ):
            release.set()
            await load.start()
            release.clear()
            await asyncio.sleep(0.02)
            pause = asyncio.create_task(load.pause())
            await asyncio.sleep(0.02)
            self.assertFalse(pause.done())
            release.set()
            await pause
            self.assertEqual(inflight["now"], 0)
            sent = len(outputs)
            await asyncio.sleep(0.05)
            self.assertEqual(len(outputs), sent)
            await load.resume({"q": Role.PREFILL})
            await asyncio.sleep(0.03)
            evidence = await load.stop()
        self.assertEqual(outputs[-1], 1)
        self.assertGreater(evidence["paused_s"], 0.04)
        self.assertLess(evidence["elapsed_s"], 0.1)
        peer = evidence["peers"]["q"]
        self.assertEqual(peer["role"], "decode")
        self.assertGreater(peer["completed_by_role"]["prefill"], 0)
        self.assertEqual(
            evidence["completed_prefill"] + evidence["completed_decode"], peer["completed"]
        )

    async def test_a_neighbour_withheld_throughout_completes_nothing_without_failing(self):
        async def handler(request):
            body = json.loads(request.content)
            return httpx.Response(200, json={"usage": {"completion_tokens": body["max_tokens"]}})

        peers = [("p", "http://p", Role.PREFILL), ("d", "http://d", Role.DECODE)]
        load = await self.load(handler, peers)
        with (
            patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("p", 8))),
            patch.object(neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)),
        ):
            await load.start(withheld={"d"})
            await asyncio.sleep(0.03)
            evidence = await load.stop()
        self.assertEqual(evidence["peers"]["d"]["completed"], 0)
        self.assertGreater(evidence["peers"]["d"]["withheld_s"], 0)
        self.assertGreater(evidence["peers"]["p"]["completed"], 0)

    async def test_a_neighbour_active_in_some_interval_must_complete_requests(self):
        async def handler(request):
            if request.url.host == "d":
                await asyncio.Event().wait()
            return httpx.Response(200, json={"usage": {"completion_tokens": 1}})

        peers = [("p", "http://p", Role.PREFILL), ("d", "http://d", Role.PREFILL)]
        load = await self.load(handler, peers)
        with (
            patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("p", 8))),
            patch.object(neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)),
            self.assertRaisesRegex(RuntimeError, "d: completed 0 requests"),
        ):
            await load.start(withheld={"d"})
            await load.pause()
            await load.resume()
            await asyncio.sleep(0.03)
            await load.stop()

    async def test_peerless_fields_go_on_every_request(self):
        bodies = []

        async def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json={"usage": {"completion_tokens": body["max_tokens"]}})

        dialect = load_backend("sglang").dialect
        load = await self.load(
            handler, [("p", "http://p", Role.PREFILL)], peerless=dialect.peerless_fields
        )
        with (
            patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("p", 8))),
            patch.object(neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)),
        ):
            await load.start()
            await asyncio.sleep(0.02)
            evidence = await load.stop()
        self.assertTrue(evidence["peerless"])
        self.assertTrue(bodies)
        self.assertEqual({body["bootstrap_host"] for body in bodies}, {FAKE_BOOTSTRAP_HOST})
        self.assertEqual(len({body["bootstrap_room"] for body in bodies}), len(bodies))

    def test_only_sglang_serves_a_request_without_a_kv_peer(self):
        self.assertIsNone(load_backend("vllm").dialect.peerless_fields())
        fields = load_backend("sglang").dialect.peerless_fields()
        self.assertEqual(fields["bootstrap_host"], FAKE_BOOTSTRAP_HOST)
        self.assertIsInstance(fields["bootstrap_room"], int)


class PeerLegTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancelled_request_cancels_its_peer_leg(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def answer(request):
            if request.url.host == "peer":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return httpx.Response(200, stream=Body({"usage": {"completion_tokens": 1}}))

        backend = load_backend("sglang")
        pairing = PairedTransport(
            httpx.MockTransport(answer), backend.connector("mooncake"), backend.dialect
        )
        pairing.pair("http://own:8000", Role.PREFILL, "http://peer:8000", {"bootstrap": {}})
        async with httpx.AsyncClient(transport=pairing) as client:
            request = asyncio.create_task(
                client.post("http://own:8000/v1/completions", json={"prompt": "p", "max_tokens": 1})
            )
            async with asyncio.timeout(5):
                await started.wait()
            # The prefill answer is read; the request now waits for its decode leg.
            await asyncio.sleep(0.02)
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request
        self.assertTrue(cancelled.is_set())


class VllmColocatedTests(unittest.IsolatedAsyncioTestCase):
    async def test_vllm_neighbours_keep_their_roles_and_send_plain_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder), engines=("e0", "e1", "e3", "e4"))
            cfg.engines = [replace(spec, shared_device=GROUP) for spec in cfg.engines]
            cfg.profiles_path = Path(folder) / "colocated.json"
            seen = []

            async def answer(request):
                if request.url.path == "/health":
                    return httpx.Response(200)
                seen.append((request.url.path, json.loads(request.content or b"{}")))
                body = seen[-1][1]
                return httpx.Response(
                    200, json={"usage": {"completion_tokens": body["max_tokens"]}}
                )

            async def measured(*args, roles, **kwargs):
                self.assertIsNone(roles)
                await asyncio.sleep(0.03)
                return profile("e0")

            client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("p", 8))),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=16384)
                ),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(return_value=GenerationEvidence("sha256:" + "a" * 64, {}, 100.0)),
                ),
                patch.object(fleet_probe, "profile_instance", side_effect=measured),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await fleet_probe.run(cfg, {"e0"}, colocated_workload=WORKLOAD), 0)
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
        load = saved["engines"]["e0"]["colocated_load"]
        self.assertNotIn("paused_s", load)
        self.assertNotIn("peerless", load)
        self.assertEqual({row["role"] for row in load["peers"].values()}, {"prefill", "decode"})
        self.assertEqual({path for path, _ in seen}, {"/v1/completions"})
        self.assertFalse(any("bootstrap_host" in body for _, body in seen))


if __name__ == "__main__":
    unittest.main()
