"""Check the Kubernetes router lease against the stub API server, each router on its own clocks.

Every router here reads its own monotonic and wall clocks, offset from each other and from the
API server's, so a test that passes cannot depend on two clocks agreeing. `fenced` checks the
property that matters at every step: no two routers are valid at once.
"""

import asyncio
import contextlib
import logging
import random
import unittest
from collections.abc import Callable
from unittest.mock import patch

import httpx

from narwhal.runtime import kubernetes_api as k
from narwhal.runtime import state
from narwhal.runtime.kubernetes_lease import KubernetesLease, check_settings
from narwhal.runtime.lease import LeaseError
from narwhal.runtime.standby import standby_loop
from narwhal.serving.app import create_app
from tests.deployment.kubernetes_cluster import LEASE, Clock, ObjectStore
from tests.runtime.test_standby_shutdown import FleetCase

NS = "fleet-a"
NAME = "narwhal-router"
TTL = 5
MARGIN = 1.0
SERVER = "https://10.96.0.1:443"


class Time:
    """Real time as the test advances it. Each router reads it through its own offsets."""

    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Router:
    """One router's lease, client and clocks, and the faults its requests meet."""

    def __init__(self, store: ObjectStore, time: Time, holder: str, offset: float) -> None:
        self.store = store
        self.down = False
        # Called once before the next write reaches the API server, and once after it landed.
        self.before_write: Callable[[], None] | None = None
        self.after_write: Callable[[], None] | None = None
        # Returns "lost" to drop a request, "unanswered" to apply it and lose the reply.
        self.fault: Callable[[httpx.Request], str] = lambda request: ""
        self.client = k.ApiClient(
            k.Connection(SERVER, NS),
            field_manager="narwhal-router",
            backoff=k.Backoff(base_s=0.1, cap_s=0.5, attempts=2, jitter=0.0),
            transport=httpx.MockTransport(self.handle),
            sleep=lambda seconds: None,
        )
        self.lease = KubernetesLease(
            self.client,
            NAME,
            holder,
            TTL,
            safety_margin_s=MARGIN,
            # Clocks that never agree: neither the wall clocks nor the monotonic ones.
            wall_clock=lambda: 1.9e9 + 7 * offset + time.now,
            monotonic_clock=lambda: offset + time.now,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("API server unreachable", request=request)
        if request.method in ("PUT", "POST") and self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        fault = self.fault(request)
        if fault == "lost":
            raise httpx.ConnectError("connection refused", request=request)
        response = self.store.handle(request)
        if request.method in ("PUT", "POST") and self.after_write is not None:
            hook, self.after_write = self.after_write, None
            hook()
        if fault == "unanswered":
            return httpx.Response(504, json={"kind": "Status", "code": 504, "reason": "Timeout"})
        return response


def quiet(case: unittest.TestCase) -> None:
    """Hide the retry and renewal warnings the injected faults are meant to cause."""
    for name in ("narwhal.kubernetes_api", "narwhal.kubernetes_lease"):
        case.enterContext(patch.object(logging.getLogger(name), "disabled", True))


class LeaseCase(unittest.TestCase):
    def setUp(self) -> None:
        quiet(self)
        self.time = Time()
        self.store = ObjectStore(Clock(1.7e9))
        self.store.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}})
        self.routers: list[Router] = []

    def router(self, holder: str, offset: float) -> Router:
        router = Router(self.store, self.time, holder, offset)
        self.routers.append(router)
        return router

    def fenced(self) -> None:
        valid = [router.lease.holder for router in self.routers if router.lease.valid()]
        self.assertLessEqual(len(valid), 1, f"{valid} are valid at once at {self.time.now}")

    def step(self, seconds: float, *actions: Callable[[], object]) -> None:
        """Advance in 50 ms ticks, checking the fence at each, then run `actions`."""
        ticks = round(seconds / 0.05)
        for _ in range(ticks):
            self.time.advance(0.05)
            self.fenced()
        for action in actions:
            with contextlib.suppress(LeaseError):
                action()
            self.fenced()

    def live(self, holder: Router, renew_every: float = 1.0, *, for_s: float) -> float:
        """Renew `holder` every `renew_every` seconds for `for_s`, returning the last send."""
        sent = self.time.now
        elapsed = 0.0
        while elapsed < for_s:
            self.step(renew_every)
            elapsed += renew_every
            sent = self.time.now
            self.assertTrue(holder.lease.renew())
        return sent

    def holding(self, router: Router) -> None:
        """Make `router` the valid holder, past its first claim's wait."""
        self.assertTrue(router.lease.claim())
        self.live(router, for_s=TTL + 1)
        self.assertTrue(router.lease.valid())

    def stored(self) -> dict:
        return self.store.get(LEASE, NS, NAME)


