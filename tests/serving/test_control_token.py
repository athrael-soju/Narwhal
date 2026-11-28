"""The routes that change membership, trust or lifecycle need the router's control token."""

import functools
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.serving import app as serving_app
from narwhal.serving import engines_api
from narwhal.serving.app import create_app
from narwhal.serving.control_token import ROUTER_TOKEN_ENV, authorized, token_problem
from narwhal.serving.router.routing import NarwhalRouter
from tests import CONTROL_HEADERS, CONTROL_TOKEN
from tests.fixtures import fleet
from tests.runtime.test_credential_order import (
    CANDIDATE,
    KEY,
    KEY_VARIABLE,
    Fleet,
    StubEngine,
)

JOIN = {
    "iid": CANDIDATE.iid,
    "url": CANDIDATE.url,
    "role": CANDIDATE.role.value,
    "attestation_url": CANDIDATE.attestation_url,
}
# Every control route, with a body it would otherwise act on.
CONTROL_ROUTES = (
    ("post", "/narwhal/engines", {"json": JOIN}),
    ("delete", "/narwhal/engines/e0", {}),
    ("post", "/narwhal/lifecycle/drain", {"json": {"engines": ["e0"]}}),
    ("post", "/narwhal/lifecycle/readmit", {"json": {"engines": ["e0"]}}),
)
# Requests that carry no control token, or another one.
FORGED = {
    "no header": {},
    "an empty bearer": {"Authorization": "Bearer "},
    "another token": {"Authorization": "Bearer " + "0" * 64},
    "the token in another scheme": {"Authorization": "Basic " + CONTROL_TOKEN},
    "the token with a suffix": {"Authorization": f"Bearer {CONTROL_TOKEN}x"},
    "the engine key": {"Authorization": f"Bearer {KEY}"},
}


