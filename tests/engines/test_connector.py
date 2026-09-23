"""Check immutable producer descriptors and decode request construction."""

import json
import unittest
from dataclasses import replace

from narwhal.engines.connector import NixlConnector, PrefillResult, lookup


class ConnectorTests(unittest.TestCase):
    """Decode requests copy the descriptor's producer ID and transfer parameters."""

    def setUp(self):
        self.connector = NixlConnector()
        self.params = {
            "remote_engine_id": "producer",
            "remote_block_ids": [[0, 1], [2]],
            "opaque": {"value": [3]},
        }
        self.result = self.connector.prefill_result(
            {"kv_transfer_params": self.params},
            url="http://producer",
            endpoint="/v1/completions",
            request_id="rid",
        )

    def test_descriptor_copies_isolate_backend_state(self):
        """Mutating source or decoded transport parameters leaves the descriptor intact."""
        self.params["opaque"]["value"].append(9)
        params = self.result.parameters()
        self.assertEqual(params["opaque"]["value"], [3])
        params["remote_block_ids"][0].append(8)
        self.assertEqual(self.result.parameters()["remote_block_ids"], [[0, 1], [2]])
        self.assertEqual(self.result.producer_request_id, "rid")

    def test_cross_engine_decode_attaches_descriptor_and_retains_prompt(self):
        """The consumer receives the original prompt and the producer's validated descriptor."""
        body = {"prompt": [10, 20], "stream": False, "kv_transfer_params": {"spoofed": True}}
        decoded = self.connector.decode_body(
            body, self.result, url="http://consumer", endpoint="/v1/completions"
        )
        self.assertEqual(decoded["prompt"], [10, 20])
        self.assertTrue(decoded["stream"])
        self.assertEqual(decoded["kv_transfer_params"], self.result.parameters())
        self.assertEqual(body["kv_transfer_params"], {"spoofed": True})

    def test_same_worker_and_standalone_decode_strip_client_handoff(self):
        """Same-worker and standalone calls carry only their own prompt state."""
        for result in (self.result, None):
            with self.subTest(result=result):
                body = self.connector.decode_body(
                    {"prompt": "original", "kv_transfer_params": {"spoofed": True}},
                    result,
                    url="http://producer",
                    endpoint="/v1/completions",
                )
                self.assertEqual(body, {"prompt": "original", "stream": True})

    def test_descriptor_type_connector_and_endpoint_must_match(self):
        """Typed handoffs bind both the connector and completion endpoint."""
        for result in (
            {},
            replace(self.result, connector="other"),
            replace(self.result, endpoint="/v1/chat/completions"),
        ):
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.connector.decode_body(
                    {}, result, url="http://consumer", endpoint="/v1/completions"
                )

    def test_flat_and_grouped_blocks_accept_zero_and_reject_invalid_ids(self):
        """Block identity accepts integer zero and rejects boolean or negative IDs."""
        for blocks in ([0, 1], [[0], [1]], []):
            self.assertEqual(
                self.connector.extract(
                    {"kv_transfer_params": {"remote_engine_id": "e", "remote_block_ids": blocks}}
                )["remote_block_ids"],
                blocks,
            )
        for blocks in ([True], [-1], [0.5], ["1"], [[0], [False]], None):
            with (
                self.subTest(blocks=blocks),
                self.assertRaisesRegex(ValueError, "remote_block_ids"),
            ):
                self.connector.extract(
                    {"kv_transfer_params": {"remote_engine_id": "e", "remote_block_ids": blocks}}
                )

    def test_invalid_response_shapes_and_empty_descriptors_fail(self):
        """Malformed choices and missing ownership fields prevent handoff construction."""
        for payload in (
            [],
            {"choices": [1]},
            {"choices": "bad"},
            {"kv_transfer_params": "bad"},
            {"kv_transfer_params": {"remote_block_ids": [1]}},
            {},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.connector.prefill_result(
                    payload, url="http://e", endpoint="/v1/completions", request_id=None
                )
        for raw in ("[]", "{}"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                PrefillResult("nixl", "http://e", "/v1/completions", None, raw).parameters()
        self.assertIs(lookup("nixl"), lookup("nixl"))
        with self.assertRaisesRegex(ValueError, "unknown connector"):
            lookup("unknown")

    def test_choice_level_descriptor_takes_precedence(self):
        """A choice-level descriptor supplies the transfer for that completion."""
        selected = {**self.params, "remote_engine_id": "choice"}
        payload = {"choices": [{"kv_transfer_params": selected}], "kv_transfer_params": self.params}
        self.assertEqual(self.connector.extract(payload), selected)
        self.assertEqual(json.loads(self.result.descriptor_json)["remote_engine_id"], "producer")
