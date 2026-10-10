import io
import unittest
from contextlib import redirect_stderr

import httpx

from narwhal.cli_errors import failure


class FailureTests(unittest.TestCase):
    def test_an_error_without_a_message_names_its_type(self):
        request = httpx.Request("POST", "http://engine:8000/v1/completions")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = failure(
                "narwhal-profile", "profile fleet f", httpx.ReadTimeout("", request=request), 1
            )
        self.assertEqual(status, 1)
        self.assertEqual(
            stderr.getvalue(),
            "narwhal-profile: profile fleet f: "
            "POST http://engine:8000/v1/completions: ReadTimeout\n",
        )