class ClaimTests(LeaseCase):
    def test_a_created_lease_holds_at_once_and_serves_one_duration_later(self):
        a = self.router("a", 3.0)
        self.assertTrue(a.lease.claim())
        self.assertEqual(a.lease.epoch, 1)
        spec = self.stored()["spec"]
        self.assertEqual(spec["holderIdentity"], "a")
        self.assertEqual((spec["leaseTransitions"], spec["leaseDurationSeconds"]), (1, TTL))
        # A deleted Lease's holder could still be inside its renew deadline.
        self.assertFalse(a.lease.valid())
        self.assertAlmostEqual(a.lease.serving_delay(), TTL)
        self.live(a, for_s=TTL - 1)
        self.step(0.9, a.lease.renew)
        self.assertFalse(a.lease.valid())
        self.step(0.2, a.lease.renew)
        self.assertTrue(a.lease.valid())
        self.assertEqual(a.lease.serving_delay(), 0.0)

    def test_a_held_lease_reads_with_its_holder_epoch_and_this_routers_expiry(self):
        a, b = self.router("a", 0.0), self.router("b", 1e6)
        self.holding(a)
        record = b.lease.read()
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual((record.holder, record.epoch), ("a", 1))
        # B's wall clock, which A's never matches: the record is B's estimate.
        self.assertAlmostEqual(record.expires_at - record.updated_at, TTL)
        self.assertGreater(record.updated_at, 1.9e9 + 7e6)

    def test_an_absent_lease_reads_as_none(self):
        self.assertIsNone(self.router("a", 0.0).lease.read())

    def test_two_first_claims_elect_one_holder(self):
        a, b = self.router("a", 0.0), self.router("b", 50.0)
        # B reads the Lease as absent, then A creates it before B's create lands.
        b.before_write = lambda: self.assertTrue(a.lease.claim())
        self.assertFalse(b.lease.claim())
        self.assertFalse(b.lease.owned)
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "a")


class ExpiryTests(LeaseCase):
    def test_a_standby_takes_over_only_after_its_own_clock_sees_the_lease_unchanged(self):
        a, b = self.router("a", 0.0), self.router("b", 1234.5)
        self.holding(a)
        # B shadows A, reading the Lease every 0.25 s while A renews every second.
        last = self.time.now
        for _ in range(8):
            self.step(0.25, b.lease.read)
            if self.time.now - last >= 1.0:
                self.assertTrue(a.lease.renew())
                last = self.time.now
        # A stops renewing. B tries to claim every 0.25 s.
        fenced_at = taken_at = None
        while taken_at is None:
            self.step(0.25)
            if fenced_at is None and not a.lease.valid():
                fenced_at = self.time.now
            if b.lease.claim():
                taken_at = self.time.now
        assert fenced_at is not None
        self.assertAlmostEqual(fenced_at, last + TTL - MARGIN, delta=0.25)
        self.assertGreaterEqual(taken_at - fenced_at, MARGIN - 0.25)
        # The standby takes over within the duration plus one probe and one read interval.
        self.assertGreaterEqual(taken_at, last + TTL)
        self.assertLessEqual(taken_at, last + TTL + 0.5)
        self.assertEqual(b.lease.epoch, 2)
        self.assertTrue(b.lease.valid())
        self.assertEqual(b.lease.serving_delay(), 0.0)
        # A's next renewal finds the deadline gone and does not write.
        self.assertFalse(a.lease.renew())
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "b")

    def test_each_renewal_restarts_the_standbys_clock(self):
        a, b = self.router("a", 0.0), self.router("b", 99.0)
        self.holding(a)
        b.lease.read()
        for _ in range(3 * TTL):
            self.step(1.0)
            self.assertTrue(a.lease.renew())
            self.assertFalse(b.lease.claim())
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "a")

    def test_a_standby_that_first_reads_a_dead_holders_lease_waits_a_whole_duration(self):
        a = self.router("a", 0.0)
        self.holding(a)
        # A dies. Long after, a new router reads the Lease for the first time.
        self.step(60.0)
        b = self.router("b", 7.0)
        self.assertFalse(b.lease.claim())
        self.step(TTL - 0.25, b.lease.claim)
        self.assertFalse(b.lease.owned)
        self.step(0.3)
        self.assertTrue(b.lease.claim())
        self.assertTrue(b.lease.valid())

    def test_a_longer_duration_written_by_the_holder_is_respected(self):
        a, b = self.router("a", 0.0), self.router("b", 3.0)
        self.holding(a)
        lease = self.stored()
        lease["spec"]["leaseDurationSeconds"] = 3 * TTL
        self.store.replace(lease)
        b.lease.read()
        self.step(2 * TTL)
        self.assertFalse(b.lease.claim())
        self.step(TTL + 0.1)
        self.assertTrue(b.lease.claim())


