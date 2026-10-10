import asyncio
import itertools
import json
import time
import unittest

import httpx

from narwhal.engines.connector import RendezvousConnector
from tests.serving.test_http_accounting import HttpHarness


class FakeRendezvous(RendezvousConnector):
    name = "fake"
    decode_wait_s = 0.3

    def __init__(self):
        self.rooms = itertools.count()

    def handoff_bound(self, lease_s):
        return float(lease_s)

    def rendezvous(self, producer):
        return {"room": next(self.rooms)}

    def prefill_body(self, body, rendezvous):
        return {**body, "leg": "prefill", **rendezvous}

    def decode_body(self, body, rendezvous):
        return {**body, "leg": "decode", **rendezvous}


class RendezvousServingTests(HttpHarness):
    def setUp(self):
        super().setUp()
        self.prefill_seen = asyncio.Event()
        self.decode_seen = asyncio.Event()
        self.prefill_status = 200

    async def engine(self, request):
        body = json.loads(request.content)
        self.calls.append((dict(request.headers), body))
        if body.get("leg") == "prefill":
            self.prefill_seen.set()
            # Each leg waits for the other, so only concurrent dispatch completes.
            await asyncio.wait_for(self.decode_seen.wait(), 5)
            return httpx.Response(self.prefill_status, json={})
        self.decode_seen.set()
        if self.prefill_status != 200:
            await asyncio.Event().wait()
        await asyncio.wait_for(self.prefill_seen.wait(), 5)
        wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in self.decode_frames)
        return httpx.Response(200, text=wire + "data: [DONE]\n\n")

    def client(self):
        client = super().client()
        self.router.engines.kv = FakeRendezvous()
        return client

    async def test_both_legs_run_concurrently_with_one_rendezvous(self):
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 200, response.text)
        legs = {body["leg"]: (headers, body) for headers, body in self.calls}
        self.assertEqual(legs["prefill"][1]["room"], legs["decode"][1]["room"])
        self.assertEqual(legs["prefill"][1]["max_tokens"], 1)
        self.assertTrue(legs["decode"][1]["stream"])
        self.assertTrue(legs["prefill"][0]["x-request-id"].endswith("-prefill"))
        self.assertTrue(legs["decode"][0]["x-request-id"].endswith("-decode"))
        self.assert_released()

    async def test_decode_without_its_prefill_ends_at_the_wait_bound(self):
        self.prefill_status = 500
        client = self.client()
        began = time.monotonic()
        response = await self.post(client)
        self.assertLess(time.monotonic() - began, 2)
        self.assertGreaterEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["error"]["type"], "prefill")
        row = self.terminal_rows()[-1]
        self.assertIn("prefill leg", row["error"])
        for failure in row["attempt_failures"]:
            self.assertEqual(failure["phase"], "prefill")
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
