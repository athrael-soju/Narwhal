import asyncio
import contextlib
import io
import json
import tempfile
import time
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
from tests.wire import routed

WORKLOAD = ColocatedWorkload(200, 200, 32, 32, 4)
GROUP = SharedDeviceAllocation("gpu-0", "uuid", 0.2, 0.2)
OTHER_GROUP = SharedDeviceAllocation("gpu-1", "uuid-1", 0.2, 0.2)


class Body(httpx.AsyncByteStream):
    """A response body read off the wire, so the client closes it after reading."""

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self.payload


def completed(body):
    return httpx.Response(200, json={"usage": {"completion_tokens": body["max_tokens"]}})


class SglangGroup:
    """SGLang engines on one GPU that refuse a switch or cache reset while busy.

    Like SGLang, an engine keeps serving a request after its client goes away.
    """

    def __init__(self, cfg):
        hosts = [httpx.URL(spec.url).host for spec in cfg.engines]
        # Each engine launches as prefill.
        self.roles = dict.fromkeys(hosts, Role.PREFILL)
        self.inflight = dict.fromkeys(hosts, 0)
        self.received = []
        self.busy_controls = []
        self.flush_timeouts = []
        self.serving = set()

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
            self.flush_timeouts.append(request.url.params.get("timeout"))
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

        async def serve():
            try:
                await asyncio.sleep(0.02 if body["prompt"] == "neighbour" else 0.002)
            finally:
                self.inflight[host] -= 1

        task = asyncio.ensure_future(serve())
        self.serving.add(task)
        task.add_done_callback(self.serving.discard)
        await asyncio.shield(task)
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


@contextlib.contextmanager
def colocated_run(handler, measured):
    """Serve every engine request from `handler` and measure each target with `measured`."""
    with (
        patch.object(httpx, "AsyncClient", routed(handler)),
        patch.object(httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handler)),
        patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
        patch.object(neighbours_probe, "engine_context_limit", AsyncMock(return_value=16384)),
        patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("neighbour", 32))),
        patch.object(
            fleet_probe,
            "read_generation",
            AsyncMock(return_value=GenerationEvidence("sha256:" + "a" * 64, {}, 100.0)),
        ),
        patch.object(fleet_probe, "profile_instance", side_effect=measured),
        redirect_stdout(io.StringIO()),
    ):
        yield


@contextlib.contextmanager
def sglang_engines(cfg, group, measured):
    """Serve `cfg`'s engines from `group` and measure each target with `measured`."""
    launches = {
        spec.iid: {"bootstrap": {"bootstrap_host": f"10.0.0.{index}", "bootstrap_port": 8998}}
        for index, spec in enumerate(cfg.engines)
    }
    with (
        colocated_run(group.handle, measured),
        patch.object(pairing_probe, "attested_launches", AsyncMock(return_value=launches)),
    ):
        yield


