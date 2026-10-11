from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from narwhal.profiling.store import ProfileStore
from narwhal.runtime import transfer_refresh
from narwhal.runtime.transfer_refresh import TransferRefresh
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile


def fleet(folder: str, stale: set[tuple[str, str]]) -> GlobalScheduler:
    profiles = ProfileStore(Path(folder) / "profiles.json")
    monitor = InstanceMonitor(profiles=profiles)
    for iid, role in (("p1", Role.PREFILL), ("p2", Role.PREFILL), ("d1", Role.DECODE)):
        profiles.put(profile(iid))
        monitor.add(Instance(iid, f"http://{iid}", role))
    profiles.put(profile("d2"))
    monitor.add(Instance("d2", "http://d2", Role.DECODE))
    scheduler = GlobalScheduler(monitor, profiles, SLO(2.0, 0.05), Thresholds())
    scheduler.availability.roles_bound = True
    scheduler.stale_handoff = lambda producer, consumer: (producer, consumer) in stale
    return scheduler


class StalePlacementTests(unittest.TestCase):
    def test_a_decode_leg_avoids_a_consumer_with_stale_state_for_its_producer(self):
        with tempfile.TemporaryDirectory() as folder:
            scheduler = fleet(folder, {("p1", "d1")})
        request = Request("r", 128, phase=Phase.DECODE, prefill_instance="p1")
        self.assertEqual(scheduler.decode_exclusion(request), {"p1", "d1"})
        self.assertEqual(scheduler.schedule(request).iid, "d2")
        self.assertEqual(scheduler.decode_exclusion(replace_producer(request, "p2")), {"p2"})

    def test_a_decode_leg_keeps_its_pool_when_every_consumer_is_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            scheduler = fleet(folder, {("p1", "d1"), ("p1", "d2")})
        request = Request("r", 128, phase=Phase.DECODE, prefill_instance="p1")
        self.assertEqual(scheduler.decode_exclusion(request), {"p1"})

    def test_a_prefill_leg_avoids_a_producer_no_consumer_can_take(self):
        with tempfile.TemporaryDirectory() as folder:
            scheduler = fleet(folder, {("p1", "d1"), ("p1", "d2")})
        for _ in range(3):
            self.assertEqual(scheduler.schedule(Request("r", 128)).iid, "p2")


def replace_producer(request: Request, producer: str) -> Request:
    return Request(request.rid, request.input_len, phase=request.phase, prefill_instance=producer)


class TransferRefreshTests(unittest.IsolatedAsyncioTestCase):
    def router(self, folder: str, stale: set[tuple[str, str]]) -> SimpleNamespace:
        scheduler = fleet(folder, stale)
        refreshed: list[str] = []

        async def refresh(iid: str) -> None:
            scheduler.availability.switching.discard(iid)
            stale.difference_update({pair for pair in stale if pair[1] == iid})
            refreshed.append(iid)

        return SimpleNamespace(
            scheduler=scheduler,
            monitor=scheduler.monitor,
            role_switch=object(),
            transfer_refresh=TransferRefresh(),
            stale_handoff=scheduler.stale_handoff,
            refresh_transfer_state=refresh,
            refreshed=refreshed,
        )

    async def settle(self, router: SimpleNamespace) -> None:
        await asyncio.gather(*router.transfer_refresh.tasks)

    async def test_an_idle_stale_consumer_refreshes(self):
        with tempfile.TemporaryDirectory() as folder:
            router = self.router(folder, {("p1", "d1")})
        self.assertEqual(transfer_refresh.stale_consumers(router), {"d1": ["p1"]})
        transfer_refresh.refresh_consumers(router)
        await self.settle(router)
        self.assertEqual(router.refreshed, ["d1"])
        self.assertEqual(transfer_refresh.stale_consumers(router), {})

    async def test_a_busy_stale_consumer_waits_while_its_producer_has_another(self):
        with tempfile.TemporaryDirectory() as folder:
            router = self.router(folder, {("p1", "d1")})
        router.monitor.instances["d1"].decode["r"] = Request("r", 128, phase=Phase.DECODE)
        transfer_refresh.refresh_consumers(router)
        await self.settle(router)
        self.assertEqual(router.refreshed, [])
        self.assertIsNone(router.transfer_refresh.held)

    async def test_a_starved_producer_holds_the_least_loaded_consumer_until_it_drains(self):
        with tempfile.TemporaryDirectory() as folder:
            router = self.router(folder, {("p1", "d1"), ("p1", "d2")})
        decodes = {iid: router.monitor.instances[iid].decode for iid in ("d1", "d2")}
        decodes["d1"].update(r1=Request("r1", 128), r2=Request("r2", 128))
        decodes["d2"]["r3"] = Request("r3", 128)
        transfer_refresh.refresh_consumers(router)
        self.assertEqual(router.transfer_refresh.held, "d2")
        self.assertIn("d2", router.scheduler.availability.switching)
        transfer_refresh.refresh_consumers(router)
        self.assertEqual(router.refreshed, [])
        decodes["d2"].clear()
        transfer_refresh.refresh_consumers(router)
        await self.settle(router)
        self.assertEqual(router.refreshed, ["d2"])
        self.assertIsNone(router.transfer_refresh.held)
        self.assertNotIn("d2", router.scheduler.availability.switching)

    async def test_startup_refreshes_every_idle_stale_consumer(self):
        with tempfile.TemporaryDirectory() as folder:
            router = self.router(folder, {("p1", "d1"), ("p2", "d2")})
        await transfer_refresh.refresh_idle_consumers(router)
        self.assertEqual(router.refreshed, ["d1", "d2"])
