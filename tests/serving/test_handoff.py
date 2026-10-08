"""Check the KV handoff bound from attested producer leases and every handoff expiry."""

import asyncio
import json
import time
import unittest

from narwhal.engines.attestation import attested_kv_lease
from narwhal.serving.handoff import handoff_bound
from narwhal.serving.policy import ServingPolicy
from narwhal.serving.seats import decode_seats
from narwhal.types import Phase, Request
from tests.serving.test_http_accounting import HttpHarness


def attestation(*args):
    return {"launch": {"args": list(args)}}


def connector(**extra):
    return json.dumps(
        {
            "kv_connector": "NixlConnector",
            "kv_role": "kv_both",
            "kv_connector_extra_config": {"backends": ["UCX"], **extra},
        }
    )


class AttestedLeaseTests(unittest.TestCase):
    def test_the_lease_comes_from_the_attested_connector_configuration(self):
        for payload, lease in (
            (attestation("--kv-transfer-config", connector(kv_lease_duration=30)), 30),
            (attestation(f"--kv-transfer-config={connector(kv_lease_duration=6)}"), 6),
            (attestation("--kv-transfer-config", connector()), None),
            (attestation("--kv-transfer-config", connector(kv_lease_duration="30")), None),
            (attestation("--kv-transfer-config", connector(kv_lease_duration=0)), None),
            (attestation("--kv-transfer-config", "{"), None),
            (attestation("--max-num-seqs", "64"), None),
            ({"launch": {}}, None),
            (None, None),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(attested_kv_lease(payload), lease)
        other = json.loads(connector(kv_lease_duration=30))
        other["kv_connector"] = "LMCacheConnector"
        self.assertIsNone(attested_kv_lease(attestation("--kv-transfer-config", json.dumps(other))))


class HandoffExpiryTests(HttpHarness):
    """Each handoff expiry returns 504 handoff_expired and counts as expired by `handoff`."""

    def lease(self, iid, seconds):
        self.router.attested(
            iid, attestation("--kv-transfer-config", connector(kv_lease_duration=seconds))
        )

    def assert_handoff_expiry(self, response, error):
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(
            response.json()["error"],
            {
                "message": "KV handoff expired before decode dispatch",
                "type": "handoff_expired",
                "code": "handoff_expired",
            },
        )
        row = self.terminal_rows()[-1]
        self.assertEqual(
            (row["terminal"], row["reason"], row["status"], row["error_type"]),
            ("expired", "handoff", 504, "handoff_expired"),
        )
        self.assertIn(error, row["error"])
        self.assertEqual(self.router.outcome_reasons["expired"]["handoff"], 1)
        self.assert_released()

    async def test_state_and_metrics_report_each_producers_lease_and_bound(self):
        client = self.client()
        self.lease("e0", 30)
        self.assertEqual(handoff_bound(self.router, "e0"), 25.0)
        self.assertIsNone(handoff_bound(self.router, "e3"))
        state = (await client.get("/narwhal/state")).json()["handoff"]
        self.assertEqual(state["e0"], {"kv_lease_s": 30, "renewal_s": 5, "bound_s": 25.0})
        self.assertEqual(state["e3"], {"kv_lease_s": None, "renewal_s": None, "bound_s": None})
        text = (await client.get("/metrics")).text
        self.assertIn('narwhal_kv_lease_seconds{iid="e0"} 30', text)
        self.assertIn('narwhal_handoff_bound_seconds{iid="e0"} 25', text)
        self.assertNotIn('narwhal_handoff_bound_seconds{iid="e3"}', text)

    async def test_handoff_age_counts_from_prefill_completion(self):
        client = self.client()
        self.lease("e0", 6)
        # Prefill outlasts the 5 s bound on the router clock; its age starts afterwards.
        offset = [0.0]
        self.router._clock = lambda: time.monotonic() + offset[0]
        self.blocked = asyncio.Event()
        task = asyncio.create_task(self.post(client))
        await self.started.wait()
        offset[0] += 6.0
        self.blocked.set()
        self.assertEqual((await task).status_code, 200)
        self.assert_released()

    async def test_a_handoff_past_its_bound_at_decode_dispatch_expires(self):
        client = self.client()
        self.lease("e0", 6)
        offset = [0.0]
        self.router._clock = lambda: time.monotonic() + offset[0]
        schedule = self.router.scheduler.schedule

        def slow_decode_placement(request, **kwargs):
            if request.phase is Phase.DECODE:
                offset[0] += 5.0
            return schedule(request, **kwargs)

        self.router.scheduler.schedule = slow_decode_placement
        response = await self.post(client)
        self.assert_handoff_expiry(response, "KV handoff expired before decode dispatch")
        decode_legs = [body for _, body in self.calls if "kv_transfer_params" not in body]
        self.assertEqual(decode_legs, [])

    async def test_a_decode_seat_wait_ends_at_the_bound(self):
        self.cfg.serving = ServingPolicy(queue_capacity=4, queue_timeout_s=5.0)
        client = self.client()
        # A 1 s lease renews every 0 s, so the bound is the whole second.
        self.router.kv_leases["e0"] = 1
        decode = self.router.monitor.instances["e3"]
        held = [f"held{index}" for index in range(decode_seats(self.router, "e3"))]
        for rid in held:
            self.router.monitor.dispatched("e3", Request(rid, 5, phase=Phase.DECODE))
        response = await self.post(client)
        self.assert_handoff_expiry(
            response, "KV handoff bound reached while waiting for a decode seat"
        )
        waited = self.terminal_rows()[-1]["queue_waits"]["decode"]
        self.assertGreaterEqual(waited, 0.9)
        for rid in held:
            self.router.monitor.finished(decode.iid, rid)

    async def test_a_handoff_expiry_with_attempts_left_retries_on_a_fresh_prefill(self):
        self.cfg.serving = ServingPolicy(max_attempts=2, retry_base_s=0.001, retry_cap_s=0.001)
        client = self.client()
        self.lease("e0", 6)
        offset = [0.0]
        self.router._clock = lambda: time.monotonic() + offset[0]
        schedule = self.router.scheduler.schedule
        decode_placements = []

        def slow_once(request, **kwargs):
            if request.phase is Phase.DECODE:
                decode_placements.append(request.rid)
                if len(decode_placements) == 1:
                    offset[0] += 5.0
            return schedule(request, **kwargs)

        self.router.scheduler.schedule = slow_once
        response = await self.post(client)
        self.assertEqual(response.status_code, 200, response.text)
        row = self.terminal_rows()[-1]
        (failure,) = row["attempt_failures"]
        self.assertEqual(
            (failure["phase"], failure["reason"], failure["retry_reason"]),
            ("decode", "handoff", "allowed"),
        )
        self.assertEqual(row["attempts"], 2)
        self.assert_released()
