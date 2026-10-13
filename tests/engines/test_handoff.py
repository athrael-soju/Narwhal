"""One handoff orchestration runs both leg orders that connectors declare."""

import asyncio
import re
import unittest
from pathlib import Path

from narwhal.backends import load, names
from narwhal.engines.connector import PrefillResult
from narwhal.engines.handoff import HandoffLegs
from tests.wire import FakeRendezvous

SRC = Path(__file__).resolve().parents[2] / "src" / "narwhal"
LAUNCH = {"bootstrap": {"bootstrap_host": "192.0.2.11", "bootstrap_port": 5600}}


def connectors():
    for name in names():
        backend = load(name)
        for key, kv in backend.connectors.items():
            yield f"{name}/{key}", kv


class HandoffLegsTests(unittest.IsolatedAsyncioTestCase):
    def test_every_connector_declares_its_leg_order_bound_and_diagnostics(self):
        for label, kv in connectors():
            with self.subTest(connector=label):
                self.assertIn(kv.leg_order, ("sequential", "concurrent"))
                self.assertTrue(
                    all(isinstance(name, str) and name for name in kv.diagnostics_fields)
                )
                bound = kv.first_token_bound(None)
                if kv.concurrent:
                    self.assertIsNotNone(bound)
                    self.assertIsInstance(kv.rendezvous(LAUNCH), dict)
                else:
                    self.assertIsNone(bound)
                    self.assertEqual(kv.first_token_bound(5.0), 5.0)
                    self.assertIsNone(kv.rendezvous(LAUNCH))

    async def test_a_sequential_prefill_finishes_before_decode(self):
        kv = load("vllm").connector("nixl")
        result = PrefillResult(
            "nixl", "http://p", "/v1/completions", None, '{"remote_engine_id": "p"}'
        )
        seen = []

        async def leg(rendezvous):
            seen.append(rendezvous)
            return result

        handoff = await HandoffLegs(kv, LAUNCH).start(leg)
        self.assertEqual(seen, [None])
        self.assertIsNone(handoff.task)
        self.assertIs(handoff.result, result)
        self.assertIsNotNone(handoff.prefill_seconds)
        self.assertEqual(handoff.diagnostics()["remote_engine_id"], "p")
        self.assertIsNone(await handoff.settle(cancel=True))

    async def test_a_concurrent_prefill_runs_while_decode_streams(self):
        kv = FakeRendezvous()
        release = asyncio.Event()

        async def leg(rendezvous):
            await release.wait()
            raise RuntimeError(f"prefill failed in room {rendezvous['room']}")

        handoff = await HandoffLegs(kv, LAUNCH).start(leg)
        self.assertIsNotNone(handoff.task)
        self.assertIsNone(handoff.failure())
        decode = handoff.decode_body({"prompt": "p"}, url="http://d", endpoint="/v1/completions")
        self.assertEqual(decode["room"], handoff.rendezvous["room"])
        release.set()
        failure = await handoff.settle(cancel=False)
        self.assertIsInstance(failure, RuntimeError)
        self.assertIs(handoff.failure(), failure)

    def test_no_shared_module_selects_behaviour_by_connector_class(self):
        names_ = re.compile(r"\b(KvConnector|RendezvousConnector)\b")
        found = [
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if path.relative_to(SRC).parts[0] != "backends"
            and path.relative_to(SRC) != Path("engines/connector.py")
            and names_.search(path.read_text())
        ]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