class TokenTests(unittest.TestCase):
    def test_a_token_needs_32_random_bytes(self):
        self.assertIsNone(token_problem("a" * 64))
        self.assertIsNone(token_problem("A_-" * 15))
        for short in ("", "a" * 63, "A" * 42, "a b" * 30, "+" * 64):
            with self.subTest(short=short):
                self.assertIn(ROUTER_TOKEN_ENV, token_problem(short) or "")

    def test_only_the_bearer_scheme_with_the_exact_token_is_authorized(self):
        self.assertTrue(authorized(f"Bearer {CONTROL_TOKEN}", CONTROL_TOKEN))
        self.assertTrue(authorized(f"bearer {CONTROL_TOKEN}", CONTROL_TOKEN))
        for name, headers in FORGED.items():
            with self.subTest(name):
                self.assertFalse(authorized(headers.get("Authorization"), CONTROL_TOKEN))

    def test_the_router_refuses_to_start_without_a_token(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            with patch.dict(os.environ, clear=False):
                del os.environ[ROUTER_TOKEN_ENV]
                with self.assertRaisesRegex(ValueError, ROUTER_TOKEN_ENV):
                    create_app(cfg)
                os.environ[ROUTER_TOKEN_ENV] = "a" * 32
                with self.assertRaisesRegex(ValueError, "at least 32 random bytes"):
                    create_app(cfg)
            with self.assertRaisesRegex(ValueError, "control_token"):
                create_app(cfg, control_token="short")


class ControlRouteTests(unittest.IsolatedAsyncioTestCase):
    """A primary and a standby both refuse a control request without the token before acting,
    and say nothing about why."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.cfg = fleet(root)
        self.cfg.state_path = root / "handoff.json"
        self.app = create_app(self.cfg)
        self.router = self.app.state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        # Any engine call fails the test: a refused request reaches no engine.
        self.engines = httpx.MockTransport(self.unexpected)
        self.router.lifecycle_transport = self.engines

    def unexpected(self, request):
        raise AssertionError(f"a refused request reached {request.url}")

    async def requests(self, headers):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            return [
                (path, await client.request(method.upper(), path, headers=headers, **kwargs))
                for method, path, kwargs in CONTROL_ROUTES
            ]

    async def assert_refused(self):
        with (
            patch.object(engines_api, "join_engine", AsyncMock()) as join,
            patch.object(serving_app, "capture_process_identities", AsyncMock()) as capture,
            patch.object(serving_app, "validate_readmission", AsyncMock()) as readmit,
        ):
            for name, headers in FORGED.items():
                for path, response in await self.requests(headers):
                    with self.subTest(name, path=path):
                        self.assertEqual(response.status_code, 401, response.text)
                        self.assertEqual(response.content, b"")
                        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        join.assert_not_awaited()
        capture.assert_not_awaited()
        readmit.assert_not_awaited()
        self.assertEqual(self.router.membership.generation, 0)
        self.assertEqual(self.router.lifecycle.records, {})

    async def test_the_primary_refuses_a_control_request_without_the_token(self):
        await self.assert_refused()

    async def test_the_standby_refuses_a_control_request_without_the_token(self):
        self.router.standby = True
        await self.assert_refused()

    async def test_the_token_reaches_each_route(self):
        self.router.standby = True
        for path, response in await self.requests(CONTROL_HEADERS):
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 503, response.text)

    async def test_reads_need_no_token(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            for path in ("/health", "/narwhal/engines", "/narwhal/lifecycle", "/narwhal/state"):
                with self.subTest(path=path):
                    self.assertEqual((await client.get(path)).status_code, 200)

    async def test_openapi_lists_the_refusal_of_each_control_route(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            paths = (await client.get("/openapi.json")).json()["paths"]
        for method, path, _ in CONTROL_ROUTES:
            route = path.replace("/e0", "/{iid}")
            with self.subTest(path=route):
                self.assertIn("401", paths[route][method]["responses"])


class ForgedJoinTests(unittest.IsolatedAsyncioTestCase):
    """A host that serves a copy of a member's attestation cannot join without the token, so
    the router never sends it the engine key, whatever route the backend reads identity from."""

    async def test_a_forged_join_never_receives_the_engine_key(self):
        connectors = {
            "vllm": "nixl",
            "sglang": "mooncake",
            "trtllm": "nixl",
            "llamacpp": "slot-file",
        }
        for backend, connector in connectors.items():
            with self.subTest(backend=backend):
                await self.attack(backend, connector)

    async def attack(self, backend, connector):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = fleet(root)
            cfg.backend = backend
            cfg.connector = connector
            cfg.engine_api_key_env = KEY_VARIABLE
            cfg.state_path = root / "handoff.json"
            guarded = backend in ("sglang", "llamacpp")
            # The attacker's host attests the fleet's own contract, copied from a member.
            attacker = StubEngine(CANDIDATE, cfg.engine_contract, 200.0, guarded=guarded)
            stubs = Fleet(
                [StubEngine(spec, cfg.engine_contract, 100.0) for spec in cfg.engines] + [attacker]
            )
            built = functools.partial(NarwhalRouter, transport=stubs.transport)
            with (
                patch.dict(os.environ, {KEY_VARIABLE: KEY}),
                patch.object(serving_app, "NarwhalRouter", built),
            ):
                app = create_app(cfg, lifecycle_transport=stubs.transport)
                router = app.state.router
                try:
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://router"
                    ) as client:
                        statuses = {
                            name: (
                                await client.post("/narwhal/engines", json=JOIN, headers=headers)
                            ).status_code
                            for name, headers in FORGED.items()
                        }
                finally:
                    await router.engines.aclose()
            self.assertEqual(attacker.keyed(), [], attacker.log)
            self.assertEqual(attacker.log, [])
            self.assertEqual(statuses, dict.fromkeys(FORGED, 401))
            self.assertNotIn(CANDIDATE.iid, router.membership.ids)


if __name__ == "__main__":
    unittest.main()
