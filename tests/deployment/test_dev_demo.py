import io
import json
import threading
from unittest.mock import patch

import httpx

from narwhal.dev import cli, demo, lifecycle
from tests.deployment.test_dev_ask import chat_stream
from tests.deployment.test_dev_direct import DirectInstance
from tests.deployment.test_dev_watch import STATE, metrics


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class DemoTests(DirectInstance):
    def setUp(self):
        super().setUp()
        self.init()
        self.run_directory = self.root / "run-1"
        self.run_directory.mkdir()
        lifecycle.write(self.run_directory / "fleet.json", {"model": "served"})
        self.state("launched")
        self.bodies: list[dict] = []
        self.lock = threading.Lock()

    def state(self, phase: str) -> None:
        document = {"run": str(self.run_directory), "phase": phase, "processes": []}
        lifecycle.write(self.root / "lifecycle.json", document)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self.lock:
            self.bodies.append(body)
        if body["max_tokens"] == demo.PROMPT_TOKENS and len(self.bodies) % 5 == 0:
            return httpx.Response(503, json={"error": {"code": "not_ready"}})
        return chat_stream(["Tok", "yo"], 2)

    def fetch(self, url: str):
        return STATE, metrics(10, 400, (4, 10, 10))

    def run_demo(self, out=None) -> dict:
        with patch.object(demo, "RETRY_PAUSE_S", 0.0):
            return demo.run(
                self.root,
                seconds=0.2,
                concurrency=3,
                interval=0.01,
                fetch=self.fetch,
                transport=httpx.MockTransport(self.handle),
                out=out or io.StringIO(),
                sleep=lambda seconds: None,
            )

    def test_long_replies_then_long_prompts_and_the_roles_they_leave(self):
        result = self.run_demo()
        self.assertEqual(result["status"], "completed")
        replies, prompts = result["phases"]
        self.assertEqual((replies["name"], prompts["name"]), ("long replies", "long prompts"))
        self.assertGreater(replies["requests"], 0)
        self.assertGreater(prompts["requests"], 0)
        self.assertEqual(replies["failed"], 0)
        self.assertGreater(prompts["failed"], 0)
        self.assertEqual(result["requests"], len(self.bodies))
        self.assertEqual(result["failed"], prompts["failed"])
        self.assertEqual(result["split"], {"prefill": 1, "decode": 3})
        self.assertEqual(result["role_switches"], 7)
        # Long replies cycle the short prompts; long prompts ask for two tokens.
        first, last = self.bodies[0], self.bodies[-1]
        self.assertEqual(first["max_tokens"], demo.REPLY_TOKENS)
        self.assertEqual(first["model"], "served")
        self.assertEqual(last["max_tokens"], demo.PROMPT_TOKENS)
        self.assertEqual(last["messages"][0]["content"], demo.LONG_PROMPT)
        self.assertGreater(len(demo.LONG_PROMPT.split()), 400)

    def test_a_terminal_shows_the_live_view_with_the_load_until_it_settles(self):
        out = Terminal()
        with patch.object(demo, "SETTLE_S", 0.0):
            result = self.run_demo(out)
        drawn = out.getvalue()
        self.assertEqual(result["status"], "completed")
        for text in ("narwhal dev demo", "phase 1 of 2: long replies", "3 in flight"):
            self.assertIn(text, drawn)
        self.assertIn("\x1b[?1049l", drawn)

    def test_the_demo_needs_a_launched_instance_and_positive_settings(self):
        with self.assertRaisesRegex(ValueError, "must be positive"):
            demo.run(self.root, seconds=0)
        self.state("stopped")
        with self.assertRaisesRegex(ValueError, "requires a launched instance"):
            demo.run(self.root)
        with self.assertRaises(SystemExit) as stopped:
            cli._main(["dev", "demo", "--instance", str(self.root), "--concurrency", "0"])
        self.assertEqual(stopped.exception.code, 2)

    def test_the_command_starts_a_stopped_fleet_and_fails_on_failed_requests(self):
        self.state("stopped")
        outcome = {"status": "completed", "failed": 2, "requests": 9}
        with (
            patch.object(lifecycle, "up") as up,
            patch.object(demo, "run", return_value=outcome) as ran,
        ):
            code = cli._main(["dev", "demo", "--instance", str(self.root), "--phase-seconds", "5"])
        up.assert_called_once()
        self.assertEqual(ran.call_args.kwargs["seconds"], 5.0)
        self.assertEqual(code, 1)
        self.state("launched")
        outcome["failed"] = 0
        with (
            patch.object(lifecycle, "up") as up,
            patch.object(demo, "run", return_value=outcome),
        ):
            code = cli._main(["dev", "demo", "--instance", str(self.root)])
        up.assert_not_called()
        self.assertEqual(code, 0)