class ReleaseTests(LeaseCase):
    def test_a_release_the_standby_saw_is_taken_at_once(self):
        a, b = self.router("a", 0.0), self.router("b", 11.0)
        self.holding(a)
        b.lease.read()
        a.lease.release()
        self.assertFalse(a.lease.valid())
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "")
        self.assertEqual(self.stored()["spec"]["leaseTransitions"], 1)
        self.assertTrue(b.lease.claim())
        self.assertTrue(b.lease.valid())
        self.assertEqual(b.lease.epoch, 2)

    def test_a_free_lease_this_router_never_saw_held_serves_one_duration_later(self):
        a = self.router("a", 0.0)
        self.holding(a)
        a.lease.release()
        b = self.router("b", 5.0)
        self.assertTrue(b.lease.claim())
        self.assertEqual(b.lease.epoch, 2)
        self.assertFalse(b.lease.valid())
        self.assertAlmostEqual(b.lease.serving_delay(), TTL)
        self.live(b, for_s=TTL)
        self.assertTrue(b.lease.valid())

    def test_a_lease_freed_by_an_edit_under_a_live_holder_never_serves_two(self):
        a, b = self.router("a", 0.0), self.router("b", 17.0)
        self.holding(a)
        b.lease.read()
        # Someone frees the Lease with a new epoch while A holds it and cannot reach it.
        lease = self.stored()
        lease["spec"].update(holderIdentity="", leaseTransitions=7)
        self.store.replace(lease)
        a.down = True
        self.assertTrue(b.lease.claim())
        for _ in range(int(2 * TTL / 0.25)):
            self.step(0.25, a.lease.renew, b.lease.renew)
        self.assertFalse(a.lease.valid())
        self.assertTrue(b.lease.valid())


class ConflictTests(LeaseCase):
    def test_a_takeover_that_meets_a_landed_renewal_loses_and_waits_again(self):
        a, b = self.router("a", 0.0), self.router("b", 31.0)
        self.holding(a)
        b.lease.read()
        self.step(TTL + 0.1)
        # B read a version it has seen for a whole duration, but A's slow renewal lands first.
        renewed = self.stored()
        renewed["spec"]["renewTime"] = "2030-01-01T00:00:00.000000Z"
        b.before_write = lambda: self.store.replace(renewed)
        self.assertFalse(b.lease.claim())
        self.assertFalse(b.lease.owned)
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "a")
        # The conflict's re-read restarts B's clock on the new version.
        self.step(TTL - 0.5)
        self.assertFalse(b.lease.claim())

    def test_a_renewal_that_meets_another_holder_fences_the_router(self):
        a, b = self.router("a", 0.0), self.router("b", 2.0)
        self.holding(a)
        # A sends a renewal before its deadline; it reaches the API server only after B took
        # the Lease over, so A's update conflicts.
        b.lease.read()

        def late() -> None:
            self.time.advance(TTL + 0.1)
            self.assertTrue(b.lease.claim())

        a.before_write = late
        self.assertFalse(a.lease.renew())
        self.assertFalse(a.lease.owned)
        self.assertFalse(a.lease.valid())
        self.assertTrue(b.lease.valid())

    def test_a_renewal_that_meets_a_metadata_change_retries_and_keeps_the_lease(self):
        a = self.router("a", 0.0)
        self.holding(a)
        labelled = self.stored()
        labelled["metadata"].setdefault("labels", {})["team"] = "serving"
        a.before_write = lambda: self.store.replace(labelled)
        self.assertTrue(a.lease.renew())
        self.assertTrue(a.lease.valid())
        self.assertEqual(self.stored()["metadata"]["labels"], {"team": "serving"})

    def test_a_deleted_lease_fences_its_holder_and_its_creator_waits_out_the_holder(self):
        a, b = self.router("a", 0.0), self.router("b", 4.0)
        self.holding(a)
        self.store.delete(LEASE, NS, NAME)
        a.down = True
        self.assertTrue(b.lease.claim())
        self.assertEqual(b.lease.epoch, 1)
        for _ in range(int(2 * TTL / 0.25)):
            self.step(0.25, a.lease.renew, b.lease.renew)
        self.assertTrue(b.lease.valid())
        a.down = False
        self.assertFalse(a.lease.renew())


