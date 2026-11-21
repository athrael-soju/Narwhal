import json
import unittest

import httpx

from narwhal.backends import load
from narwhal.backends.trtllm.connector import TransceiverConnector
from narwhal.backends.trtllm.identity import TrtllmIdentity, transfer_timeout_ms
from narwhal.engines.connector import PrefillResult
from narwhal.serving.execution import request_error

BACKEND = load("trtllm")
# A context leg's response as the server returns it for a completion.
CONTEXT = {
    "id": "cmpl-1",
    "prompt_token_ids": [760, 6511, 314],
    "choices": [
        {
            "index": 0,
            "text": " Paris",
            "token_ids": None,
            "disaggregated_params": {
                "request_type": "context_only",
                "first_gen_tokens": [11751],
                "ctx_request_id": 424242,
                "disagg_request_id": 424242,
                "encoded_opaque_state": "AQAAAAACAAAAAAAAAAEA",
                "ctx_info_endpoint": None,
            },
        }
    ],
}


def result(kv: TransceiverConnector, producer: str = "http://ctx") -> PrefillResult:
    return kv.prefill_result(CONTEXT, url=producer, endpoint="/v1/completions", request_id=None)


class TransceiverConnectorTests(unittest.TestCase):
    def setUp(self):
        self.kv = BACKEND.connector("ucx")

    def test_each_context_leg_asks_for_a_fresh_handoff_id(self):
        first, second = self.kv.prefill_params(), self.kv.prefill_params()
        self.assertEqual(first["disaggregated_params"]["request_type"], "context_only")
        ids = [p["disaggregated_params"]["disagg_request_id"] for p in (first, second)]
        self.assertNotEqual(ids[0], ids[1])
        self.assertTrue(all(0 < value < 2**63 for value in ids))

    def test_a_completion_generation_leg_continues_from_the_context_prompt(self):
        body = {"model": "m", "prompt": "The capital of France is", "max_tokens": 6}
        leg = self.kv.decode_body(
            body, result=result(self.kv), url="http://gen", endpoint="/v1/completions"
        )
        self.assertEqual(leg["prompt"], [760, 6511, 314])
        self.assertTrue(leg["stream"])
        params = leg["disaggregated_params"]
        self.assertEqual(params["request_type"], "generation_only")
        self.assertEqual((params["ctx_request_id"], params["first_gen_tokens"]), (424242, [11751]))
        self.assertNotIn("prompt_token_ids", params)

    def test_a_chat_generation_leg_keeps_its_messages_beside_the_prompt_ids(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        chat = self.kv.prefill_result(
            CONTEXT, url="http://ctx", endpoint="/v1/chat/completions", request_id=None
        )
        leg = self.kv.decode_body(
            body, result=chat, url="http://gen", endpoint="/v1/chat/completions"
        )
        self.assertEqual(leg["messages"], body["messages"])
        self.assertEqual(leg["prompt_token_ids"], [760, 6511, 314])

    def test_a_decode_on_the_producer_computes_the_prompt_again(self):
        body = {"model": "m", "prompt": "x", "disaggregated_params": {"request_type": "x"}}
        leg = self.kv.decode_body(
            body, result=result(self.kv), url="http://ctx", endpoint="/v1/completions"
        )
        self.assertEqual(leg, {"model": "m", "prompt": "x", "stream": True})

    def test_a_context_response_without_complete_parameters_is_refused(self):
        for broken in (
            {"ctx_request_id": "1"},
            {"first_gen_tokens": []},
            {"encoded_opaque_state": 3},
        ):
            with self.subTest(broken=broken):
                payload = json.loads(json.dumps(CONTEXT))
                payload["choices"][0]["disaggregated_params"].update(broken)
                with self.assertRaises(ValueError):
                    self.kv.extract(payload)
        with self.assertRaisesRegex(ValueError, "prompt_token_ids"):
            self.kv.extract({**CONTEXT, "prompt_token_ids": None})
        self.assertEqual(self.kv.extract({"choices": [{"index": 0}]}), {})

    def test_an_output_limit_below_two_tokens_is_refused_before_prefill(self):
        class Router:
            class cfg:
                model = "m"

            class engines:
                kv = self.kv

        refused = request_error(Router(), {"model": "m", "prompt": "x", "max_tokens": 1})
        self.assertEqual(refused.status_code, 400)
        self.assertIsNone(request_error(Router(), {"model": "m", "prompt": "x", "max_tokens": 2}))


class TrtllmIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_version_route_names_the_engine_version(self):
        def answer(request):
            self.assertEqual(request.url.path, "/version")
            return httpx.Response(200, json={"version": "1.4.0rc3"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            identity = await TrtllmIdentity().read(client, "http://e", process_start=12.5)
        self.assertEqual(
            (identity.version, identity.process_start_time_seconds), ("1.4.0rc3", 12.5)
        )

    def test_the_lease_is_the_attested_transfer_timeout(self):
        def attested(config: dict | None) -> dict:
            files = {} if config is None else {"engine.yaml": json.dumps(config)}
            args = ["--config", "/narwhal-hooks/engine.yaml"]
            return {"launch": {"args": args, "config_files": files}}

        reader = TrtllmIdentity()
        transceiver = {"backend": "UCX"}
        self.assertEqual(reader.kv_lease(attested({"cache_transceiver_config": transceiver})), 60)
        timed = {**transceiver, "kv_transfer_timeout_ms": 45500}
        self.assertEqual(reader.kv_lease(attested({"cache_transceiver_config": timed})), 45)
        self.assertIsNone(reader.kv_lease(attested(None)))
        self.assertIsNone(reader.kv_lease(attested({"kv_cache_config": {}})))
        self.assertIsNone(reader.kv_lease({"launch": {}}))
        unnamed = attested({"cache_transceiver_config": timed})
        unnamed["launch"]["args"] = ["--config"]
        self.assertEqual(reader.kv_lease(unnamed), 45)
        self.assertIsNone(transfer_timeout_ms("not: [json"))


class TrtllmMetricsTests(unittest.TestCase):
    def test_reads_drain_and_an_idle_read_reports_nothing(self):
        metrics = BACKEND.metrics
        self.assertTrue(metrics.drains)
        self.assertIsNone(metrics.kv_capacity("[]"))
        self.assertIsNone(metrics.cache_block_tokens("[]"))
        self.assertIsNone(metrics.kv_capacity('{"error": "busy"}'))


class TrtllmDialectTests(unittest.TestCase):
    def test_counts_prompts_only_and_reads_the_context_limit_from_the_launch(self):
        dialect = BACKEND.dialect
        self.assertFalse(dialect.tokenize_messages)
        self.assertIsNone(dialect.render_path)
        self.assertEqual(dialect.context_limit_flag, "--max_seq_len")
        self.assertEqual(
            dialect.tokenize_request("m", {"prompt": "hi", "add_special_tokens": False}),
            {"model": "m", "prompt": "hi"},
        )

    def test_the_usage_frame_reports_the_output_count(self):
        dialect = BACKEND.dialect
        frame = {"choices": [], "usage": {"completion_tokens": 7, "prompt_tokens": 5}}
        self.assertEqual(dialect.stream_output_tokens(frame), 7)
        self.assertIsNone(dialect.stream_output_tokens({"choices": [{"text": "a"}]}))


if __name__ == "__main__":
    unittest.main()
