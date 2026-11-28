"""The routes that change membership or lifecycle need the router's control token, and the
engine key goes to no engine before its attestation verifies."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.runtime import state as handoff_state
from narwhal.serving import app as serving_app
from narwhal.serving import engines_api
from narwhal.serving.app import create_app
from narwhal.serving.control_token import (
    ROUTER_TOKEN_ENV,
    authorized,
    read_token,
    token_problem,
)
from tests import CONTROL_HEADERS, CONTROL_TOKEN
from tests.fixtures import fleet

KEY_VARIABLE = "NARWHAL_TEST_ENGINE_KEY"
KEY = "engine-key-" + "4b" * 24
ATTACKER = "attacker.invalid"
JOIN = {
    "iid": "e6",
    "url": f"http://{ATTACKER}:8000",
    "role": "decode",
    "attestation_url": f"http://{ATTACKER}:8010/v1/attestation",
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


def unexpected(request):
    raise AssertionError(f"a refused request reached {request.url}")


class TokenTests(unittest.TestCase):
    def test_a_token_needs_32_random_bytes(self):
        self.assertIsNone(token_problem("a" * 64))
        self.assertIsNone(token_problem("A_-" * 15))
        for short in ("a" * 63, "A" * 42, "a b" * 30, "+" * 64):
            with self.subTest(short=short):
                self.assertIn(ROUTER_TOKEN_ENV, token_problem(short) or "")
        self.assertEqual(read_token({}), "")
        with self.assertRaisesRegex(ValueError, "at least 32 random bytes"):
            read_token({ROUTER_TOKEN_ENV: "short"})

    def test_only_the_bearer_scheme_with_the_exact_token_is_authorized(self):
        self.assertTrue(authorized(f"Bearer {CONTROL_TOKEN}", CONTROL_TOKEN))
        self.assertTrue(authorized(f"bearer {CONTROL_TOKEN}", CONTROL_TOKEN))
        self.assertFalse(authorized("Bearer ", ""))
        for name, headers in FORGED.items():
            with self.subTest(name):
                self.assertFalse(authorized(headers.get("Authorization"), CONTROL_TOKEN))

    def test_the_router_refuses_a_weak_token_and_one_equal_to_the_engine_key(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            with (
                patch.dict(os.environ, {ROUTER_TOKEN_ENV: "a" * 32}),
                self.assertRaisesRegex(ValueError, "at least 32 random bytes"),
            ):
                create_app(cfg)
            with self.assertRaisesRegex(ValueError, "at least 32 random bytes"):
                create_app(cfg, control_token="short")
            cfg.engine_api_key_env = KEY_VARIABLE
            with (
                patch.dict(os.environ, {KEY_VARIABLE: CONTROL_TOKEN}),
                self.assertRaisesRegex(ValueError, "equals the engine API key"),
            ):
                create_app(cfg)

    def test_a_standby_needs_the_token(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            with self.assertRaisesRegex(ValueError, ROUTER_TOKEN_ENV):
                create_app(
                    cfg,
                    standby_of="http://primary.invalid",
                    lease_path=Path(folder) / "lease",
                    control_token="",
                )


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
        self.router.lifecycle_transport = httpx.MockTransport(unexpected)

    async def requests(self, headers, app=None):
        transport = httpx.ASGITransport(app=app or self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            return [
                (path, await client.request(method.upper(), path, headers=headers, **kwargs))
                for method, path, kwargs in CONTROL_ROUTES
            ]

    async def assert_refused(self):
        with (
            patch.object(engines_api, "join_engine", AsyncMock()) as join,
            patch.object(engines_api, "begin_leave", AsyncMock()) as leave,
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
        leave.assert_not_awaited()
        capture.assert_not_awaited()
        readmit.assert_not_awaited()
        self.assertEqual(self.router.membership.generation, 0)
        self.assertEqual(self.router.lifecycle.records, {})
        self.assertEqual(self.router.scheduler.draining, set())

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

    async def test_a_router_without_a_token_refuses_control_and_names_the_setting(self):
        app = create_app(self.cfg, control_token="")
        self.addAsyncCleanup(app.state.router.engines.aclose)
        for headers in ({}, CONTROL_HEADERS):
            for path, response in await self.requests(headers, app):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertIn(ROUTER_TOKEN_ENV, response.text)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            handoff = await client.get("/narwhal/handoff")
            self.assertEqual(handoff.status_code, 403)
            self.assertIn(ROUTER_TOKEN_ENV, handoff.text)
            self.assertEqual((await client.get("/narwhal/engines")).status_code, 200)

    async def send_body(self, path, headers, size, chunk=1024 * 1024):
        """Stream a `size`-byte body to `path`; return the status and the bytes read."""
        read = 0
        status = {}

        async def receive():
            nonlocal read
            if read >= size:
                return {"type": "http.disconnect"}
            read += chunk
            return {"type": "http.request", "body": b" " * chunk, "more_body": read < size}

        async def send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json"), *headers],
            "server": ("router", 80),
            "client": ("client", 1),
        }
        await self.app(scope, receive, send)
        return status["code"], read

    async def test_a_control_request_without_the_token_is_refused_before_its_body_is_read(self):
        # A route reads its body model before its own token dependency runs, so the ingress
        # checks the token first: no unauthenticated body is read at all.
        for path in ("/narwhal/lifecycle/drain", "/narwhal/lifecycle/readmit", "/narwhal/engines"):
            with self.subTest(path=path):
                status, read = await self.send_body(path, [], 64 * 1024 * 1024)
                self.assertEqual((status, read), (401, 0))

    async def test_a_control_body_above_64_kib_is_refused_once_it_passes_the_limit(self):
        token = [(b"authorization", f"Bearer {CONTROL_TOKEN}".encode())]
        for path in ("/narwhal/lifecycle/drain", "/narwhal/lifecycle/readmit", "/narwhal/engines"):
            with self.subTest(path=path):
                status, read = await self.send_body(path, token, 64 * 1024 * 1024, chunk=16 * 1024)
                self.assertEqual(status, 413)
                self.assertLessEqual(read, 64 * 1024 + 16 * 1024)

    async def test_reads_need_no_token(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            for path in ("/health", "/narwhal/engines", "/narwhal/lifecycle", "/narwhal/state"):
                with self.subTest(path=path):
                    self.assertEqual((await client.get(path)).status_code, 200)

    async def test_the_handoff_is_signed_with_the_token_it_never_sends(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            response = await client.get("/narwhal/handoff")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(CONTROL_TOKEN, response.text)
        signature = response.headers[handoff_state.SIGNATURE_HEADER]
        doc = handoff_state.verified(response.content, signature, CONTROL_TOKEN)
        self.assertEqual(doc["engines"], ["e0", "e3"])
        forged = json.loads(response.content)
        forged["engines"] = ["e0"]
        body = json.dumps(forged).encode()
        for refused in (
            (response.content, signature, "0" * 64),
            (body, signature, CONTROL_TOKEN),
            (response.content, None, CONTROL_TOKEN),
        ):
            with (
                self.subTest(refused=refused[1:]),
                self.assertRaises(handoff_state.HandoffRefused),
            ):
                handoff_state.verified(*refused)

    async def test_openapi_lists_the_refusals_of_each_control_route(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            paths = (await client.get("/openapi.json")).json()["paths"]
        for method, path, _ in CONTROL_ROUTES:
            route = path.replace("/e0", "/{iid}")
            with self.subTest(path=route):
                self.assertIn("401", paths[route][method]["responses"])
                self.assertIn("403", paths[route][method]["responses"])


class AttackerHost:
    """A host that answers like an engine and its sidecar, and records every request."""

    def __init__(self, members):
        self.members = members
        self.log = []

    def __call__(self, request):
        if request.url.host != ATTACKER:
            return self.members(request)
        self.log.append((request.method, request.url.path, request.headers.get("authorization")))
        path = request.url.path
        if path == "/version":
            return httpx.Response(200, json={"version": "1.0.0"})
        if path == "/metrics":
            return httpx.Response(200, text="process_start_time_seconds 200.0\n")
        if path == "/v1/process":
            return httpx.Response(200, json={"process_start_time_seconds": 200.0})
        if path == "/v1/attestation":
            # A copy of what a member might serve, for another contract.
            return httpx.Response(
                200,
                json={
                    "schema": "narwhal.attestation",
                    "schema_version": 1,
                    "contract": {"engine_version": "0.0.0"},
                    "sources": {},
                    "engine": {"version": "1.0.0", "process_start_time_seconds": 200.0},
                    "attestation_digest": "sha256:" + "0" * 64,
                },
            )
        return httpx.Response(404)

    def keyed(self):
        return [entry for entry in self.log if entry[2] is not None]


class ForgedJoinTests(unittest.IsolatedAsyncioTestCase):
    """A join that names an attacker's host never hands that host the engine key: without the
    token the router refuses it, and with the token the attestation fails before any request
    to the host carries the key."""

    async def join(self, backend, headers):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = fleet(root)
            cfg.backend = backend
            cfg.connector = {"vllm": "nixl", "sglang": "mooncake"}[backend]
            cfg.engine_api_key_env = KEY_VARIABLE
            cfg.state_path = root / "handoff.json"
            attacker = AttackerHost(httpx.MockTransport(lambda request: httpx.Response(503)))
            transport = httpx.MockTransport(attacker)
            with patch.dict(os.environ, {KEY_VARIABLE: KEY}):
                app = create_app(cfg, lifecycle_transport=transport)
                router = app.state.router
                router.lifecycle_transport = transport
                try:
                    client = httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://router"
                    )
                    async with client:
                        response = await client.post("/narwhal/engines", json=JOIN, headers=headers)
                finally:
                    await router.engines.aclose()
            self.assertNotIn("e6", router.membership.ids)
            return response, attacker

    async def test_an_unauthenticated_join_never_reaches_the_attacker(self):
        for name, headers in FORGED.items():
            with self.subTest(name):
                response, attacker = await self.join("vllm", headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(attacker.log, [])

    async def test_an_authenticated_join_sends_no_key_before_the_attestation_verifies(self):
        for backend in ("vllm", "sglang"):
            with self.subTest(backend=backend):
                response, attacker = await self.join(backend, CONTROL_HEADERS)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("attestation", response.json()["error"])
                self.assertTrue(attacker.log)
                self.assertEqual(attacker.keyed(), [], attacker.log)


if __name__ == "__main__":
    unittest.main()
