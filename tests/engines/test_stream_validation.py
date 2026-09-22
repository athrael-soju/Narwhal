"""Validate chat delta shapes before exact-token accounting."""

import json
import unittest

from narwhal.engines.stream import event_choices, sse_token_ids


class StreamValidationTests(unittest.TestCase):
    def test_non_object_deltas_are_rejected(self):
        for delta in ([], "", 0, False, [1], "text", 1, True):
            with self.subTest(delta=delta):
                event = {"choices": [{"delta": delta, "token_ids": [42]}]}
                with self.assertRaisesRegex(ValueError, "SSE delta must be an object"):
                    event_choices(event)
                self.assertIsNone(sse_token_ids("data: " + json.dumps(event)))

    def test_absent_null_and_object_deltas_preserve_token_ids(self):
        for fields in ({}, {"delta": None}, {"delta": {}}, {"delta": {"content": "x"}}):
            with self.subTest(fields=fields):
                choice = {**fields, "token_ids": [42]}
                event = {"choices": [choice]}
                self.assertEqual(event_choices(event), [choice])
                self.assertEqual(sse_token_ids("data: " + json.dumps(event)), (42,))