class SlowReplyTests(LeaseCase):
    def test_a_slow_renewal_counts_its_deadline_from_when_it_was_sent(self):
        a = self.router("a", 0.0)
        self.holding(a)
        sent = self.time.now
        # The renewal lands at once, but its reply takes three seconds to arrive.
        a.after_write = lambda: self.time.advance(3.0)
        self.assertTrue(a.lease.renew())
        self.step(TTL - MARGIN - 3.0 - 0.1)
        self.assertTrue(a.lease.valid())
        self.step(0.2)
        self.assertFalse(a.lease.valid())
        self.assertAlmostEqual(self.time.now - sent, TTL - MARGIN + 0.1)

    def test_a_slow_takeover_counts_its_deadline_from_when_it_was_sent(self):
        a, b = self.router("a", 0.0), self.router("b", 3.0)
        self.holding(a)
        b.lease.read()
        self.step(TTL + 0.1)
        sent = self.time.now
        b.after_write = lambda: self.time.advance(2.0)
        self.assertTrue(b.lease.claim())
        self.step(TTL - MARGIN - 2.0 - 0.1)
        self.assertTrue(b.lease.valid())
        self.step(0.2)
        self.assertFalse(b.lease.valid())
        self.assertAlmostEqual(self.time.now - sent, TTL - MARGIN + 0.1)


class OutcomeUnknownTests(LeaseCase):
    def test_a_renewal_whose_reply_is_lost_but_landed_moves_the_deadline_from_its_send(self):
        a = self.router("a", 0.0)
        self.holding(a)
        self.store.fail("replace", LEASE, name=NAME, code=504, commit=True)
        sent = self.time.now
        self.assertTrue(a.lease.renew())
        self.step(TTL - MARGIN - 0.05)
        self.assertTrue(a.lease.valid())
        self.step(0.1)
        self.assertFalse(a.lease.valid())
        self.assertAlmostEqual(self.time.now - sent, TTL - MARGIN + 0.05)

    def test_a_renewal_that_did_not_land_keeps_the_old_deadline(self):
        a = self.router("a", 0.0)
        self.holding(a)
        self.step(1.0)
        before = self.stored()["metadata"]["resourceVersion"]
        self.store.fail("replace", LEASE, name=NAME, code=500, commit=False)
        self.assertTrue(a.lease.renew())
        self.assertEqual(self.stored()["metadata"]["resourceVersion"], before)
        # The last landed write was sent one second earlier.
        self.step(TTL - MARGIN - 1.0 - 0.05)
        self.assertTrue(a.lease.valid())
        self.step(0.1)
        self.assertFalse(a.lease.valid())
        self.assertFalse(a.lease.renew())

    def test_a_takeover_whose_reply_is_lost_is_won_only_if_it_landed(self):
        a, b = self.router("a", 0.0), self.router("b", 8.0)
        self.holding(a)
        b.lease.read()
        self.step(TTL + 0.1)
        self.store.fail("replace", LEASE, name=NAME, code=504, commit=False)
        self.assertFalse(b.lease.claim())
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "a")
        self.store.fail("replace", LEASE, name=NAME, code=504, commit=True)
        self.assertTrue(b.lease.claim())
        self.assertEqual(b.lease.epoch, 2)
        self.assertTrue(b.lease.valid())

    def test_a_takeover_whose_outcome_cannot_be_read_is_resumed_from_the_lease(self):
        a, b = self.router("a", 0.0), self.router("b", 8.0)
        self.holding(a)
        b.lease.read()
        self.step(TTL + 0.1)

        def landed_then_down(request: httpx.Request) -> str:
            if request.method == "PUT":
                b.fault = lambda request: "lost"
                return "unanswered"
            return ""

        b.fault = landed_then_down
        with self.assertRaises(LeaseError):
            b.lease.claim()
        self.assertFalse(b.lease.owned)
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "b")
        b.fault = lambda request: ""
        # B finds its own write and keeps that epoch.
        self.assertTrue(b.lease.claim())
        self.assertEqual(b.lease.epoch, 2)

    def test_a_create_whose_reply_is_lost_is_found_on_the_re_read(self):
        a = self.router("a", 0.0)
        self.store.fail("create", LEASE, name=NAME, code=504, commit=True)
        self.assertTrue(a.lease.claim())
        self.assertEqual(self.stored()["spec"]["holderIdentity"], "a")
        self.assertEqual(a.lease.epoch, 1)

    def test_a_release_whose_reply_is_lost_still_stops_this_router(self):
        a = self.router("a", 0.0)
        self.holding(a)
        self.store.fail("replace", LEASE, name=NAME, code=504, commit=False)
        a.lease.release()
        self.assertFalse(a.lease.owned)
        self.assertFalse(a.lease.valid())