class ColocatedPairsTests(unittest.IsolatedAsyncioTestCase):
    async def profile_split(self, prefill):
        with tempfile.TemporaryDirectory() as folder:
            cfg = split_fleet(Path(folder), prefill)
            group = SglangGroup(cfg)
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
                    await asyncio.sleep(0.06)
                targets[-1] = (iid, targets[-1][1], len(group.received))
                return profile(iid)

            with sglang_engines(cfg, group, measured):
                code = await fleet_probe.run(cfg, None, colocated_workload=WORKLOAD)
            self.assertEqual(code, 0)
            evidence = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            stored = ProfileStore(cfg.profiles_path).all_profiles()
            return cfg, group, phases, targets, evidence, stored

    async def test_neighbours_serve_alone_at_each_split(self):
        for prefill in (1, 2, 3):
            with self.subTest(prefill=prefill):
                cfg, group, phases, targets, evidence, stored = await self.profile_split(prefill)
                split = (prefill, len(cfg.engines) - prefill)
                self.assertEqual([iid for iid, *_ in targets], [spec.iid for spec in cfg.engines])
                # Every phase keeps the split's role counts, with the target in the phase's role.
                self.assertEqual(len(phases), 3 * len(cfg.engines))
                for iid, wanted, live, counts in phases:
                    self.assertIs(live, wanted, iid)
                    self.assertEqual(counts, split, (iid, wanted))
                # No neighbour request reaches the target while it is measured.
                hosts = {spec.iid: httpx.URL(spec.url).host for spec in cfg.engines}
                for iid, start, end in targets:
                    on_target = [
                        row for row in group.received[start:end] if row["host"] == hosts[iid]
                    ]
                    self.assertEqual({row["prompt"] for row in on_target}, {"measured"}, iid)
                # The engines switch and reset their caches only with no request in flight, and
                # each reset waits for its engine to go idle.
                self.assertEqual(group.busy_controls, [])
                self.assertEqual(set(group.flush_timeouts), {str(cfg.health_timeout_s)})
                rows = [row for row in group.received if row["prompt"] == "neighbour"]
                self.assertTrue(rows)
                self.assertTrue(all(row["fake"] for row in rows))
                self.assertEqual(len({row["room"] for row in rows}), len(rows))
                for spec in cfg.engines:
                    row = evidence["engines"][spec.iid]
                    load = row["colocated_load"]
                    self.assertGreater(load["paused_s"], 0)
                    self.assertTrue(load["peerless"])
                    for peer in load["peers"].values():
                        self.assertGreater(peer["completed"], 0)
                    partner = next(peer for peer in cfg.engines if peer.iid == row["peer"])
                    self.assertIsNot(partner.role, spec.role)
                self.assertEqual(
                    {
                        (row.colocated_prefill_engines, row.colocated_decode_engines)
                        for row in stored
                    },
                    {split},
                )

    async def assert_refused_before_measuring(self, cfg, message):
        measured = AsyncMock()
        with (
            sglang_engines(cfg, SglangGroup(cfg), measured),
            self.assertRaisesRegex(ValueError, message),
        ):
            await fleet_probe.run(cfg, None, colocated_workload=WORKLOAD)
        measured.assert_not_called()
        self.assertFalse(cfg.profiles_path.exists())

    async def test_a_backend_without_peerless_requests_stops_before_measuring(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(SglangDialect, "peerless_fields", return_value=None),
        ):
            await self.assert_refused_before_measuring(
                split_fleet(Path(folder), 2), "without a KV peer"
            )

    async def test_every_target_needs_a_partner_before_any_engine_is_measured(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = split_fleet(Path(folder), 2)
            e0, e1, e2, e3 = cfg.engines
            # The second group holds only prefill engines, and its targets come last.
            cfg.engines = [
                replace(e0, shared_device=GROUP),
                replace(e2, shared_device=GROUP),
                replace(e1, shared_device=OTHER_GROUP),
                replace(e3, shared_device=OTHER_GROUP, role=Role.PREFILL),
            ]
            await self.assert_refused_before_measuring(
                cfg, f"{e1.iid}: no decode engine in shared_device group gpu-1"
            )


class NeighbourPauseTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, handler, peers, workload=WORKLOAD):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        self.enterContext(
            patch.object(neighbours_probe, "make_prompt", AsyncMock(return_value=("p", 8)))
        )
        self.enterContext(
            patch.object(neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096))
        )
        dialect = load_backend("sglang").dialect
        return NeighbourLoad(client, peers, "m", dialect, 4.0, workload, 1.0)

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
            return completed(body)

        load = await self.load(handler, [("q", "http://q", Role.DECODE)])
        release.set()
        await load.start()
        before = time.monotonic()
        release.clear()
        await asyncio.sleep(0.02)
        pause = asyncio.create_task(load.pause())
        await asyncio.sleep(0.02)
        self.assertFalse(pause.done())
        release.set()
        await pause
        self.assertEqual(inflight["now"], 0)
        sent = len(outputs)
        await asyncio.sleep(0.3)
        self.assertEqual(len(outputs), sent)
        await load.resume({"q": Role.PREFILL})
        await asyncio.sleep(0.03)
        after = time.monotonic()
        evidence = await load.stop()
        self.assertEqual(outputs[-1], 1)
        # The paused time is left out of the measured time.
        self.assertGreater(evidence["paused_s"], 0.3)
        self.assertLess(evidence["elapsed_s"], after - before - 0.25)
        peer = evidence["peers"]["q"]
        self.assertEqual(peer["role"], "decode")
        self.assertGreater(peer["completed_by_role"]["prefill"], 0)
        self.assertEqual(
            evidence["completed_prefill"] + evidence["completed_decode"], peer["completed"]
        )

    async def test_resumed_neighbours_send_at_their_own_offsets(self):
        sends = []

        async def handler(request):
            sends.append((request.url.host, time.monotonic()))
            return completed(json.loads(request.content))

        hosts = ("a", "b", "c", "d")
        load = await self.load(
            handler,
            [(host, f"http://{host}", Role.PREFILL) for host in hosts],
            ColocatedWorkload(10, 10, 32, 32, 4),
        )
        await load.start()
        await load.pause()
        await asyncio.sleep(0.25)
        resumed = time.monotonic()
        await load.resume()
        await asyncio.sleep(0.15)
        await load.stop()
        first = {}
        for host, at in sends:
            if at >= resumed:
                first.setdefault(host, at)
        self.assertEqual(set(first), set(hosts))
        # One period is 0.1 s, so four neighbours start 0.025 s apart instead of together.
        self.assertGreater(max(first.values()) - min(first.values()), 0.05)

    async def test_stop_with_drain_waits_for_requests_in_flight(self):
        release = asyncio.Event()
        finished = []

        async def handler(request):
            body = json.loads(request.content)
            if len(finished) >= 1:
                await release.wait()
            finished.append(body["max_tokens"])
            return completed(body)

        load = await self.load(handler, [("p", "http://p", Role.PREFILL)])
        await load.start()
        await asyncio.sleep(0.02)
        stop = asyncio.create_task(load.stop(drain=True))
        await asyncio.sleep(0.02)
        self.assertFalse(stop.done())
        release.set()
        await stop
        self.assertGreater(len(finished), 1)

    def test_only_sglang_serves_peerless_requests_and_waits_to_reset_its_cache(self):
        vllm, sglang = load_backend("vllm").dialect, load_backend("sglang").dialect
        self.assertIsNone(vllm.peerless_fields())
        self.assertEqual(vllm.cache_reset_params(5.0), {})
        fields = sglang.peerless_fields()
        self.assertEqual(fields["bootstrap_host"], FAKE_BOOTSTRAP_HOST)
        self.assertIsInstance(fields["bootstrap_room"], int)
        self.assertEqual(sglang.cache_reset_params(5.0), {"timeout": 5.0})


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

    async def test_a_cancel_while_the_response_closes_cancels_the_peer_leg(self):
        closing = asyncio.Event()

        class SlowClose(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b""

            async def aclose(self):
                closing.set()
                await asyncio.Event().wait()

        peer = asyncio.create_task(asyncio.Event().wait())
        joined = pairing_probe._Joined(SlowClose(), peer, 5.0)
        close = asyncio.create_task(joined.aclose())
        async with asyncio.timeout(5):
            await closing.wait()
        close.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await close
        self.assertTrue(peer.cancelled())


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
                seen.append((request.url.path, json.loads(request.content)))
                return completed(seen[-1][1])

            async def measured(*args, roles, **kwargs):
                self.assertIsNone(roles)
                await asyncio.sleep(0.03)
                return profile("e0")

            with colocated_run(answer, measured):
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
