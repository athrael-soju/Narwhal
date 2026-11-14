import asyncio
import unittest
from typing import Any

import httpx

from narwhal.backends import load
from narwhal.backends.llamacpp.connector import FILES_PER_SLOT, SlotFileConnector
from narwhal.engines.connector import HandoffStepError, PrefillResult

A, B = "http://a:1", "http://b:1"
ENDPOINT = "/v1/completions"


class Calls:
    """Engine replies for the connector's staged steps, with each call recorded."""

    def __init__(self, slots: object = 2) -> None:
        self.slots = slots
        self.log: list[tuple[str, str, dict | None]] = []
        self.failing: set[str] = set()

    async def get(self, url: str, path: str, *, timeout_s: float) -> dict[str, Any]:
        self.log.append((url, path, None))
        return {"total_slots": self.slots}

    async def post(
        self, url: str, path: str, body: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        self.log.append((url, path, body))
        if path in self.failing:
            raise HandoffStepError(400, "Unable to restore slot")
        return {"n_saved": 5}


class SlotFileConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.kv = SlotFileConnector()
        self.calls = Calls()

    async def prefilled(self, url: str = A) -> tuple[dict, PrefillResult]:
        seat = await self.kv.claim(self.calls, url, "prefill")
        result = self.kv.prefill_result({}, url=url, endpoint=ENDPOINT, request_id=None)
        return seat, await self.kv.export(self.calls, url, seat, result, timeout_s=1)

    async def test_each_leg_takes_a_slot_and_the_engine_is_asked_for_its_count_once(self):
        first = await self.kv.claim(self.calls, A, "prefill")
        second = await self.kv.claim(self.calls, A, "decode")
        self.assertEqual((first, second), ({"id_slot": 0}, {"id_slot": 1}))
        self.assertEqual(await self.kv.claim(self.calls, B, "decode"), {"id_slot": 0})
        self.assertEqual(self.calls.log, [(A, "/props", None), (B, "/props", None)])

    async def test_a_leg_waits_for_a_slot_in_arrival_order_and_a_cancelled_wait_takes_none(self):
        held = [await self.kv.claim(self.calls, A, "prefill") for _ in range(2)]
        waits = [asyncio.create_task(self.kv.claim(self.calls, A, "decode")) for _ in range(3)]
        await asyncio.sleep(0)
        self.assertFalse(any(wait.done() for wait in waits))
        waits[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waits[0]
        self.kv.release(A, "prefill", held[1])
        self.assertEqual(await waits[1], held[1])
        self.kv.release(A, "prefill", held[0])
        # A wait cancelled once a slot is on its way hands the slot to the next leg.
        waits[2].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waits[2]
        self.assertEqual(await self.kv.claim(self.calls, A, "decode"), held[0])

    async def test_a_released_slot_is_the_next_one_taken(self):
        first = await self.kv.claim(self.calls, A, "prefill")
        second = await self.kv.claim(self.calls, A, "prefill")
        self.kv.release(A, "prefill", second)
        self.kv.release(A, "prefill", first)
        self.kv.release(A, "prefill", first)
        self.assertEqual(await self.kv.claim(self.calls, A, "decode"), first)
        self.assertEqual(await self.kv.claim(self.calls, A, "decode"), second)

    async def test_an_engine_that_reports_no_slots_fails_the_step(self):
        for slots in (0, None, "2", True):
            with self.subTest(slots=slots), self.assertRaisesRegex(HandoffStepError, "502"):
                await SlotFileConnector().claim(Calls(slots), A, "prefill")

    async def test_the_producer_saves_its_slot_and_the_consumer_restores_that_file(self):
        seat, result = await self.prefilled()
        filename = result.parameters()["filename"]
        self.assertEqual(result.parameters(), {"filename": filename, "tokens": 5})
        self.assertEqual(self.calls.log[-1], (A, "/slots/0?action=save", {"filename": filename}))
        self.kv.release(A, "prefill", seat)
        await self.kv.claim(self.calls, B, "decode")
        target = await self.kv.claim(self.calls, B, "decode")
        await self.kv.restore(self.calls, B, target, result, timeout_s=1)
        self.assertEqual(self.calls.log[-1], (B, "/slots/1?action=restore", {"filename": filename}))

    async def test_a_slot_cycles_its_files_and_engines_never_share_one(self):
        names = []
        for _ in range(FILES_PER_SLOT + 1):
            seat, result = await self.prefilled()
            self.kv.release(A, "prefill", seat)
            names.append(result.parameters()["filename"])
        self.assertEqual(len(set(names)), FILES_PER_SLOT)
        self.assertEqual(names[0], names[FILES_PER_SLOT])
        _, other = await self.prefilled(B)
        self.assertNotIn(other.parameters()["filename"], names)

    async def test_a_restore_the_engine_refuses_or_cannot_name_fails_the_step(self):
        seat, result = await self.prefilled()
        self.calls.failing.add("/slots/0?action=restore")
        with self.assertRaisesRegex(HandoffStepError, "Unable to restore slot"):
            await self.kv.restore(self.calls, B, {"id_slot": 0}, result, timeout_s=1)
        unsaved = self.kv.prefill_result({}, url=A, endpoint=ENDPOINT, request_id=None)
        with self.assertRaisesRegex(HandoffStepError, "names no saved slot file"):
            await self.kv.restore(self.calls, B, seat, unsaved, timeout_s=1)

    def test_both_legs_keep_prompt_caching_on_and_drop_a_client_slot(self):
        body = {"model": "m", "prompt": "x", "id_slot": 3}
        self.assertEqual(self.kv.prefill_body(body, None), {**body, "cache_prompt": True})
        result = self.kv.prefill_result({}, url=A, endpoint=ENDPOINT, request_id=None)
        leg = self.kv.decode_body(body, result=result, url=B, endpoint=ENDPOINT)
        self.assertEqual(leg, {"model": "m", "prompt": "x", "stream": True, "cache_prompt": True})
        with self.assertRaisesRegex(ValueError, "does not match decode"):
            self.kv.decode_body(body, result=result, url=B, endpoint="/v1/chat/completions")


class LlamacppBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = load("llamacpp")

    def test_the_backend_starts_no_engines_and_the_router_holds_the_roles(self):
        self.assertIsNone(self.backend.launcher)
        self.assertFalse(self.backend.role_switch.engine_held)
        self.assertTrue(self.backend.connector("slot-file").staged)

    def test_counts_use_the_text_route_as_the_server_tokenizes_each_request(self):
        dialect = self.backend.dialect
        self.assertEqual(
            dialect.tokenize_request("m", {"prompt": "hi"}),
            {"content": "hi", "add_special": True, "parse_special": True},
        )
        self.assertEqual(
            dialect.tokenize_request("m", {"prompt": [1, 2], "add_special_tokens": False}),
            {"content": [1, 2], "add_special": False, "parse_special": True},
        )
        self.assertEqual(
            dialect.rendered_tokenize_request("m", "<|user|>hi", {"messages": []}),
            {"content": "<|user|>hi", "add_special": True, "parse_special": True},
        )
        self.assertEqual(dialect.tokenize_response({"tokens": [1, 2, 3]}), 3)
        self.assertEqual(dialect.tokenize_token_ids({"tokens": [1, 2, 3]}), [1, 2, 3])
        for reply in ({}, {"tokens": 3}, []):
            self.assertIsNone(dialect.tokenize_response(reply))

    def test_the_context_limit_is_one_slot_of_the_server_context(self):
        dialect = self.backend.dialect
        props = {"total_slots": 2, "default_generation_settings": {"n_ctx": 4096}}
        self.assertEqual(dialect.context_limit(props, "m"), 4096)
        for reply in ({}, {"default_generation_settings": {"n_ctx": 0}}, [], None):
            self.assertIsNone(dialect.context_limit(reply, "m"))

    async def test_the_version_is_the_server_build(self):
        replies = iter(
            [
                httpx.Response(200, json={"build_info": " b1-abc "}),
                httpx.Response(200, json={"build_info": ""}),
                httpx.Response(503),
            ]
        )
        transport = httpx.MockTransport(lambda request: next(replies))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertEqual(await self.backend.identity.version(client, "http://e/"), "b1-abc")
            with self.assertRaisesRegex(ValueError, "/props returned no build_info"):
                await self.backend.identity.version(client, "http://e")
            with self.assertRaises(httpx.HTTPStatusError):
                await self.backend.identity.version(client, "http://e")
            with self.assertRaisesRegex(ValueError, "publishes no process start"):
                await self.backend.identity.read(client, "http://e")

    def test_cache_hits_read_the_cached_prompt_token_counter(self):
        metrics = self.backend.metrics
        self.assertEqual(metrics.prefix_cache_hits("llamacpp:prompt_tokens_cached_total 12\n"), 12)
        self.assertIsNone(metrics.prefix_cache_hits("llamacpp:prompt_tokens_total 12\n"))
        self.assertIsNone(metrics.kv_capacity("llamacpp:n_tokens_max 13\n"))
