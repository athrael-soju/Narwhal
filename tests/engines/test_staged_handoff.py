"""A staged connector's steps run around the two legs, inside each leg's deadline."""

import asyncio
import json
import unittest

import httpx

from narwhal.backends import load
from narwhal.engines.client import NO_HANDOFF_DETAIL, EngineClient, EngineError
from narwhal.engines.connector import HandoffStepError, KvConnector, PrefillResult
from narwhal.engines.handoff import HandoffLegs
from narwhal.types import LEG_KV_HANDOFF
from tests.wire import engine_transports

ENDPOINT = "/v1/completions"
TOKEN = b'data: {"choices":[{"text":"x","token_ids":[1]}]}\n\ndata: [DONE]\n\n'


class Staged(KvConnector):
    """Keeps the producer's state in a named file that the consumer loads."""

    name = contract_name = "staged"
    param_key = "kv"
    staged = True

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.held: set[str] = set()

    def handoff_bound(self, lease_s: int) -> float:
        return float(lease_s)

    def prefill_params(self) -> dict:
        return {}

    def extract(self, payload: dict) -> dict:
        return dict(payload.get("kv") or {})

    def attach(self, body: dict, params: dict) -> None:
        pass

    def prefill_result(self, payload, *, url, endpoint, request_id):
        return PrefillResult(self.name, url, endpoint, request_id, json.dumps({"file": ""}))

    async def claim(self, calls, url, phase):
        self.events.append(("claim", url, phase))
        return {"slot": 3 if phase == "prefill" else 5}

    def release(self, url, phase, seat):
        self.events.append(("release", url, phase, dict(seat)))

    async def export(self, calls, url, seat, result, *, timeout_s):
        name = f"{url.rsplit('/', 1)[-1]}-{seat['slot']}.bin"
        await calls.post(
            url, f"/slots/{seat['slot']}?action=save", {"filename": name}, timeout_s=timeout_s
        )
        self.held.add(name)
        self.events.append(("export", url, name))
        return PrefillResult(
            self.name, url, result.endpoint, result.producer_request_id, json.dumps({"file": name})
        )

    async def restore(self, calls, url, seat, result, *, timeout_s):
        name = result.parameters()["file"]
        await calls.post(
            url, f"/slots/{seat['slot']}?action=restore", {"filename": name}, timeout_s=timeout_s
        )
        self.events.append(("restore", url, name))

    def discard(self, result):
        name = result.parameters()["file"]
        if name in self.held:
            self.held.discard(name)
            self.events.append(("discard", name))


class StagedHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.kv = Staged()
        self.requests: list[tuple[str, str, dict]] = []
        self.fail: dict[str, int] = {}
        self.delay: dict[str, float] = {}
        self.client = EngineClient(
            kv=self.kv,
            dialect=load("vllm").dialect,
            model="stub",
            prefill_timeout_s=0.5,
            **engine_transports(self.handle),
        )
        self.addAsyncCleanup(self.client.aclose)

    async def handle(self, request):
        body = json.loads(request.content) if request.content else {}
        action = request.url.params.get("action", "")
        kind = action or ("decode" if body.get("stream") else "prefill")
        self.requests.append((request.url.host, kind, body))
        await asyncio.sleep(self.delay.get(kind, 0))
        if kind in self.fail:
            return httpx.Response(self.fail[kind], text=f"{kind} refused")
        if kind == "decode":
            return httpx.Response(200, content=TOKEN, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"text": "x"}]})

    def kinds(self):
        return [(host, kind) for host, kind, _ in self.requests]

    async def decode(self, result, url="http://d", timeout=None):
        return [
            batch
            async for batch in self.client.decode(
                url, ENDPOINT, {"prompt": "p"}, {}, result, first_token_timeout_s=timeout
            )
        ]

    async def test_prefill_claims_a_slot_exports_and_releases(self):
        result = await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.assertEqual(self.kinds(), [("p", "prefill"), ("p", "save")])
        self.assertEqual(self.requests[0][2]["slot"], 3)
        self.assertEqual(result.parameters(), {"file": "p-3.bin"})
        self.assertEqual([event[0] for event in self.kv.events], ["claim", "export", "release"])

    async def test_decode_claims_a_slot_restores_then_streams(self):
        result = await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.requests.clear()
        self.kv.events.clear()
        batches = await self.decode(result)
        self.assertTrue(batches)
        self.assertEqual(self.kinds(), [("d", "restore"), ("d", "decode")])
        self.assertEqual(self.requests[0][2], {"filename": "p-3.bin"})
        self.assertEqual(self.requests[1][2]["slot"], 5)
        self.assertEqual([event[0] for event in self.kv.events], ["claim", "restore", "release"])

    async def test_decode_on_the_producer_skips_the_restore(self):
        result = await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.requests.clear()
        await self.decode(result, url="http://p")
        self.assertEqual(self.kinds(), [("p", "decode")])

    async def test_a_failed_export_is_a_retryable_prefill_failure(self):
        self.fail["save"] = 400
        with self.assertRaises(EngineError) as raised:
            await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.assertEqual((raised.exception.leg, raised.exception.status), ("prefill", 502))
        self.assertTrue(raised.exception.detail.startswith(NO_HANDOFF_DETAIL))
        self.assertEqual(self.kv.events[-1][0], "release")

    async def test_a_failed_restore_fails_the_decode_leg_before_any_output(self):
        result = await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.fail["restore"] = 500
        self.requests.clear()
        with self.assertRaises(EngineError) as raised:
            await self.decode(result)
        self.assertEqual((raised.exception.leg, raised.exception.status), ("decode", 500))
        self.assertEqual(self.kinds(), [("d", "restore")])
        self.assertEqual(self.kv.events[-1][:3], ("release", "http://d", "decode"))

    async def test_the_export_counts_against_the_prefill_deadline(self):
        self.delay["save"] = 2
        with self.assertRaises(httpx.ReadTimeout):
            await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.assertEqual(self.kv.events[-1][0], "release")

    async def test_the_restore_counts_against_the_first_token_deadline(self):
        result = await self.client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.delay["restore"] = 2
        with self.assertRaises(EngineError) as raised:
            await self.decode(result, timeout=0.05)
        self.assertEqual(raised.exception.status, 504)

    async def test_closing_a_handoff_discards_its_export_once(self):
        handoff = await self.client.start_handoff("http://p", ENDPOINT, {"prompt": "p"}, {})
        self.assertEqual(self.kv.held, {"p-3.bin"})
        handoff.close()
        handoff.close()
        self.assertEqual(self.kv.held, set())
        self.assertEqual([e for e in self.kv.events if e[0] == "discard"], [("discard", "p-3.bin")])

    async def test_decode_handoff_closes_the_handoff_when_the_leg_ends(self):
        handoff = await self.client.start_handoff("http://p", ENDPOINT, {"prompt": "p"}, {})
        async for _ in self.client.decode_handoff(
            handoff, "http://d", ENDPOINT, {"prompt": "p"}, {}
        ):
            pass
        self.assertEqual(self.kv.held, set())

    async def test_a_cross_engine_probe_runs_every_step_and_discards(self):
        probe = await self.client.probe_inference("http://d", prefill_url="http://p")
        self.assertIsNone(probe.prefill.failed)
        self.assertIsNone(probe.decode.failed)
        self.assertEqual(
            self.kinds(), [("p", "prefill"), ("p", "save"), ("d", "restore"), ("d", "decode")]
        )
        self.assertEqual(self.kv.held, set())

    async def test_a_probe_reports_a_failed_step_as_a_handoff_failure(self):
        self.fail["save"] = 500
        probe = await self.client.probe_inference("http://d", prefill_url="http://p")
        self.assertEqual(probe.prefill.failed, LEG_KV_HANDOFF)
        self.assertTrue(probe.decode.inconclusive)

    async def test_an_unstaged_connector_makes_no_extra_request(self):
        client = EngineClient(
            kv=load("vllm").connector("nixl"),
            dialect=load("vllm").dialect,
            **engine_transports(self.handle_nixl),
        )
        self.addAsyncCleanup(client.aclose)
        result = await client.prefill("http://p", ENDPOINT, {"prompt": "p"}, {})
        HandoffLegs(client.kv).close()
        self.assertEqual(result.connector, "nixl")
        self.assertEqual(self.kinds(), [("p", "prefill")])

    def handle_nixl(self, request):
        self.requests.append((request.url.host, "prefill", json.loads(request.content)))
        return httpx.Response(
            200, json={"kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [0]}}
        )

    def test_the_step_error_keeps_the_engine_status(self):
        self.assertEqual(HandoffStepError(409, "busy").status, 409)


if __name__ == "__main__":
    unittest.main()
