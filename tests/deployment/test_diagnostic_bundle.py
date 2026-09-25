"""Keep incident evidence private, bounded and useful through individual source failures."""

import asyncio
import hashlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.dev.cli import main
from narwhal.diagnostics import bundle


class _InterruptedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"phase":"draining","pending":'
        await asyncio.sleep(10)


class DiagnosticBundleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "bundle"
        self.requests = []

    def healthy(self, request):
        self.requests.append(request)
        if request.url.path == "/metrics":
            return httpx.Response(200, text="narwhal_accepted_total 42\n")
        return httpx.Response(200, json={"phase": "ready", "path": request.url.path})

    async def collect(self, **kwargs):
        return await bundle.collect(
            "http://router.test",
            self.output,
            transport=httpx.MockTransport(kwargs.pop("handler", self.healthy)),
            **kwargs,
        )

    def exported(self, manifest):
        return b"\n".join(
            (self.output / row["file"]).read_bytes() for row in manifest["sources"] if "file" in row
        )

    async def test_healthy_bundle_hashes_private_permissions_and_read_only_sources(self):
        run = self.root / "run-one"
        run.mkdir()
        fleet = self.root / "fleet.json"
        fleet.write_text('{"model":"fixture"}\n')
        profile = run / "profiles.json"
        profile.write_text('{"engines":["one"]}\n')
        log = run / "router.log"
        log.write_text("engine one ready\n")
        (run / "profile.stdout").write_text("partial stage stdout\n")
        engine = run / "engine-one"
        engine.mkdir()
        (engine / "start.stderr").write_text("partial stage stderr\n")
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (fleet, profile, log)
        }
        with patch("subprocess.run", side_effect=AssertionError("collector spawned a subprocess")):
            manifest = await self.collect(fleet=fleet, run=run)
        self.assertEqual(manifest["schema"], "narwhal.diagnostic-bundle")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(len(manifest["sources"]), 10)
        self.assertEqual(
            {request.url.path for request in self.requests},
            {path for _, path in bundle.SNAPSHOTS},
        )
        self.assertEqual({request.method for request in self.requests}, {"GET"})
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual(json.loads((self.output / "manifest.json").read_text()), manifest)
        for row in manifest["sources"]:
            path = self.output / row["file"]
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(row["bytes"], path.stat().st_size)
            self.assertIn("collected_at", row)
            if row["kind"] == "artifact" and Path(row["source"]) in before:
                source = Path(row["source"])
                self.assertEqual(
                    row["source_sha256"], hashlib.sha256(before[source][0]).hexdigest()
                )
                self.assertEqual(row["source_mtime_ns"], before[source][1])
        self.assertEqual(stat.S_IMODE((self.output / "manifest.json").stat().st_mode), 0o600)
        self.assertEqual(
            before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}
        )

    async def test_degraded_unreachable_and_missing_sources_preserve_other_responses(self):
        def handler(request):
            if request.url.path == "/ready":
                return httpx.Response(503, json={"reason": "lease fenced"})
            if request.url.path == "/narwhal/lifecycle":
                raise httpx.ConnectError("fixture connection refused", request=request)
            return self.healthy(request)

        missing = self.root / "missing.log"
        manifest = await self.collect(handler=handler, artifacts=(missing,))
        rows = {row["source"]: row for row in manifest["sources"]}
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(rows[str(missing)]["status"], "error")
        ready = rows["http://router.test/ready"]
        self.assertEqual(ready["http_status"], 503)
        self.assertEqual(ready["status"], "http_error")
        self.assertEqual(
            json.loads((self.output / ready["file"]).read_text()), {"reason": "lease fenced"}
        )
        self.assertEqual(rows["http://router.test/narwhal/lifecycle"]["status"], "error")
        self.assertEqual(rows["http://router.test/metrics"]["status"], "ok")

    async def test_source_deadline_keeps_partial_body_and_continues(self):
        def handler(request):
            if request.url.path == "/ready":
                return httpx.Response(503, stream=_InterruptedBody())
            return self.healthy(request)

        started = time.monotonic()
        manifest = await self.collect(handler=handler, source_timeout=0.03, timeout=2)
        self.assertLess(time.monotonic() - started, 1)
        ready = next(row for row in manifest["sources"] if row["source"].endswith("/ready"))
        self.assertEqual(ready["status"], "timeout")
        self.assertEqual(ready["http_status"], 503)
        self.assertIn(b'"phase":"draining"', (self.output / ready["file"]).read_bytes())
        self.assertEqual(manifest["sources"][-1]["status"], "ok")

    async def test_overall_deadline_records_every_endpoint_without_waiting_per_endpoint(self):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, stream=_InterruptedBody())

        started = time.monotonic()
        manifest = await self.collect(handler=handler, source_timeout=2, timeout=0.04)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(manifest["sources"]), 5)
        self.assertEqual({row["status"] for row in manifest["sources"]}, {"timeout"})
        self.assertIn("file", manifest["sources"][0])
        self.assertTrue((self.output / "manifest.json").exists())

    async def test_partial_write_retains_manifest_and_other_sources(self):
        fdopen = os.fdopen
        writes = 0

        class PartialWriter:
            def __init__(self, target):
                self.target = target

            def write(self, data):
                self.target.write(data[:5])
                self.target.flush()
                raise OSError("fixture partial write failure")

        @contextmanager
        def fail_one(descriptor, mode):
            nonlocal writes
            with fdopen(descriptor, mode) as target:
                writes += 1
                yield PartialWriter(target) if writes == 1 else target

        with patch.object(bundle.os, "fdopen", side_effect=fail_one):
            manifest = await self.collect()
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["sources"][0]["status"], "write_error")
        self.assertNotIn("file", manifest["sources"][0])
        self.assertFalse((self.output / "0000-health.json").exists())
        self.assertEqual(manifest["sources"][-1]["status"], "ok")
        self.assertEqual(json.loads((self.output / "manifest.json").read_text()), manifest)

    async def test_redacts_credentials_custom_references_and_request_fields(self):
        fleet = self.root / "fleet.json"
        fleet.write_text(
            json.dumps(
                {
                    "engine_api_key_env": "CUSTOM_ENGINE",
                    "credential_env": "CUSTOM_REMOTE",
                    "token": "embedded-secret",
                    "max_tokens": 2048,
                }
            )
        )
        log = self.root / "process.log"
        log.write_text("custom-engine-secret custom-remote-secret embedded-secret\n")
        command = self.root / "engine.command.json"
        command.write_text(json.dumps({"argv": ["server", "--api-key", "argv-secret"]}))
        secret_file = self.root / ".env"
        secret_file.write_text("SAMPLE=private-file-secret\n")

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "credential_env": "CUSTOM_REMOTE",
                    "prompt": "private prompt",
                    "messages": [{"content": "private message"}],
                    "authorization": "Bearer header-secret",
                    "endpoint": "https://user:url-secret@fixture.test?token=query-secret",
                    "phase": "ready",
                    "detail": "environment-secret",
                },
            )

        with patch.dict(
            os.environ,
            {
                "CUSTOM_ENGINE": "custom-engine-secret",
                "CUSTOM_REMOTE": "custom-remote-secret",
                "TEST_API_KEY": "environment-secret",
            },
        ):
            manifest = await self.collect(
                handler=handler, fleet=fleet, artifacts=(log, command, secret_file)
            )
        exported = self.exported(manifest)
        for value in (
            "custom-engine-secret",
            "custom-remote-secret",
            "embedded-secret",
            "argv-secret",
            "private-file-secret",
            "private prompt",
            "private message",
            "header-secret",
            "url-secret",
            "query-secret",
            "environment-secret",
        ):
            self.assertNotIn(value.encode(), exported)
            self.assertNotIn(value, json.dumps(manifest))
        self.assertIn(b'"engine_api_key_env": "CUSTOM_ENGINE"', exported)
        self.assertIn(b'"credential_env": "CUSTOM_REMOTE"', exported)
        self.assertIn(b'"max_tokens": 2048', exported)
        self.assertEqual(
            next(row for row in manifest["sources"] if row["source"].endswith("/.env"))["status"],
            "excluded",
        )

    async def test_request_content_requires_explicit_inclusion(self):
        run = self.root / "run"
        run.mkdir()
        (run / "journal.jsonl").write_text('{"prompt":"journal prompt"}\n')
        verify = run / "verify-one"
        verify.mkdir()
        (verify / "completion.json").write_text('{"text":"completion text","api_key":"secret"}')
        manifest = await self.collect(run=run)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(len(manifest["sources"]), 5)
        self.output = self.root / "included"
        manifest = await self.collect(run=run, include_request_content=True)
        exported = self.exported(manifest)
        self.assertIn(b"journal prompt", exported)
        self.assertIn(b"completion text", exported)
        self.assertNotIn(b'"secret"', exported)
        self.assertTrue(manifest["policy"]["include_request_content"])

    async def test_export_names_and_hashes_survive_short_secrets(self):
        source = self.root / "secret-in-name.log"
        source.write_text("log body\n")
        with patch.dict(os.environ, {"TEST_TOKEN": "a", "TEST_SECRET": "secret-in-name"}):
            manifest = await self.collect(artifacts=(source,))
        row = manifest["sources"][0]
        self.assertNotIn("secret-in-name", row["source"])
        self.assertEqual(
            row["sha256"], hashlib.sha256((self.output / row["file"]).read_bytes()).hexdigest()
        )
        self.assertEqual(row["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(manifest["schema"], "narwhal.diagnostic-bundle")
        self.assertEqual(manifest["status"], "success")
        self.assertTrue(all("secret-in-name" not in path.name for path in self.output.iterdir()))

    async def test_byte_limit_bounds_sources_and_records_truncation(self):
        source = self.root / "large.log"
        source.write_bytes(b"a" * 1000)
        manifest = await self.collect(
            handler=lambda request: httpx.Response(200, content=b"b" * 1000),
            artifacts=(source,),
            max_source_bytes=32,
        )
        self.assertEqual(manifest["status"], "partial")
        for row in manifest["sources"]:
            self.assertEqual(row["status"], "truncated")
            self.assertEqual(row["source_bytes"], 32)
            self.assertEqual(
                (self.output / row["file"]).read_bytes(),
                (b"a" if row["kind"] == "artifact" else b"b") * 32,
            )
            self.assertNotIn("source_sha256", row)

    async def test_rejects_symlinks_fifos_and_linked_parent_directories(self):
        source = self.root / "source.log"
        source.write_text("must stay outside bundle")
        link = self.root / "linked.log"
        link.symlink_to(source)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "fifo.log"
        os.mkfifo(fifo)
        manifest = await self.collect(artifacts=(link, fifo, parent_link / "source.log"))
        self.assertEqual([row["status"] for row in manifest["sources"][:3]], ["error"] * 3)
        self.assertNotIn(b"must stay outside bundle", self.exported(manifest))

    async def test_fresh_path_protection_preserves_existing_content(self):
        self.output.mkdir()
        sentinel = self.output / "manifest.json"
        sentinel.write_text("existing evidence")
        with self.assertRaises(FileExistsError):
            await self.collect()
        self.assertEqual(sentinel.read_text(), "existing evidence")
        self.assertEqual(self.requests, [])

    async def test_instance_selects_its_run_and_rejects_a_sibling_run(self):
        instance = self.root / "instance"
        instance.mkdir()
        (instance / "instance.json").write_text("{}")
        (instance / "fleet.json").write_text("{}")
        run = instance / "run-one"
        run.mkdir()
        (run / "router.log").write_text("selected run")
        (instance / "lifecycle.json").write_text(json.dumps({"run": str(run)}))
        manifest = await self.collect(instance=instance)
        self.assertIn(b"selected run", self.exported(manifest))
        sibling = self.root / "sibling"
        sibling.mkdir()
        (sibling / "router.log").write_text("sibling private data")
        (instance / "lifecycle.json").write_text(json.dumps({"run": str(sibling)}))
        self.output = self.root / "second"
        manifest = await self.collect(instance=instance)
        self.assertEqual(manifest["status"], "partial")
        self.assertNotIn(b"sibling private data", self.exported(manifest))
        self.assertTrue(
            any(
                row["kind"] == "selection" and row["status"] == "error"
                for row in manifest["sources"]
            )
        )

    async def test_invalid_options_fail_before_creating_output_or_http(self):
        options = [
            {"source_timeout": 0},
            {"source_timeout": float("inf")},
            {"timeout": -1},
            {"timeout": float("nan")},
            {"max_source_bytes": 0},
            {"instance": self.root, "run": self.root},
        ]
        for option in options:
            with self.subTest(option=option), self.assertRaises(ValueError):
                await self.collect(**option)
            self.assertFalse(self.output.exists())
        for router in (
            "relative",
            "ftp://host",
            "http://user:secret@host",
            "http://host?token=x",
            "http://host#fragment",
            "http://host:0",
            "http://host:invalid",
        ):
            with self.subTest(router=router), self.assertRaises(ValueError):
                await bundle.collect(router, self.output)
            self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])