class UnavailableTests(LeaseCase):
    def test_a_short_outage_keeps_the_holder_serving(self):
        a, b = self.router("a", 0.0), self.router("b", 21.0)
        self.holding(a)
        b.lease.read()
        a.down = b.down = True
        for _ in range(2):
            self.step(1.0, a.lease.renew, b.lease.claim)
            self.assertTrue(a.lease.valid())
        a.down = b.down = False
        self.step(0.5, a.lease.renew)
        self.assertTrue(a.lease.valid())
        self.assertFalse(b.lease.claim())

    def test_a_long_outage_fences_the_holder_before_the_standby_can_claim(self):
        a, b = self.router("a", 0.0), self.router("b", 21.0)
        self.holding(a)
        sent = self.time.now
        b.lease.read()
        a.down = b.down = True
        fenced_at = None
        for _ in range(40):
            self.step(0.25, b.lease.claim)
            if self.time.now - sent >= 1.0:
                a.lease.renew()
            if fenced_at is None and not a.lease.valid():
                fenced_at = self.time.now
            with self.assertRaises(LeaseError):
                b.lease.claim()
        assert fenced_at is not None
        self.assertAlmostEqual(fenced_at, sent + TTL - MARGIN, delta=0.25)
        self.assertFalse(a.lease.renew())
        a.down = b.down = False
        # B saw A's last version more than a duration ago, and A has fenced.
        self.assertTrue(b.lease.claim())
        self.assertTrue(b.lease.valid())
        self.assertFalse(a.lease.renew())

    def test_a_read_that_cannot_reach_the_api_server_is_a_lease_error(self):
        a = self.router("a", 0.0)
        a.down = True
        with self.assertRaises(LeaseError):
            a.lease.read()
        with self.assertRaises(LeaseError):
            a.lease.claim()
        self.assertFalse(a.lease.owned)

    def test_a_forbidden_renewal_fences_at_the_deadline(self):
        a = self.router("a", 0.0)
        self.holding(a)
        sent = self.time.now
        self.store.fail("replace", LEASE, name=NAME, code=403, times=100)
        while a.lease.renew():
            self.step(0.5)
        self.assertAlmostEqual(self.time.now, sent + TTL - MARGIN, delta=0.5)
        self.assertFalse(a.lease.valid())


