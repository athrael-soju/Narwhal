import asyncio
import contextlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load as load_backend
from narwhal.backends.llamacpp.connector import SlotFileConnector
from narwhal.config import EngineSpec
from narwhal.config.model import SharedDeviceAllocation
from narwhal.engines.client import EngineError
from narwhal.engines.connector import PrefillResult
from narwhal.observability.metrics.render import render
from narwhal.runtime import state as handoff_state
from narwhal.runtime.lifecycle.records import DrainRecord
from narwhal.runtime.monitoring import monitor_once, sweep_liveness
from narwhal.runtime.release import PeerRelease
from narwhal.runtime.role_switch import EngineHeldRoles
from narwhal.scheduling.control import Flip
from narwhal.scheduling.monitor import Instance
from narwhal.serving import app as serving_app
from narwhal.serving.router.membership import Membership
from narwhal.types import Phase, Request, Role
from tests.fixtures import contract_free_router, fleet, profile


def spec(iid: str) -> EngineSpec:
    return EngineSpec(iid, f"http://{iid}:8000")


class MembershipTests(unittest.TestCase):
    def test_changes_return_a_successor_one_generation_higher(self) -> None:
        start = Membership.initial([spec("e0"), spec("e1")])
        grown = start.joined(spec("e2"))
        shrunk = grown.left("e0")
        self.assertEqual((start.generation, grown.generation, shrunk.generation), (0, 1, 2))
        self.assertEqual(start.ids, ["e0", "e1"])
        self.assertEqual(grown.ids, ["e0", "e1", "e2"])
        self.assertEqual(shrunk.ids, ["e1", "e2"])
        self.assertEqual(shrunk.spec("e2").url, "http://e2:8000")
        self.assertNotIn("e0", shrunk)
        self.assertEqual(len(shrunk), 2)

    def test_a_snapshot_cannot_change_in_place(self) -> None:
        current = Membership.initial([spec("e0")])
        with self.assertRaises(FrozenInstanceError):
            current.generation = 5  # type: ignore[misc]
        current.by_id().pop("e0")
        self.assertIn("e0", current)
        with self.assertRaises(FrozenInstanceError):
            current.spec("e0").pin = True  # type: ignore[misc]

    def test_duplicates_and_unknown_engines_are_refused(self) -> None:
        current = Membership.initial([spec("e0")])
        with self.assertRaisesRegex(ValueError, "already a member"):
            current.joined(spec("e0"))
        with self.assertRaisesRegex(ValueError, "not a member"):
            current.left("e9")
        with self.assertRaisesRegex(ValueError, "duplicate engine id"):
            Membership.initial([spec("e0"), spec("e0")])
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            Membership(-1, ())


class RouterMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_monitor_keys_follow_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            self.assertEqual(router.membership.generation, 0)
            self.assertEqual(router.membership.ids, [e.iid for e in router.cfg.engines])
            self.assertEqual(router.membership.ids, list(router.monitor.instances))
            self.assertEqual(set(router.residency.views), set(router.membership.ids))

    async def test_a_joined_engine_enters_every_table_in_its_spec_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            router.profiles.put(profile("e6"))
            joined = EngineSpec("e6", "http://e6:8000", role=Role.PREFILL, pin=True)
            router.apply_membership(router.membership.joined(joined))
            self.assertEqual(router.membership.generation, 1)
            self.assertEqual(router.membership.ids, list(router.monitor.instances))
            self.assertIs(router.monitor.instances["e6"].role, Role.PREFILL)
            self.assertIn("e6", router.residency.views)
            self.assertIn("e6", router.scheduler.pinned)
            self.assertIn("e6", {inst.iid for inst in router.scheduler.live_instances()})
            self.assertEqual(router.monitor.requests_sent["e6"], 0)
            # The joined engine takes placement like any other member.
            request = Request("r1", 64, arrived_at=router._clock())
            chosen = router.scheduler.schedule(request, exclude={"e0", "e1", "e2"})
            self.assertEqual(chosen.iid, "e6")

    async def test_a_removed_engine_leaves_no_entry_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            now = router._clock()
            scheduler = router.scheduler
            scheduler.availability.failures["timeout"]["e4"] = 2
            scheduler.availability.liveness_misses["e4"] = 1
            scheduler.eject("e4", "liveness")
            scheduler.availability.switching.add("e4")
            scheduler.verifying.add(("e4", "verify_health"))
            scheduler.inference_suspects.update({"e4", "e5"})
            scheduler.availability.quarantined["e4"] = now + 60
            scheduler.availability.hold_since["e4"] = now
            scheduler.availability.hold_starts[("e4", "quarantine")] = 1
            scheduler.availability.hold_ends[("e4", "quarantine", "expired")] = 1
            scheduler.availability.readmissions[("e4", "health")] = 1
            scheduler.switch_holds["e4"] = now
            scheduler.health.note("e4", 1.0)
            scheduler.roles._last_flip["e4"] = now
            scheduler.roles._last_flip_role["e4"] = Role.DECODE
            scheduler.roles.flips.append(Flip(now, "e4", Role.DECODE, "test", 0, 0))
            scheduler.roles.held_donor = "e4"
            router.verifier.sources["e4"] = {"e0"}
            router.verifier.sources["e5"] = {"e4"}
            router.verifier.verified_at["e4"] = now
            router.verifier.probes[("e4", "verify_health", "passed")] = 1
            router.sizer.backoff["e4"] = (1, now)
            router.peer_release.track({"e4": "ejected"})
            router.peer_release.missed.add("e4")
            router.sequence_limits["e4"] = 8
            router.kv_leases["e4"] = 30
            router.launches["e4"] = {"role": "decode"}
            router.lifecycle.records["e4"] = DrainRecord(
                iid="e4", state="drained", requested_at=1.0, deadline_at=2.0
            )
            router.lifecycle.process_starts["e4"] = 100.0
            router.transfer_refresh.held = "e4"

            router.apply_membership(router.membership.left("e4"))

            self.assertNotIn("e4", router.membership)
            self.assertEqual(router.membership.ids, list(router.monitor.instances))
            for table in (
                scheduler.ejected,
                scheduler.liveness_misses,
                scheduler.availability.failures["timeout"],
                scheduler.switch_holds,
                scheduler.availability.quarantined,
                scheduler.availability.hold_since,
                scheduler.roles._last_flip,
                scheduler.roles._last_flip_role,
                router.verifier.sources,
                router.verifier.verified_at,
                router.sizer.backoff,
                router.peer_release.snapshot(),
                router.sequence_limits,
                router.kv_leases,
                router.launches,
                router.lifecycle.records,
                router.lifecycle.process_starts,
                router.residency.views,
                router.monitor.requests_sent,
                router.scheduler.health.window_stats(),
            ):
                self.assertNotIn("e4", table)
            self.assertNotIn("e4", scheduler.availability.switching)
            self.assertNotIn("e4", scheduler.inference_suspects)
            for counts in (
                scheduler.availability.hold_starts,
                scheduler.availability.hold_ends,
                scheduler.availability.readmissions,
            ):
                self.assertFalse(any(key[0] == "e4" for key in counts))
            self.assertNotIn("e4", router.peer_release.missed)
            self.assertFalse(any(iid == "e4" for iid, _ in scheduler.verifying))
            self.assertFalse(any(flip.iid == "e4" for flip in scheduler.roles.flips))
            self.assertIsNone(scheduler.roles.held_donor)
            self.assertIsNone(router.transfer_refresh.held)
            self.assertEqual(router.verifier.sources["e5"], {""})
            self.assertEqual(router.verifier.probe_counts(), [])
            self.assertFalse(any(row[0] == "e4" for row in scheduler.availability.ejections))
            # Every view of the router renders and the handoff it writes validates.
            state = router.state()
            self.assertNotIn("e4", state["resident"])
            self.assertNotIn("e4", state["breaker"]["failures"])
            text = render(
                state,
                router.ttft,
                router.tpot,
                router.seat,
                router.queue_wait,
                router.monitor.requests_sent,
            )
            self.assertNotIn('iid="e4"', text)
            handoff_state.write(router.cfg.state_path, handoff_state.snapshot(router))
            self.assertNotIn("e4", handoff_state.load(router.cfg.state_path)["engines"])
            self.assertNotIn("e4", router.lifecycle.view()["engines"])

    async def test_resident_work_on_a_removed_engine_finishes_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            monitor = router.monitor
            prefill = Request("p", 64)
            monitor.dispatched("e0", prefill)
            decode = Request("d", 64, phase=Phase.DECODE, prefill_instance="e0")
            monitor.dispatched("e3", decode)
            monitor.first_token("e3", "d")
            router.apply_membership(router.membership.left("e0").left("e3"))
            monitor.first_token("e0", "p")
            monitor.output_token("e3", "d")
            monitor.output_token("e3", "d")
            monitor.output_ended("d")
            monitor.finished("e3", "d")
            monitor.finished("e0", "p")
            monitor.roll_interval()
            self.assertNotIn("d", monitor._last_token)
            self.assertNotIn("p", monitor._prefilled_at)
            # Late evidence about a removed engine changes nothing.
            router.verifier.leg_failed("e3", httpx.ConnectError("gone"), prefill_iid="e0")
            router.verifier.leg_failed(
                "e4", httpx.ReadTimeout("t"), prefill_iid="e0", decode_leg=True
            )
            self.assertFalse(router.scheduler.eject("e3", "liveness"))
            router.scheduler.record_answer("e3", "health")
            self.assertNotIn(("e3", "health"), router.scheduler.availability.readmissions)
            self.assertIsNone(router.scheduler.record_failure("e3"))
            for counts in router.scheduler.availability.failures.values():
                self.assertNotIn("e3", counts)
            self.assertNotIn("e3", router.scheduler.ejected)
            self.assertEqual(router.verifier.sources.get("e4"), {""})
            self.assertFalse(router.stale_handoff("e0", "e4"))
            router.note_handoff("e0", "e4")

    async def test_changes_that_do_not_follow_the_rules_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            current = router.membership
            with self.assertRaisesRegex(ValueError, "does not follow"):
                router.apply_membership(Membership(0, current.specs))
            moved = replace(current.spec("e0"), url="http://elsewhere:8000")
            with self.assertRaisesRegex(ValueError, "changed in place: e0"):
                router.apply_membership(
                    Membership(1, (moved, *(s for s in current if s.iid != "e0")))
                )
            shared = EngineSpec(
                "e6",
                "http://e6:8000",
                shared_device=SharedDeviceAllocation("g", "GPU-x", 0.5, 0.4),
            )
            with self.assertRaisesRegex(ValueError, "shared-device membership"):
                router.apply_membership(current.joined(shared))
            self.assertIs(router.membership, current)
            self.assertEqual(router.membership.ids, list(router.monitor.instances))

    async def test_monitoring_passes_run_cleanly_across_a_join_and_a_leave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            router.engines.healthy = AsyncMock(return_value=True)  # type: ignore[method-assign]
            router.profiles.put(profile("e6"))
            monitor = router.monitor
            resident = Request("r", 64, phase=Phase.DECODE, prefill_instance="e0")
            monitor.dispatched("e5", resident)
            await monitor_once(router)
            router.apply_membership(
                router.membership.joined(EngineSpec("e6", "http://e6:8000", role=Role.DECODE))
            )
            await monitor_once(router)
            router.scheduler.eject("e4", "liveness")
            router.apply_membership(router.membership.left("e4").left("e5"))
            await monitor_once(router)
            monitor.finished("e5", "r")
            await monitor_once(router)
            self.assertEqual(router.monitoring.snapshot()["core_failures"], 0)
            saved = handoff_state.load(router.cfg.state_path)
            self.assertEqual(saved["engines"], sorted(router.membership.ids))
            self.assertEqual(sorted(router.state()["resident"]), sorted(router.membership.ids))

    async def test_an_engine_that_leaves_during_a_probe_leaves_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            gone = router.membership.spec("e4").url

            async def leaves(url, *args, **kwargs):
                if url == gone and "e4" in router.membership:
                    router.apply_membership(router.membership.left("e4"))
                return url != gone

            router.engines.healthy = AsyncMock(side_effect=leaves)  # type: ignore[method-assign]
            self.assertEqual(await sweep_liveness(router), [])
            self.assertNotIn("e4", router.membership)
            self.assertNotIn("e4", router.scheduler.liveness_misses)
            e4 = EngineSpec("e4", gone, role=Role.DECODE)
            router.apply_membership(router.membership.joined(e4))
            router.scheduler.verifying.add(("e4", "verify_health"))
            await router.verifier._verify_suspect("e4", "verify_health")
            self.assertNotIn("e4", router.membership)
            router.apply_membership(router.membership.joined(e4))
            self.assertNotIn("e4", router.verifier.verified_at)

    async def test_late_completions_do_not_restore_a_removed_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            router.apply_membership(router.membership.left("e5"))
            router.attested("e5", {})
            self.assertNotIn("e5", router.launches)
            router.sizer._count_failed(
                Instance("e5", "http://gone", Role.DECODE),
                EngineError("tokenize", "http://gone", 503, "busy"),
            )
            self.assertNotIn("e5", router.sizer.backoff)
            self.assertEqual(await router.resume_role("e5"), "engine left the fleet")
            self.assertEqual(await router.refresh_transfer_state("e5"), "engine left the fleet")

    async def test_a_rejoined_producer_reads_stale_to_consumers_that_held_it(self) -> None:
        class Switcher(EngineHeldRoles):
            async def change_role(self, client, base, role, launch):
                return False

        switcher = Switcher()
        switcher._received["http://c"] = {}
        switcher.record_handoff("http://p", "http://c")
        switcher._received["http://fresh"] = {}
        self.assertFalse(switcher.stale_handoff("http://p", "http://c"))
        switcher.forget("http://p")
        # The rejoined producer serves prefill already, so no switch counts a new endpoint.
        await switcher.switch(None, "http://p", Role.PREFILL)
        self.assertTrue(switcher.stale_handoff("http://p", "http://c"))
        self.assertFalse(switcher.stale_handoff("http://p", "http://fresh"))

    async def test_a_count_whose_engines_leave_fails_with_the_engine_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            router.cfg.tokenize = True
            router.cfg.serving = replace(router.cfg.serving, max_attempts=6)
            calls = []

            async def tokenize(url, *args, **kwargs):
                calls.append(url)
                if len(calls) == 1:
                    keep = next(s for s in router.membership if s.url == url)
                    others = [s.iid for s in router.membership if s.iid != keep.iid]
                    shrunk = router.membership
                    for iid in others[:-1]:
                        shrunk = shrunk.left(iid)
                    router.apply_membership(shrunk)
                raise EngineError("tokenize", url, 503, "busy")

            router.engines.tokenize = tokenize  # type: ignore[method-assign]
            with self.assertRaises(EngineError):
                await router.sizer.size({"prompt": "hello"})
            # The count tried the one engine left after the first failure, then stopped.
            self.assertEqual(len(calls), 2)

    async def test_late_release_and_slot_cleanup_after_a_departure(self) -> None:
        now = [0.0]
        release = PeerRelease(lambda: now[0], load_backend("vllm").fabric)

        async def round_() -> dict[str, str]:
            raise asyncio.CancelledError

        task = asyncio.ensure_future(round_())
        release.tasks.add(task)
        release.forget("e4")
        with contextlib.suppress(asyncio.CancelledError):
            await task
        release.finish(task, retry=False)
        self.assertEqual(release._departed, set())

        slots = SlotFileConnector()
        slots._free["http://e4"] = [1]
        slots._held["http://e4"] = {7}
        slots.forget("http://e4")
        self.assertIn("http://e4", slots._free)
        slots.discard(PrefillResult("slot-file", "http://e4", "", None, json.dumps({"file": 7})))
        self.assertNotIn("http://e4", slots._free)
        self.assertNotIn("http://e4", slots._held)

    async def test_a_cancelled_wait_on_a_departed_engine_stays_cancelled(self) -> None:
        slots = SlotFileConnector()
        slots._free["http://e4"] = []
        waiter = asyncio.ensure_future(slots.claim(None, "http://e4", "decode"))
        await asyncio.sleep(0)
        slots.forget("http://e4")
        slots.release("http://e4", "decode", {"id_slot": 0})
        self.assertNotIn("http://e4", slots._free)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertNotIn("http://e4", slots._free)

    async def test_the_monitor_follows_snapshot_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            e1 = router.membership.spec("e1")
            router.apply_membership(router.membership.left("e1"))
            router.apply_membership(router.membership.joined(e1))
            self.assertEqual(router.membership.ids[-1], "e1")
            self.assertEqual(router.membership.ids, list(router.monitor.instances))

    async def test_profile_set_check_follows_a_replaced_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            keep = tuple(spec for spec in router.membership if spec.iid in ("e0", "e3"))
            router.membership = Membership(1, keep)
            self.assertEqual(router.profile_set_diff(), ([], ["e1", "e2", "e4", "e5"]))


class MembershipRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_wave_defaults_and_health_follow_the_snapshot_in_sorted_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = fleet(root, engines=("e3", "e0"))
            cfg.state_path = root / "handoff.json"
            app = serving_app.create_app(cfg)
            router = app.state.router
            self.addAsyncCleanup(router.engines.aclose)
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://router"
            )
            self.addAsyncCleanup(client.aclose)
            self.assertEqual(router.membership.ids, ["e3", "e0"])
            response = await client.get("/health")
            self.assertEqual(response.json()["instances"], 2)
            with patch.object(
                serving_app,
                "capture_process_identities",
                new=AsyncMock(return_value=({"e0": 100, "e3": 100}, {})),
            ) as capture:
                response = await client.post("/narwhal/lifecycle/drain", json={"wave": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(capture.call_args.args[1], ["e0", "e3"])
            started = [
                event["iid"]
                for event in router.lifecycle.events
                if event["action"] == "drain_started"
            ]
            self.assertEqual(started, ["e0", "e3"])


if __name__ == "__main__":
    unittest.main()