class RedactionTests(unittest.TestCase):
    def test_partial_and_multiline_values_cannot_escape_redaction(self):
        redactor = bundle.Redactor(False)
        for body in (
            b'{"password":"first secret part second',
            b'{\n  "api_key": "first secret part\nsecond secret part\n',
            b"credential=first secret part\nsecond secret part",
            b'{"prompt":"first secret part\nsecond secret part',
        ):
            with self.subTest(body=body):
                result = redactor.body(body)
                self.assertNotIn(b"first secret part", result)
                self.assertNotIn(b"second secret part", result)
                self.assertIn(b"[REDACTED]", result)

    def test_response_references_redact_custom_environment_values(self):
        with patch.dict(os.environ, {"CUSTOM_VALUE": "endpoint-credential"}):
            result = bundle.Redactor(False).body(
                b'{"credential_env":"CUSTOM_VALUE","detail":"endpoint-credential"}'
            )
        self.assertNotIn(b"endpoint-credential", result)
        self.assertIn(b"CUSTOM_VALUE", result)

    def test_known_secret_cannot_disable_sensitive_field_detection(self):
        with patch.dict(os.environ, {"TEST_TOKEN": "password"}):
            redactor = bundle.Redactor(True)
            result = redactor.body(b'{"error":"password: second-secret"}')
        self.assertNotIn(b"second-secret", result)

    def test_credential_arguments_are_redacted_in_embedded_strings(self):
        redactor = bundle.Redactor(True)
        result = redactor.body(b'{"error":"command failed: --api-key=secret-value"}')
        self.assertNotIn(b"secret-value", result)


