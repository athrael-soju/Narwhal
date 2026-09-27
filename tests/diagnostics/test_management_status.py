"""Keep router inspection bounded and distinguish service health from collection failure."""

import asyncio
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime
from unittest.mock import patch

import httpx

from narwhal.contracts import LIFECYCLE, STATE, versioned
from narwhal.diagnostics import management_status as status
from narwhal.diagnostics.bundle import Redactor


class InterruptedBody(httpx.AsyncByteStream):
    def __init__(self, prefix):
        self.prefix = prefix
        self.closed = False

    async def __aiter__(self):
        yield self.prefix
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class SourceBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class RedactorDeadlineTests(unittest.TestCase):
    def test_long_unbroken_text_finishes_and_preserves_credential_detection(self):
        script = """
import json
from narwhal.diagnostics.bundle import Redactor

redactor = Redactor(False)
line = b'a' * 262144
assert redactor.body(line) == line
document = {'detail': line.decode()}
assert json.loads(redactor.body(json.dumps(document).encode())) == document
assert redactor.body(line + b'_api_key=private-value\\ncontinued-private-value') == b'[REDACTED]'
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_prefixed_quoted_and_embedded_credential_labels_keep_their_redaction_boundary(self):
        redactor = Redactor(False)
        for source, expected in (
            ('prefix "service_api_key"="private-value', "prefix [REDACTED]"),
            ("prefix --service-api-key=private-value", "prefix [REDACTED]"),
            ("prefix userpassword=private-value", "prefix user[REDACTED]"),
            ("prefix 'credential'=private-value", "prefix [REDACTED]"),
            ("prefix AUTHORIZATION: private-value", "prefix [REDACTED]"),
        ):
            with self.subTest(source=source):
                self.assertEqual(redactor.text(source), expected)


class ManagementStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []

    def response(self, request):
        self.requests.append(request)
        route = request.url.path.removeprefix("/prefix")
        if route == "/narwhal/state":
            data = versioned(STATE, {"served": 2, "ha": {"ready": True}})
        elif route == "/narwhal/lifecycle":
            data = versioned(LIFECYCLE, {"engines": {}, "error": ""})
        else:
            data = {"status": "ok"}
        return httpx.Response(200, json=data)

    async def observe(self, handler=None, **kwargs):
        return await status.observe_router(
            "http://router.test/prefix/",
            redactor=kwargs.pop("redactor", Redactor(False)),
            transport=httpx.MockTransport(handler or self.response),
            **kwargs,
        )

    async def test_get_routes_keep_healthy_and_unhealthy_readiness_observations(self):
        def handler(request):
            if request.url.path.endswith("/ready"):
                self.requests.append(request)
                return httpx.Response(503, json={"ready": False, "reason": "no available engines"})
            return self.response(request)

        observations = await self.observe(handler)
        self.assertEqual([row["source"] for row in observations], list(status.ROUTES))
        self.assertEqual(
            [request.url.path for request in self.requests],
            ["/prefix" + route for route in status.ROUTES],
        )
        self.assertEqual({request.method for request in self.requests}, {"GET"})
        self.assertEqual({row["status"] for row in observations}, {"ok"})
        ready = observations[1]
        self.assertEqual(ready["http_status"], 503)
        self.assertEqual(ready["data"]["reason"], "no available engines")
        for row in observations:
            self.assertIsNone(row["artifact_id"])
            self.assertTrue(row["observed_at"].endswith("Z"))
            datetime.fromisoformat(row["observed_at"])
            self.assertNotIn("raw_body", row)

    async def test_missing_endpoint_and_connection_failure_preserve_other_sources(self):
        def handler(request):
            if request.url.path.endswith("/health"):
                return httpx.Response(404, text="route unavailable")
            if request.url.path.endswith("/ready"):
                raise httpx.ConnectError(
                    "https://private.test/?token=hidden-connect-secret", request=request
                )
            return self.response(request)

        observations = await self.observe(handler)
        self.assertEqual(
            [row["status"] for row in observations], ["unavailable", "unavailable", "ok", "ok"]
        )
        self.assertEqual(observations[0]["http_status"], 404)
        self.assertEqual(observations[0]["data"], "route unavailable")
        self.assertIsNone(observations[1]["http_status"])
        self.assertIsNone(observations[1]["data"])
        self.assertNotIn("hidden-connect-secret", json.dumps(observations))
        self.assertNotIn("private.test", json.dumps(observations))

    async def test_redirects_never_contact_the_destination(self):
        for destination in ("http://foreign.test/health", "http://router.test/other"):
            with self.subTest(destination=destination):
                requests = []

                def handler(request, requests=requests, destination=destination):
                    requests.append(str(request.url))
                    return httpx.Response(307, headers={"location": destination}, text="redirect")

                observations = await self.observe(handler)
                self.assertEqual(len(requests), 4)
                self.assertTrue(all("/prefix/" in url for url in requests))
                self.assertEqual({row["status"] for row in observations}, {"unavailable"})
                self.assertEqual({row["http_status"] for row in observations}, {307})

    async def test_source_deadline_includes_streaming_and_keeps_partial_body(self):
        stream = InterruptedBody(b'{"ready":false,"reason":"partial body"')

        def handler(request):
            if request.url.path.endswith("/ready"):
                return httpx.Response(503, stream=stream)
            return self.response(request)

        started = time.monotonic()
        with patch.object(status, "SOURCE_TIMEOUT_S", 0.025):
            observations = await self.observe(handler, timeout_s=1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(observations[1]["status"], "timeout")
        self.assertEqual(observations[1]["http_status"], 503)
        self.assertIn("partial body", observations[1]["data"])
        self.assertEqual(observations[-1]["status"], "ok")
        self.assertTrue(stream.closed)

    async def test_total_deadline_reports_unattempted_routes_without_more_requests(self):
        stream = InterruptedBody(b'{"status":"partially received"')
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, stream=stream)

        started = time.monotonic()
        observations = await self.observe(handler, timeout_s=0.025)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(observations), 4)
        self.assertEqual({row["status"] for row in observations}, {"timeout"})
        self.assertEqual(observations[0]["http_status"], 200)
        self.assertTrue(all(row["http_status"] is None for row in observations[1:]))
        self.assertTrue(stream.closed)

    async def test_source_cap_keeps_only_redacted_prefix_and_closes_stream(self):
        prefix = b'api_key="oversized-private-secret"\n'
        stream = SourceBody([prefix, b"x" * status.MAX_SOURCE_BYTES, b"never retained"])

        def handler(request):
            if request.url.path.endswith("/health"):
                return httpx.Response(200, stream=stream)
            return self.response(request)

        observations = await self.observe(handler)
        row = observations[0]
        self.assertEqual(row["status"], "truncated")
        self.assertEqual(row["error_code"], "source_truncated")
        self.assertEqual(row["raw_body"], b"[REDACTED]")
        self.assertEqual(row["data"], "[REDACTED]")
        self.assertTrue(stream.closed)
        self.assertEqual(observations[-1]["status"], "ok")

    async def test_exact_byte_limit_is_complete_but_one_more_byte_is_truncated(self):
        for extra, expected in ((b"", "ok"), (b"x", "truncated")):
            with self.subTest(extra=extra):
                stream = SourceBody([b"a" * status.MAX_SOURCE_BYTES, extra])

                def handler(request, stream=stream):
                    if request.url.path.endswith("/health"):
                        return httpx.Response(200, stream=stream)
                    return self.response(request)

                observations = await self.observe(handler)
                row = observations[0]
                self.assertEqual(row["status"], expected)
                self.assertEqual(len(row["data"]), status.MAX_SOURCE_BYTES)
                self.assertEqual("raw_body" in row, bool(extra))

    async def test_redaction_removes_known_credentials_and_default_request_content(self):
        def handler(request):
            response = self.response(request)
            data = json.loads(response.content)
            data.update(
                api_key="fixture-api-secret",
                engine_api_key_env="UNUSUAL_ACCESS_VALUE",
                detail="bearer-from-environment",
                prompt="private prompt",
                messages=[{"role": "user", "content": "private message"}],
            )
            return httpx.Response(200, json=data)

        with patch.dict("os.environ", {"UNUSUAL_ACCESS_VALUE": "bearer-from-environment"}):
            observations = await self.observe(handler)
        serialized = json.dumps(observations)
        for private in (
            "fixture-api-secret",
            "bearer-from-environment",
            "private prompt",
            "private message",
        ):
            self.assertNotIn(private, serialized)
        for row in observations:
            self.assertEqual(row["data"]["engine_api_key_env"], "UNUSUAL_ACCESS_VALUE")
            self.assertEqual(row["data"]["api_key"], "[REDACTED]")
            self.assertEqual(row["data"]["prompt"], "[REDACTED]")

    async def test_versioned_routes_reject_unsupported_missing_or_invalid_metadata(self):
        for fields in (
            {"schema": "narwhal.state", "schema_version": 99},
            {"schema": "narwhal.state", "schema_version": True},
            {"schema": "narwhal.state"},
            {"schema": "other.schema", "schema_version": 1},
        ):
            with self.subTest(fields=fields):

                def handler(request, fields=fields):
                    if request.url.path.endswith("/narwhal/state"):
                        return httpx.Response(200, json=fields)
                    return self.response(request)

                observations = await self.observe(handler)
                self.assertEqual(observations[2]["status"], "error")
                self.assertEqual(observations[2]["error_code"], "unsupported_contract")
                self.assertEqual(observations[2]["data"], fields)
                self.assertEqual(observations[-1]["status"], "ok")

    async def test_nonfinite_json_retains_redacted_text_and_other_observations(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e9999"):
            for route in ("/health", "/narwhal/state"):
                with self.subTest(number=number, route=route):
                    source = (
                        '{"schema":"narwhal.state","schema_version":1,'
                        '"api_key":"private-credential","prompt":"private-request",'
                        f'"value":{number}' + "}"
                    ).encode()

                    def handler(request, route=route, source=source):
                        if request.url.path.endswith(route):
                            return httpx.Response(200, content=source)
                        return self.response(request)

                    observations = await self.observe(handler)
                    row = next(row for row in observations if row["source"] == route)
                    self.assertEqual(row["status"], "error")
                    self.assertEqual(
                        row["error_code"],
                        "source_unavailable" if route == "/health" else "unsupported_contract",
                    )
                    self.assertIsInstance(row["data"], str)
                    self.assertIn("[REDACTED]", row["data"])
                    serialized = json.dumps(observations, allow_nan=False)
                    self.assertNotIn("private-credential", serialized)
                    self.assertNotIn("private-request", serialized)
                    self.assertTrue(
                        all(row["status"] == "ok" for row in observations if row["source"] != route)
                    )

    async def test_excessive_json_nesting_discards_body_and_preserves_other_observations(self):
        source = (
            b'{"nested":' * 1100
            + b'{"api_key":"nested-private-credential","prompt":"nested-private-request"}'
            + b"}" * 1100
        )
        for route in ("/health", "/narwhal/state"):
            with self.subTest(route=route):

                def handler(request, route=route):
                    if request.url.path.endswith(route):
                        return httpx.Response(200, content=source)
                    return self.response(request)

                observations = await self.observe(handler)
                row = next(row for row in observations if row["source"] == route)
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["error_code"], "source_unavailable")
                self.assertEqual(row["error"], "Router response exceeds JSON nesting limits")
                self.assertEqual(row["http_status"], 200)
                self.assertIsNone(row["data"])
                self.assertNotIn("raw_body", row)
                serialized = json.dumps(observations, allow_nan=False)
                self.assertNotIn("nested-private-credential", serialized)
                self.assertNotIn("nested-private-request", serialized)
                self.assertTrue(
                    all(row["status"] == "ok" for row in observations if row["source"] != route)
                )

    async def test_freshness_uses_elapsed_capture_age_not_nested_event_timestamps(self):
        clock = [0.0]

        def handler(request):
            clock[0] += 0.6
            response = self.response(request)
            data = json.loads(response.content)
            data.update(events=[{"at": 0}], observed_at="1970-01-01T00:00:00Z")
            return httpx.Response(200, json=data)

        with patch.object(status, "monotonic", side_effect=lambda: clock[0]):
            observations = await self.observe(handler, freshness_s=1)
        self.assertEqual([row["status"] for row in observations], ["stale", "stale", "stale", "ok"])
        self.assertEqual(observations[0]["error_code"], "source_stale")
        self.assertEqual(observations[-1]["data"]["observed_at"], "1970-01-01T00:00:00Z")

    async def test_endpoint_and_budget_rejection_happens_before_network_access(self):
        transport = httpx.MockTransport(
            lambda request: self.fail("invalid collection inputs caused network access")
        )
        for url in (
            "ftp://router.test",
            "http://operator:private-secret@router.test",
            "http://router.test?token=private-secret",
            "http://router.test/#fragment",
            "http://router.test:invalid/",
            "http://router.test/white space",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                await status.observe_router(url, redactor=Redactor(False), transport=transport)
            self.assertNotIn("private-secret", str(caught.exception))
        for kwargs in (
            {"timeout_s": 0},
            {"timeout_s": float("nan")},
            {"timeout_s": 31},
            {"timeout_s": True},
            {"freshness_s": 0},
            {"freshness_s": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await status.observe_router(
                    "http://router.test", redactor=Redactor(False), transport=transport, **kwargs
                )


if __name__ == "__main__":
    unittest.main()
