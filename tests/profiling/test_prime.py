"""Warm each engine with discarded requests before its sweep."""

import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load as load_backend
from narwhal.profiling.probe import cli as profile_cli
from narwhal.profiling.probe import instance as instance_probe
from narwhal.profiling.probe import prime
from narwhal.profiling.probe.instance import profile_instance
from narwhal.profiling.probe.prime import PRIME_PREFIX, prime_engine
from narwhal.profiling.probe.sweep import Sweep
from narwhal.types import Role
from tests.profiling.fixtures import patched_profile_sweeps

BACKEND = load_backend("vllm")
PREFILL = [(x, 0.001 * x + 0.01) for x in (1, 10, 100)]
DECODE = [(r, k, 0.001 * r + 0.000001 * k + 0.01) for r in (1, 4, 16) for k in (100, 1000, 10000)]


class PrimeEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_the_requests_with_a_prompt_apart_from_the_sweep(self):
        bodies = []

        def answer(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"text": "x"}]})

        sized = AsyncMock(return_value=(PRIME_PREFIX + "benchmark " * 8, 16))
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            with patch.object(prime, "make_prompt", sized):
                await prime_engine(client, "http://e", "m", BACKEND.dialect, 3, 16, 5)
                await prime_engine(client, "http://e", "m", BACKEND.dialect, 0, 16, 5)
        self.assertEqual(sized.await_args.kwargs["prefix"], PRIME_PREFIX)
        self.assertEqual(sized.await_args.args[3], 16)
        self.assertEqual(len(bodies), 3)
        self.assertTrue(all(body["prompt"].startswith(PRIME_PREFIX) for body in bodies))
        self.assertEqual({body["max_tokens"] for body in bodies}, {5})
        # Each request takes a fresh cache salt, as the cold sweep's requests do.
        self.assertEqual(len({body["cache_salt"] for body in bodies}), 3)

    async def test_a_failed_request_stops_the_profile(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(503, text="busy"))
        async with httpx.AsyncClient(transport=transport) as client:
            with (
                patch.object(prime, "make_prompt", AsyncMock(return_value=("p", 1))),
                self.assertRaisesRegex(RuntimeError, r"warm-up request failed on http://e \(503"),
            ):
                await prime_engine(client, "http://e", "m", BACKEND.dialect, 1, 1, 3)


class ProfileWarmupTests(unittest.IsolatedAsyncioTestCase):
    async def profile(self, sweep, roles=None):
        order = []

        async def serve_as(role):
            order.append(role)

        warm = AsyncMock(side_effect=lambda *args, **kwargs: order.append("warm"))
        with (
            patched_profile_sweeps(PREFILL, DECODE, hits=[7, 7, 7]),
            patch.object(instance_probe, "prime_engine", warm),
        ):
            instance_probe.probe_prefill.side_effect = lambda *a, **k: (
                order.append("prefill") or PREFILL
            )
            await profile_instance(
                None,
                "e",
                "http://e",
                "stub",
                BACKEND.dialect,
                sweep,
                metrics=BACKEND.metrics,
                max_model_len=1024,
                roles=serve_as,
            )
        return order, warm

    async def test_each_role_serves_the_warm_up_before_the_sweep(self):
        sweep = Sweep(prefill_lens=(128, 512, 896), decode_tokens=64, warmup_requests=4)
        order, warm = await self.profile(sweep)
        self.assertEqual(order[:5], [Role.PREFILL, "warm", Role.DECODE, "warm", Role.PREFILL])
        self.assertEqual(order[5], "prefill")
        self.assertEqual(warm.await_args.args[4:7], (4, 128, 64))

    async def test_the_warm_up_input_fits_the_context_with_its_output(self):
        sweep = Sweep(prefill_lens=(1000, 1010, 1020), decode_tokens=64, warmup_requests=1)
        _, warm = await self.profile(sweep)
        self.assertEqual(warm.await_args.args[5], 1024 - 64 - 1)

    async def test_without_warm_up_the_sweep_starts_at_once(self):
        order, warm = await self.profile(Sweep(prefill_lens=(128, 512, 896)))
        warm.assert_not_awaited()
        self.assertEqual(order[:2], [Role.PREFILL, "prefill"])

    def test_the_cli_takes_a_warm_up_count(self):
        with (
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as exit,
        ):
            profile_cli.main(["--fleet", "tests/data/fleet.json", "--warmup-requests", "-1"])
        self.assertEqual(exit.exception.code, 2)
        self.assertIn("--warmup-requests must be at least 0", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