class DiagnosticCommandTests(unittest.TestCase):
    def test_root_command_emits_created_manifest_and_partial_exit(self):
        original = bundle.collect

        async def local_collect(*args, **kwargs):
            return await original(
                *args,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        503,
                        json={
                            "phase": "degraded",
                        },
                    )
                ),
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "bundle"
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.object(bundle, "collect", side_effect=local_collect),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = main(
                    [
                        "diagnostics",
                        "collect",
                        "--router",
                        "http://router.test",
                        "--out",
                        str(output),
                        "--format",
                        "json",
                    ]
                )
            document = json.loads(stdout.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(document["schema"], "narwhal.command-result")
            self.assertEqual(document["operation"], "diagnostics collect")
            self.assertEqual(document["status"], "degraded")
            self.assertEqual(document["data"]["manifest"], str(output / "manifest.json"))
            self.assertEqual(document["data"]["sources"], 5)
            self.assertEqual(document["artifacts"][0]["state"], "created")
            self.assertEqual(document["errors"][0]["code"], "collection_partial")
            self.assertEqual(stderr.getvalue(), "")

    def test_text_mode_invalid_options_return_concise_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "bundle"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "diagnostics",
                        "collect",
                        "--router",
                        "http://router.test",
                        "--out",
                        str(output),
                        "--timeout",
                        "nan",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("--timeout must be finite and positive", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(output.exists())
