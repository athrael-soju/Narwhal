"""Exercise bounded monitoring and queries with explicit local HTTP fixtures."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from narwhal.deployment.management_access import AccessError, InspectionAccess, now
from narwhal.deployment.management_registry import ManagementQuery, ManagementRegistry
from narwhal.diagnostics.bundle import Redactor
from narwhal.diagnostics.management_artifacts import ArtifactStore
from narwhal.mcp.adapters import ToolRegistry
from narwhal.mcp.observability import observability_adapters
from narwhal.observability.management_http import fetch
from narwhal.observability.management_local import LocalSiteProvider, _utility
from narwhal.observability.management_metrics import metric_series, query_metrics, query_parameters
from narwhal.observability.management_status import observe_monitoring
from narwhal.observability.management_targets import TargetContract
from narwhal.observability.management_types import MonitoringBinding, SourceCapture


class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binding = MonitoringBinding(
            TargetContract("router:8000", (("n1", "engine:8001"),)),
            "http://prometheus:9090",
            "host",
        )
        self.documents = {
            "/-/ready": "Prometheus Server is Ready.",
            "/api/v1/status/buildinfo": {"status": "success", "data": {"version": "3.14.0"}},
            "/api/health": {"database": "ok", "version": "13.2.1"},
            "/api/datasources/name/Prometheus": {
                "type": "prometheus",
                "url": "http://prometheus:9090",
            },
            "/api/dashboards/uid/narwhal-router": {
                "dashboard": {
                    "uid": "narwhal-router",
                    "templating": {
                        "list": [
                            {
                                "name": "router",
                                "includeAll": True,
                                "allValue": ".*",
                                "current": {"value": "$__all"},
                            }
                        ]
                    },
                    "panels": [{"expr": 'narwhal_router_ready{instance=~"$router"}'}],
                }
            },
            "/api/v1/targets": {
                "status": "success",
                "data": {
                    "activeTargets": [
                        {
                            "scrapePool": "narwhal-router",
                            "scrapeUrl": "http://router:8000/metrics",
                            "health": "up",
                            "lastScrape": now(),
                            "lastError": "",
                            "labels": {},
                        },
                        {
                            "scrapePool": "engines",
                            "scrapeUrl": "http://engine:8001/metrics",
                            "health": "up",
                            "lastScrape": now(),
                            "lastError": "",
                            "labels": {"iid": "n1"},
                        },
                    ]
                },
            },
            "/api/v1/query": {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [time.time(), "1"]}],
                },
            },
            "/ready": {"status": "ready"},
        }
        self.statuses = {}
        self.requests = []

    async def observe(self):
        def request(request):
            self.requests.append(request)
            payload = self.documents[request.url.path]
            arguments = {"text": payload} if isinstance(payload, str) else {"json": payload}
            return httpx.Response(self.statuses.get(request.url.path, 200), **arguments)

        return await observe_monitoring(
            self.binding,
            prometheus_url="http://prometheus:9090",
            grafana_url="http://grafana:3000",
            router_url="http://router:8000",
            deadline=time.monotonic() + 2,
            freshness_s=60,
            redactor=Redactor(False),
            transport=httpx.MockTransport(request),
        )

    async def test_healthy_monitoring_and_refused_serving_are_distinct(self):
        healthy = await self.observe()
        self.assertEqual(healthy["readiness"], "pass", healthy)
        self.documents["/ready"] = {"status": "degraded"}
        self.statuses["/ready"] = 503
        refused = await self.observe()
        self.assertEqual(refused["readiness"], "fail", refused)
        self.assertTrue(all(row["status"] == "ok" for row in refused["sources"]))
        checks = refused["sources"][-1]["data"]["checks"]
        self.assertEqual(
            next(row for row in checks if row["check"] == "configured_targets")["status"], "pass"
        )

    async def test_missing_failed_stale_duplicate_targets_never_pass(self):
        original = copy.deepcopy(self.documents["/api/v1/targets"])
        for failure in ("missing", "down", "stale", "duplicate", "wrong_address"):
            with self.subTest(failure=failure):
                targets = copy.deepcopy(original)
                rows = targets["data"]["activeTargets"]
                if failure == "missing":
                    rows.pop()
                elif failure == "down":
                    rows[1].update(health="down", lastError="connection refused")
                elif failure == "stale":
                    rows[1]["lastScrape"] = "2000-01-01T00:00:00Z"
                elif failure == "duplicate":
                    rows.append(rows[1])
                else:
                    rows[1]["scrapeUrl"] = "http://another:8001/metrics"
                self.documents["/api/v1/targets"] = targets
                observed = await self.observe()
                self.assertEqual(observed["readiness"], "fail", observed)

    async def test_grafana_contracts_and_metric_presence_are_required(self):
        for route, replacement in (
            ("/api/health", {"database": "ok", "version": "wrong"}),
            ("/api/datasources/name/Prometheus", {"type": "prometheus", "url": "http://wrong"}),
            ("/api/dashboards/uid/narwhal-router", {"dashboard": {"uid": "wrong"}}),
            ("/api/v1/query", {"status": "success", "data": {"result": []}}),
        ):
            with self.subTest(route=route):
                original = self.documents[route]
                self.documents[route] = replacement
                self.assertEqual((await self.observe())["readiness"], "fail")
                self.documents[route] = original

    async def test_stream_cap_redirect_and_absolute_timeout_preserve_evidence(self):
        calls = []

        async def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"Location": "http://private/secret"})
            if request.url.path == "/slow":
                await asyncio.sleep(0.2)
            return httpx.Response(200, text='{"value":"' + "x" * 100 + '"}')

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
        ) as client:
            bounded = await fetch(
                client,
                "http://fixture/large",
                "fixture",
                deadline=time.monotonic() + 1,
                redactor=Redactor(False),
                maximum=32,
            )
            self.assertEqual(bounded["status"], "truncated")
            self.assertEqual(len(bounded["raw_body"]), 32)
            redirected = await fetch(
                client,
                "http://fixture/redirect",
                "fixture",
                deadline=time.monotonic() + 1,
                redactor=Redactor(False),
            )
            self.assertEqual(redirected["status"], "unavailable")
            started = time.monotonic()
            timed = await fetch(
                client,
                "http://fixture/slow",
                "fixture",
                deadline=started + 0.02,
                redactor=Redactor(False),
            )
            self.assertEqual(timed["status"], "timeout")
            self.assertLess(time.monotonic() - started, 0.15)
        self.assertNotIn("/secret", calls)


class QueryTests(unittest.IsolatedAsyncioTestCase):
    def query(self, kind="instant"):
        return ManagementQuery(id="registered", expression='up{job="engines"}', kind=kind)

    async def test_query_range_validation_precedes_http(self):
        at = datetime(2026, 1, 1, tzinfo=UTC)
        valid = {
            "start": "2025-12-31T23:00:00Z",
            "end": "2026-01-01T00:00:00Z",
            "step_s": 1,
            "limit_series": 100,
            "at": at,
        }
        self.assertEqual(query_parameters(self.query("range"), **valid)["step"], "1")
        for override in (
            {"start": "2025-12-31T22:59:59Z"},
            {"end": "2026-01-01T00:00:01Z"},
            {"start": "2026-01-01T00:00:00Z"},
            {"step_s": True},
            {"step_s": 301},
            {"limit_series": 101},
        ):
            with self.subTest(override=override), self.assertRaises(AccessError):
                query_parameters(self.query("range"), **(valid | override))
        with self.assertRaises(AccessError):
            query_parameters(self.query(), **valid)

    async def test_fixed_query_preserves_nonfinite_strings_and_warnings(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "warnings": ["partial backend"],
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {"iid": "n1"}, "value": [time.time(), "NaN"]}],
                    },
                },
            )

        data, observation, errors = await query_metrics(
            self.query(),
            prometheus_url="http://metrics",
            deadline=time.monotonic() + 1,
            freshness_s=60,
            redactor=Redactor(False),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(requests[0].url.params["query"], self.query().expression)
        self.assertIn("timeout", requests[0].url.params)
        self.assertEqual(data["series"][0]["samples"][0]["value"], "NaN")
        self.assertFalse(data["complete"])
        self.assertEqual(errors[0]["code"], "query_warning")
        self.assertEqual(observation["status"], "ok")

    def test_series_and_sample_caps_preserve_accepted_prefix(self):
        document = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"id": str(i)}, "values": [[1, "2"]] * 6000} for i in range(3)
                ],
            },
        }
        _, series, complete = metric_series(document, 2)
        self.assertEqual([len(row["samples"]) for row in series], [6000, 4000])
        self.assertFalse(complete)

    async def test_missing_stale_and_invalid_query_responses_are_incomplete(self):
        for rows, expected in (
            ([], "source_missing"),
            ([{"metric": {}, "value": [1, "1"]}], "source_stale"),
            ([{"metric": {}, "value": [True, "1"]}], "invalid_source"),
        ):
            with self.subTest(expected=expected):
                transport = httpx.MockTransport(
                    lambda request, rows=rows: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {"resultType": "vector", "result": rows},
                        },
                    )
                )
                data, _, errors = await query_metrics(
                    self.query(),
                    prometheus_url="http://metrics",
                    deadline=time.monotonic() + 1,
                    freshness_s=60,
                    redactor=Redactor(False),
                    transport=transport,
                )
                self.assertFalse(data["complete"])
                self.assertEqual(errors[0]["code"], expected)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "router.log"
        self.log.write_text('{"prompt":"private-request","detail":"test-secret-value"}\n')
        self.log.chmod(0o600)
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": str(uuid4()),
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "fixture",
                    "kind": "dev",
                    "working_directory": str(self.root),
                    "fleet_file": None,
                    "instance_dir": str(self.root / "instance"),
                    "artifact_root": str(self.root / "artifacts"),
                    "adapter": {"id": "local-dev-v1", "settings_path": None},
                    "endpoints": {"prometheus_env": "TEST_PROMETHEUS"},
                    "credential_env": ["TEST_CREDENTIAL"],
                    "queries": [
                        {"id": "ready", "kind": "instant", "expression": 'up{job="engines"}'}
                    ],
                    "logs": [{"id": "router", "host_id": "local", "source": str(self.log)}],
                }
            ],
        }
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        self.environment = patch.dict(
            "os.environ",
            {"TEST_CREDENTIAL": "test-secret-value", "TEST_PROMETHEUS": "http://fixture:9090"},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tools(self, provider=None, transport=None):
        return ToolRegistry(
            observability_adapters(self.registry, site_provider=provider, transport=transport)
        )

    def read(self, reference):
        return ArtifactStore(str(self.registry.registry_id), self.registry.targets[0]).read(
            reference
        )["text"]

    async def test_log_capture_redacts_and_remains_immutable(self):
        tools = self.tools()
        reply = await tools.dispatch("host_logs", {"target_id": "fixture", "log_id": "router"})
        self.assertEqual(reply["outcome"], "success", reply)
        artifact = reply["data"]["artifact_id"]
        text = self.read(artifact)
        self.assertNotIn("private-request", text)
        self.assertNotIn("test-secret-value", text)
        self.assertIn("[REDACTED]", text)
        self.log.write_text("changed source")
        self.assertEqual(self.read(artifact), text)

    async def test_log_tail_starts_on_utf8_boundary_and_shortening_is_incomplete(self):
        self.log.write_text("x" * 30 + "€tail")
        provider = LocalSiteProvider(InspectionAccess(self.registry))
        captures = await provider.collect(
            self.registry.targets[0], "log", "router", deadline=time.monotonic() + 1, max_bytes=6
        )
        self.assertEqual(captures[0].content, b"tail")
        original = os.pread

        def shorten(fd, count, offset):
            self.log.write_text("x")
            return original(fd, count, offset)

        with patch("narwhal.observability.management_local.os.pread", side_effect=shorten):
            reply = await self.tools().dispatch(
                "host_logs", {"target_id": "fixture", "log_id": "router"}
            )
        self.assertEqual(reply["outcome"], "degraded", reply)
        self.assertFalse(reply["data"]["complete"])

    async def test_unregistered_unsafe_missing_and_binary_log_sources(self):
        tools = self.tools()
        invalid = await tools.dispatch("host_logs", {"target_id": "fixture", "log_id": "arbitrary"})
        self.assertEqual(invalid["outcome"], "invalid_input")
        self.log.chmod(0o666)
        denied = await tools.dispatch("host_logs", {"target_id": "fixture", "log_id": "router"})
        self.assertEqual(denied["errors"][0]["code"], "permission_denied", denied)
        self.log.unlink()
        missing = await tools.dispatch("host_logs", {"target_id": "fixture", "log_id": "router"})
        self.assertEqual(missing["outcome"], "degraded", missing)
        self.assertEqual(missing["errors"][0]["code"], "source_missing")
        self.log.symlink_to(self.root / "other")
        linked = await tools.dispatch("host_logs", {"target_id": "fixture", "log_id": "router"})
        self.assertEqual(linked["errors"][0]["code"], "permission_denied", linked)
        self.log.unlink()
        self.log.write_bytes(b"binary\0")
        self.log.chmod(0o600)
        binary = await tools.dispatch("host_logs", {"target_id": "fixture", "log_id": "router"})
        self.assertEqual(binary["errors"][0]["code"], "unsupported_media_type", binary)

    async def test_bad_arguments_and_grants_precede_provider_or_http(self):
        calls = []

        def request(request):
            calls.append(request)
            return httpx.Response(500)

        tools = self.tools(transport=httpx.MockTransport(request))
        for arguments in (
            {"query_id": "arbitrary"},
            {"query_id": "ready", "step_s": 15},
            {"query_id": "ready", "limit_series": True},
            {"query_id": "ready", "url": "http://private"},
            {"query_id": "ready", "target_id": "absent"},
        ):
            reply = await tools.dispatch("metrics_query", {"target_id": "fixture", **arguments})
            self.assertEqual(reply["outcome"], "invalid_input", reply)
        self.document["targets"][0]["capabilities"] = []
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        denied = await self.tools(transport=httpx.MockTransport(request)).dispatch(
            "metrics_query", {"target_id": "fixture", "query_id": "ready"}
        )
        self.assertEqual(denied["errors"][0]["code"], "permission_denied")
        self.assertEqual(calls, [])

    async def test_inventory_partial_sources_export_manifest_and_keep_byte_cap(self):
        captured = []

        class Provider:
            async def collect(self, target, kind, subject_id, *, deadline, max_bytes):
                captured.append((target.id, kind, subject_id, deadline, max_bytes))
                return (
                    SourceCapture("system", b'{"token":"private"}', now()),
                    SourceCapture("gpu", b"", now(), False, "unavailable", "utility_missing"),
                    SourceCapture("network", b"test-secret-value" * 100000, now()),
                )

        reply = await self.tools(Provider()).dispatch(
            "host_inventory", {"target_id": "fixture", "host_id": "local"}
        )
        self.assertEqual(reply["outcome"], "degraded", reply)
        self.assertEqual(captured[0][:3], ("fixture", "inventory", "local"))
        self.assertEqual(captured[0][4], 1_048_576)
        manifest = json.loads(self.read(reply["data"]["snapshot_artifact_id"]))
        self.assertFalse(manifest["complete"])
        self.assertLessEqual(sum(row["bytes"] for row in manifest["sources"]), 1_048_576)
        self.assertEqual(manifest["sources"][1]["error"]["code"], "utility_missing")
        self.assertNotIn("private", self.read(manifest["sources"][0]["artifact_id"]))

    async def test_stale_host_evidence_never_qualifies_as_complete(self):
        class Provider:
            async def collect(self, *args, **kwargs):
                return (SourceCapture("system", b"{}", "2000-01-01T00:00:00Z"),)

        reply = await self.tools(Provider()).dispatch(
            "host_inventory", {"target_id": "fixture", "host_id": "local"}
        )
        self.assertEqual(reply["outcome"], "degraded", reply)
        self.assertEqual(reply["errors"][0]["code"], "source_stale")

    async def test_large_metric_response_uses_exported_prefix_at_exact_http_cap(self):
        body = b'{"x":"' + b"x" * 8_388_608 + b'"}'
        tools = self.tools(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        )
        reply = await tools.dispatch("metrics_query", {"target_id": "fixture", "query_id": "ready"})
        self.assertEqual(reply["outcome"], "degraded", reply)
        self.assertFalse(reply["data"]["complete"])
        self.assertEqual(reply["artifacts"][0]["size_bytes"], 8_388_608)
        self.assertFalse(reply["artifacts"][0]["complete"])


class UtilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_failed_oversized_and_timed_out_sources_are_partial(self):
        with patch("narwhal.observability.management_local.shutil.which", return_value=None):
            absent = await _utility("fixture", [], time.monotonic() + 1, 128)
        self.assertEqual(absent.error_code, "utility_missing")
        for script, expected in (
            ("raise SystemExit(1)", "utility_failed"),
            ("print('x' * 200)", "source_truncated"),
            ("import time; time.sleep(10)", "source_unavailable"),
        ):
            with (
                self.subTest(script=script),
                patch(
                    "narwhal.observability.management_local.shutil.which",
                    return_value=sys.executable,
                ),
            ):
                captured = await _utility("fixture", ["-c", script], time.monotonic() + 0.3, 128)
            self.assertFalse(captured.complete)
            self.assertEqual(captured.error_code, expected)
            self.assertLessEqual(len(captured.content), 128)
