"""Check the continuation request boundary before any backend work occurs."""

import math
import unittest
from types import SimpleNamespace

from narwhal.serving.completion import completion_body_error
from narwhal.serving.continuation_request import (
    ContinuationRequestError,
    ContinuationUnavailable,
    prepare_request,
)
from narwhal.serving.policy import ContinuationPolicy


class ContinuationRequestTests(unittest.TestCase):
    def setUp(self):
        self.policy = ContinuationPolicy(enabled=True, max_context_tokens=32)
        # Qualification itself is tested by engines/test_replay.py. These
        # bounds isolate HTTP eligibility without asserting backend capability.
        self.qualification = SimpleNamespace(
            contract=SimpleNamespace(
                vocab_size=256,
                max_context_tokens=24,
                max_output_tokens=12,
                allowed_stop_token_ids=(7,),
            )
        )
        self.body = {
            "narwhal_continuation": True,
            "prompt": [0, 255],
            "max_tokens": 4,
            "stream": True,
        }

    def prepare(self, body=None, endpoint="/v1/completions"):
        return prepare_request(endpoint, body or self.body, self.policy, self.qualification)

    def test_normalization_pins_neutral_settings_and_strips_router_flag(self):
        body, opted_in = self.prepare()
        self.assertTrue(opted_in)
        self.assertNotIn("narwhal_continuation", body)
        self.assertEqual(body["prompt"], self.body["prompt"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["repetition_penalty"], 1)
        self.assertEqual(body["min_tokens"], 0)
        self.assertFalse(body["add_special_tokens"])
        self.assertFalse(body["ignore_eos"])
        self.assertEqual(body["stop"], [])
        self.assertTrue(self.body["narwhal_continuation"])

    def test_ordinary_requests_do_not_require_policy_or_qualification(self):
        for flag in (None, False):
            body = {"prompt": "ordinary text", "temperature": 0.8}
            if flag is not None:
                body["narwhal_continuation"] = flag
            normal, enabled = prepare_request(
                "/v1/chat/completions", body, ContinuationPolicy(), None
            )
            self.assertFalse(enabled)
            self.assertEqual(normal, {"prompt": "ordinary text", "temperature": 0.8})

    def test_flag_requires_exact_boolean_at_http_and_direct_entry(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value):
                body = {**self.body, "narwhal_continuation": value}
                self.assertIsNotNone(completion_body_error(body))
                with self.assertRaises(ContinuationRequestError):
                    self.prepare(body)

    def test_rejects_chat_text_batches_nested_arrays_and_invalid_ids(self):
        with self.assertRaises(ContinuationRequestError):
            self.prepare(endpoint="/v1/chat/completions")
        for prompt in ("text", ["text"], [[1]], [], [True], [-1], [1.0], [256], [2**64]):
            with self.subTest(prompt=prompt), self.assertRaises(ContinuationRequestError):
                self.prepare({**self.body, "prompt": prompt})

    def test_original_output_limit_is_explicit_positive_and_context_bounded(self):
        for output in (None, True, 0, -1, 2.0, 13, 33):
            with self.subTest(output=output), self.assertRaises(ContinuationRequestError):
                self.prepare({**self.body, "max_tokens": output})
        missing = {key: value for key, value in self.body.items() if key != "max_tokens"}
        with self.assertRaises(ContinuationRequestError):
            self.prepare(missing)
        with self.assertRaises(ContinuationRequestError):
            self.prepare({**self.body, "prompt": [1] * 21})

    def test_rejects_stateful_sampling_and_unqualified_extensions(self):
        changes = (
            ("temperature", math.nan),
            ("temperature", math.inf),
            ("temperature", 10**400),
            ("temperature", True),
            ("repetition_penalty", 1.1),
            ("presence_penalty", 0.1),
            ("frequency_penalty", 0.1),
            ("min_tokens", 1),
            ("ignore_eos", True),
            ("n", 2),
            ("n", True),
            ("stop", "private stop text"),
            ("stop_token_ids", [8]),
            ("stop_token_ids", [True]),
            ("return_token_ids", 1),
            ("stream", False),
            ("stream_interval", 2),
            ("stream_options", {"continuous_usage_stats": True}),
            ("stream_options", {"include_usage": 1}),
            ("logprobs", 1),
            ("messages", []),
            ("private extension content", {"sensitive": "value"}),
        )
        for key, value in changes:
            with self.subTest(field=key):
                with self.assertRaises(ContinuationRequestError) as caught:
                    self.prepare({**self.body, key: value})
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("sensitive", str(caught.exception))

    def test_qualified_token_stop_and_final_usage_are_supported(self):
        body, enabled = self.prepare(
            {
                **self.body,
                "stop_token_ids": [7],
                "return_token_ids": True,
                "stream_options": {"include_usage": True},
            }
        )
        self.assertTrue(enabled)
        self.assertEqual(body["stop_token_ids"], [7])
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_disabled_and_unavailable_are_distinct(self):
        with self.assertRaisesRegex(ContinuationRequestError, "disabled"):
            prepare_request("/v1/completions", self.body, ContinuationPolicy(), None)
        with self.assertRaises(ContinuationUnavailable):
            prepare_request("/v1/completions", self.body, self.policy, None)