class ChaosTests(LeaseCase):
    """Two routers and their restarts under random faults never serve together."""

    def test_random_faults_restarts_and_outages_never_split_the_lease(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                self.setUp()
                self.chaos(random.Random(seed))

    def chaos(self, rng: random.Random) -> None:
        offsets = {"a": 0.0, "b": 77.7}
        routers = {name: self.router(f"{name}-0", offset) for name, offset in offsets.items()}
        generation = dict.fromkeys(offsets, 0)
        stopped: dict[str, float] = {}
        takeovers = 0
        holder_before = ""

        def faults(request: httpx.Request) -> str:
            roll = rng.random()
            return "lost" if roll < 0.05 else "unanswered" if roll < 0.1 else ""

        for name in routers:
            routers[name].fault = faults
        for tick in range(4000):
            self.time.advance(0.05)
            self.fenced()
            for name, router in list(routers.items()):
                if name in stopped:
                    if self.time.now >= stopped[name]:
                        # The pod restarts with a new process, so a new holder token.
                        del stopped[name]
                        generation[name] += 1
                        self.routers.remove(router)
                        router = routers[name] = self.router(
                            f"{name}-{generation[name]}", offsets[name] + 1000 * generation[name]
                        )
                        router.fault = faults
                    continue
                if rng.random() < 0.002:
                    stopped[name] = self.time.now + rng.uniform(0.5, 15.0)
                    if rng.random() < 0.5 and router.lease.owned:
                        router.lease.release()
                    router.lease = _Dead(router.lease)
                    continue
                router.down = rng.random() < 0.02 or (tick // 400) % 5 == 3
                with contextlib.suppress(LeaseError):
                    if router.lease.owned and tick % 20 == 0:
                        router.lease.renew()
                    elif not router.lease.owned and tick % 5 == 0:
                        router.lease.claim()
                self.fenced()
            holder = next((r.lease.holder for r in self.routers if r.lease.valid()), "")
            if holder and holder != holder_before:
                takeovers += 1
                holder_before = holder
        self.assertGreater(takeovers, 2)
        self.routers.clear()


class _Dead:
    """A stopped router's lease: it holds nothing and never writes again."""

    def __init__(self, lease: KubernetesLease) -> None:
        self.holder = lease.holder
        self.owned = False

    def valid(self) -> bool:
        return False


class SettingsTests(unittest.TestCase):
    def test_a_kubernetes_lease_refuses_timings_it_cannot_fence_with(self):
        for name, ttl, margin, why in (
            ("narwhal-router", 5.5, 1.0, "whole positive number"),
            ("narwhal-router", 0, 0.0, "whole positive number"),
            ("narwhal-router", 5, 0.0, "safety margin must be positive"),
            ("narwhal-router", 5, 5.0, "safety margin must be positive"),
            ("Narwhal_Router", 5, 1.0, "DNS subdomain"),
            ("a/b", 5, 1.0, "DNS subdomain"),
        ):
            with (
                self.subTest(name=name, ttl=ttl, margin=margin),
                self.assertRaisesRegex(ValueError, why),
            ):
                check_settings(name, ttl, margin)

    def test_a_lease_outside_a_pod_cannot_be_built(self):
        with self.assertRaises(ValueError):
            KubernetesLease.in_cluster(NAME, "a", TTL)


class StandbyTakeoverTests(FleetCase):
    """A standby router takes over a Kubernetes Lease after its primary stops renewing."""

    async def test_the_standby_takes_over_within_the_lease_duration_and_never_overlaps(self):
        quiet(self)
        store = ObjectStore()
        store.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}})

        def lease(holder: str) -> KubernetesLease:
            client = k.ApiClient(
                k.Connection(SERVER, NS),
                field_manager="narwhal-router",
                transport=store.transport(),
            )
            return KubernetesLease(client, NAME, holder, 2, safety_margin_s=1.0)

        primary = create_app(self.cfg).state.router
        self.addAsyncCleanup(primary.engines.aclose)
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        primary_lease = lease("primary")
        self.assertTrue(primary_lease.claim())
        primary.lease_epoch, primary.lease_holder = primary_lease.epoch, primary_lease.holder
        document = state.snapshot(primary)
        alive = True
        last_renewal = 0.0

        async def renew() -> None:
            nonlocal last_renewal
            while alive:
                sent = asyncio.get_running_loop().time()
                self.assertTrue(primary_lease.renew())
                last_renewal = sent
                await asyncio.sleep(0.1)

        def transport(request: httpx.Request) -> httpx.Response:
            if not alive:
                raise httpx.ConnectError("connection refused", request=request)
            if request.url.path == "/ready":
                return httpx.Response(200, json={"ready": True})
            return httpx.Response(200, json=document)

        router.standby = True
        router.lease = lease("standby")
        renewing = asyncio.create_task(renew())
        loop = asyncio.create_task(
            standby_loop(
                router,
                "http://primary",
                router.lease,
                probe_interval_s=0.05,
                takeover_after=2,
                transport=httpx.MockTransport(transport),
            )
        )
        try:
            async with asyncio.timeout(5):
                while not router.primary_seen:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
            alive = False
            await renewing
            stopped = last_renewal
            async with asyncio.timeout(5):
                while router.standby:
                    self.assertFalse(primary_lease.valid() and router.lease.valid())
                    await asyncio.sleep(0.005)
            took = asyncio.get_running_loop().time() - stopped
            self.assertFalse(router.standby, router.failover_blocked)
            self.assertFalse(primary_lease.valid())
            self.assertEqual(router.lease_epoch, primary_lease.epoch + 1)
            # One lease duration after the last renewal, plus the probes that notice it.
            self.assertGreaterEqual(took, 2.0)
            self.assertLess(took, 3.5)
        finally:
            alive = False
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop


class RouterAppTests(FleetCase):
    """The router app on a Kubernetes Lease: its first start, its option and its shutdown."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        quiet(self)
        self.store = ObjectStore()
        self.store.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}})

    def lease(self, holder: str, ttl_s: int = 2, *, safety_margin_s: float = 1.0):
        client = k.ApiClient(
            k.Connection(SERVER, NS),
            field_manager="narwhal-router",
            transport=self.store.transport(),
        )
        return KubernetesLease(client, NAME, holder, ttl_s, safety_margin_s=safety_margin_s)

    async def test_a_first_start_that_creates_the_lease_serves_one_duration_later(self):
        lease = self.lease("router-0")
        app = create_app(
            self.cfg,
            standby_of="http://peer",
            claim_first=True,
            lease=lease,
            lease_ttl_s=2,
            lease_renew_interval_s=0.25,
            lease_safety_margin_s=1.0,
            standby_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        )
        loop = asyncio.get_running_loop()
        began = loop.time()
        async with app.router.lifespan_context(app):
            waited = loop.time() - began
            router = app.state.router
            self.assertGreaterEqual(waited, 2.0)
            self.assertFalse(router.standby)
            self.assertTrue(lease.valid())
            self.assertEqual((router.lease_epoch, router.lease_holder), (1, "router-0"))
            # The renewals went on while it waited.
            version = int(self.store.get(LEASE, NS, NAME)["metadata"]["resourceVersion"])
            self.assertGreater(version, 4)
        # Shutdown frees the Lease.
        self.assertEqual(self.store.get(LEASE, NS, NAME)["spec"]["holderIdentity"], "")

    async def test_the_kubernetes_lease_option_builds_the_lease_from_the_pod(self):
        built = []

        def in_cluster(name, holder, ttl_s, *, safety_margin_s):
            built.append((name, holder.rsplit(":", 1)[0], ttl_s, safety_margin_s))
            return self.lease(holder, int(ttl_s), safety_margin_s=safety_margin_s)

        with patch("narwhal.runtime.kubernetes_lease.KubernetesLease.in_cluster", in_cluster):
            app = create_app(
                self.cfg,
                standby_of="http://peer",
                kubernetes_lease=NAME,
                router_id="router-1",
                lease_ttl_s=6,
                lease_safety_margin_s=1.5,
            )
        self.addAsyncCleanup(app.state.router.engines.aclose)
        self.assertEqual(built, [(NAME, "router-1", 6, 1.5)])
        self.assertIsInstance(app.state.router.lease, KubernetesLease)
        with self.assertRaisesRegex(ValueError, "one of lease, lease_path and kubernetes_lease"):
            create_app(self.cfg, kubernetes_lease=NAME, lease_path=self.root / "lease.json")

    async def test_a_shutdown_hands_the_lease_to_a_standby_that_watched_it(self):
        primary = self.lease("primary")
        self.assertTrue(primary.claim())
        standby = self.lease("standby")
        standby.read()
        app = create_app(
            self.cfg,
            lease=primary,
            lease_ttl_s=2,
            lease_renew_interval_s=0.25,
            lease_safety_margin_s=1.0,
        )
        async with app.router.lifespan_context(app):
            self.assertTrue(primary.valid())
            app.state.yield_control()
            self.assertFalse(primary.valid())
            # The standby saw the Lease held, then freed: it serves at once.
            self.assertTrue(standby.claim())
            self.assertTrue(standby.valid())
            self.assertEqual(standby.epoch, 2)
