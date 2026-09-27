"""Keep CLI diagnostic collection within registered inputs and retained exports."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.command_results import EXIT_CODES
from narwhal.contracts import COMMAND_RESULT, ContractVersionError, versioned
from narwhal.deployment.management_access import AccessError, InspectionAccess
from narwhal.deployment.management_commands import CommandError
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.diagnostics import bundle
from narwhal.diagnostics.management_artifacts import ArtifactStore
from narwhal.diagnostics.management_collection import MAX_SOURCES, collect_diagnostics


class ManagementCollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.fleet = self.root / "fleet.json"
        self.fleet.write_text(
            json.dumps(
                {
                    "engines": [],
                    "credential_env": "EXPLICIT",
                    "credential": "explicit-private-marker",
                    "prompt": "request-content-marker",
                }
            )
        )
        self.fleet.chmod(0o600)
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": "10000000-0000-4000-8000-000000000001",
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "fleet-a",
                    "kind": "fleet",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "exports"),
                    "fleet_file": str(self.fleet),
                    "instance_dir": None,
                    "adapter": {"id": "ssh-v1", "settings_path": str(self.root / "site.json")},
                    "endpoints": {"router_env": "ROUTER"},
                    "credential_env": ["EXPLICIT"],
                }
            ],
        }
        environment = patch.dict(
            os.environ,
            {
                "ROUTER": "http://router.example.invalid",
                "EXPLICIT": "explicit-private-marker",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.calls = []
        self.captured = []
        self.requests = []
        self.partial = False
        self.manipulate = None

    def access(self):
        return InspectionAccess(ManagementRegistry.model_validate_json(json.dumps(self.document)))

    def response(self, request):
        self.requests.append(request)
        status = 503 if self.partial and request.url.path == "/ready" else 200
        return httpx.Response(
            status,
            json={
                "credential": "explicit-private-marker",
                "arbitrary": "explicit-private-marker",
                "prompt": "request-content-marker",
                "route": request.url.path,
            },
        )

    async def command(self, arguments, *, cwd, env, timeout_s, pass_fds):
        self.calls.append((arguments, cwd, env, timeout_s, pass_fds))
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        bundle.add_commands(commands)
        args = parser.parse_args(arguments)
        if args.fleet.exists():
            self.captured.append(args.fleet.read_bytes())
        with patch.dict(os.environ, env, clear=True):
            manifest = await bundle.collect(
                args.router,
                args.out,
                fleet=args.fleet,
                instance=args.instance,
                timeout=args.timeout,
                source_timeout=args.source_timeout,
                max_source_bytes=args.max_source_bytes,
                max_sources=args.max_sources,
                include_request_content=args.include_request_content,
                transport=httpx.MockTransport(self.response),
            )
        self.captured.extend(path.read_bytes() for path in args.out.iterdir())
        if self.manipulate:
            self.manipulate(args.out, manifest)
        status = "degraded" if manifest["status"] == "partial" else "success"
        return versioned(
            COMMAND_RESULT,
            {
                "command": "narwhal",
                "operation": "diagnostics collect",
                "status": status,
                "exit_code": EXIT_CODES[status],
                "data": {
                    "bundle": str(args.out),
                    "manifest": str(args.out / "manifest.json"),
                    "collection_status": manifest["status"],
                    "sources": len(manifest["sources"]),
                },
                "artifacts": [
                    {
                        "kind": "diagnostic_manifest",
                        "path": str(args.out / "manifest.json"),
                        "state": "created",
                    }
                ],
                "errors": (
                    [
                        {
                            "code": "collection_partial",
                            "message": "Partial capture",
                            "command": "narwhal",
                        }
                    ]
                    if self.partial
                    else []
                ),
            },
        )

    def stored_json(self, access, artifact_id):
        target = access.target("fleet-a")
        store = ArtifactStore(str(access.registry.registry_id), target)
        return json.loads(store.read(artifact_id)["text"])

    def assert_cleaned(self):
        if (self.root / "exports").exists():
            self.assertEqual(list((self.root / "exports").glob(".mcp-collection-*")), [])

    async def test_installed_command_arguments_and_prewrite_redaction_produce_readable_bundle(self):
        access = self.access()
        with patch(
            "narwhal.diagnostics.management_collection.run_command", side_effect=self.command
        ):
            collected = await collect_diagnostics(access, "fleet-a")
        self.assertEqual(collected.data["collection_status"], "success")
        self.assertEqual(collected.data["source_count"], 6)
        self.assertEqual(collected.errors, [])
        self.assertEqual(len(collected.artifacts), 8)
        args, cwd, environment, timeout, fds = self.calls[0]
        self.assertEqual(args[:4], ["diagnostics", "collect", "--format", "json"])
        self.assertEqual(cwd, self.root)
        self.assertEqual(args[args.index("--max-sources") + 1], "128")
        self.assertEqual(args[args.index("--max-source-bytes") + 1], str(8 * 1024 * 1024))
        self.assertLess(float(args[args.index("--source-timeout") + 1]), timeout)
        self.assertIn(
            "explicit-private-marker",
            [v for k, v in environment.items() if k.startswith("NARWHAL_MCP_SECRET_")],
        )
        self.assertNotIn("explicit-private-marker", " ".join(args))
        self.assertEqual(len(fds), 1)
        self.assertNotIn(b"explicit-private-marker", b"\n".join(self.captured))
        self.assertNotIn(b"request-content-marker", b"\n".join(self.captured))
        manifest = self.stored_json(access, collected.data["manifest_artifact_id"])
        index = self.stored_json(access, collected.data["bundle_artifact_id"])
        self.assertEqual(index["manifest_artifact_id"], collected.data["manifest_artifact_id"])
        self.assertEqual(len(index["sources"]), 6)
        self.assertEqual(manifest["sources"][0]["source"], str(self.fleet))
        self.assertEqual(manifest["sources"][0]["capture"], "redacted_registered_fleet_snapshot")
        self.assertEqual(
            manifest["sources"][0]["source_sha256"],
            hashlib.sha256(self.fleet.read_bytes()).hexdigest(),
        )
        self.assertNotIn("source_mtime_ns", manifest["sources"][0])
        for source in index["sources"]:
            self.assertIsNotNone(source["artifact_id"])
        self.assertEqual({request.method for request in self.requests}, {"GET"})
        self.assert_cleaned()

    async def test_partial_sources_preserve_command_status_and_available_evidence(self):
        access = self.access()
        self.partial = True
        with patch(
            "narwhal.diagnostics.management_collection.run_command", side_effect=self.command
        ):
            collected = await collect_diagnostics(access, "fleet-a")
        self.assertEqual(collected.command_result["status"], "degraded")
        self.assertEqual(collected.command_result["errors"][0]["code"], "collection_partial")
        self.assertEqual(collected.data["collection_status"], "partial")
        manifest = self.stored_json(access, collected.data["manifest_artifact_id"])
        ready = next(row for row in manifest["sources"] if row["source"].endswith("/ready"))
        self.assertEqual(ready["http_status"], 503)
        self.assertEqual(ready["status"], "http_error")
        self.assertIsNotNone(ready["artifact_id"])
        self.assert_cleaned()

    async def test_denied_content_or_inspection_never_reads_inputs_or_starts_command(self):
        for missing_grant in (False, True):
            with self.subTest(missing_grant=missing_grant):
                if missing_grant:
                    self.document["targets"][0]["capabilities"] = []
                access = self.access()
                with (
                    patch("narwhal.diagnostics.management_collection.open_input") as read,
                    patch(
                        "narwhal.diagnostics.management_collection.run_command",
                        new_callable=AsyncMock,
                    ) as run,
                    self.assertRaises(AccessError) as error,
                ):
                    await collect_diagnostics(access, "fleet-a", include_request_content=True)
                self.assertEqual(error.exception.code, "permission_denied")
                read.assert_not_called()
                run.assert_not_called()
        self.assertFalse((self.root / "exports").exists())
        self.assertFalse((self.root / "state").exists())

    async def test_permitted_request_content_remains_explicit_and_credentials_stay_redacted(self):
        self.document["targets"][0]["allow_request_content"] = True
        access = self.access()
        with patch(
            "narwhal.diagnostics.management_collection.run_command", side_effect=self.command
        ):
            collected = await collect_diagnostics(access, "fleet-a", include_request_content=True)
        self.assertIn("--include-request-content", self.calls[0][0])
        self.assertIn(b"request-content-marker", b"\n".join(self.captured))
        self.assertNotIn(b"explicit-private-marker", b"\n".join(self.captured))
        self.assertEqual(collected.data["collection_status"], "success")

    async def test_source_tampering_or_unsafe_manifest_filename_preserves_partial_bundle(self):
        for mode in ("changed", "outside"):
            with self.subTest(mode=mode):
                access = self.access()

                def manipulate(output, manifest, mode=mode):
                    row = manifest["sources"][0]
                    if mode == "changed":
                        (output / row["file"]).write_bytes(b"changed")
                    else:
                        row["file"] = "../fleet.json"
                        (output / "manifest.json").write_text(json.dumps(manifest))

                self.manipulate = manipulate
                with patch(
                    "narwhal.diagnostics.management_collection.run_command",
                    side_effect=self.command,
                ):
                    collected = await collect_diagnostics(access, "fleet-a")
                self.assertEqual(collected.data["collection_status"], "partial")
                self.assertEqual(
                    collected.errors[0]["code"],
                    "artifact_changed" if mode == "changed" else "permission_denied",
                )
                manifest = self.stored_json(access, collected.data["manifest_artifact_id"])
                self.assertNotIn("artifact_id", manifest["sources"][0])
                self.assert_cleaned()

    async def test_cli_failure_and_cancellation_remove_ephemeral_capture(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):

                async def fail(*args, cancelled=cancelled, **kwargs):
                    raise (
                        asyncio.CancelledError
                        if cancelled
                        else CommandError("stage_timeout", "Command timed out")
                    )

                with (
                    patch(
                        "narwhal.diagnostics.management_collection.run_command", side_effect=fail
                    ),
                    self.assertRaises(asyncio.CancelledError if cancelled else CommandError),
                ):
                    await collect_diagnostics(self.access(), "fleet-a")
                self.assert_cleaned()

    async def test_source_count_limit_reports_omissions_and_default_preserves_all_sources(self):
        run = self.root / "run"
        run.mkdir()
        for index in range(140):
            (run / f"worker-{index:03}.log").write_text("source\n")
        limited = await bundle.collect(
            "http://router.example.invalid",
            self.root / "limited",
            run=run,
            max_sources=MAX_SOURCES,
            transport=httpx.MockTransport(self.response),
        )
        self.assertEqual(len(limited["sources"]), MAX_SOURCES)
        self.assertEqual(limited["status"], "partial")
        selection = [row for row in limited["sources"] if row["kind"] == "selection"]
        self.assertEqual(len(selection), 1)
        self.assertEqual(selection[0]["status"], "truncated")
        self.assertEqual(sum(row["kind"] == "http" for row in limited["sources"]), 5)
        unlimited = await bundle.collect(
            "http://router.example.invalid",
            self.root / "unlimited",
            run=run,
            transport=httpx.MockTransport(self.response),
        )
        self.assertEqual(len(unlimited["sources"]), 145)
        self.assertEqual(unlimited["status"], "success")
        self.assertNotIn("max_sources", unlimited["policy"])

    async def test_source_count_and_manifest_limits_fail_without_unbounded_exports(self):
        for value in (0, 6, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await bundle.collect(
                    "http://router.example.invalid",
                    self.root / "invalid",
                    max_sources=value,
                )
        self.assertFalse((self.root / "invalid").exists())

        def manipulate(output, manifest):
            manifest["sources"] = [manifest["sources"][0]] * (MAX_SOURCES + 1)
            (output / "manifest.json").write_text(json.dumps(manifest))

        self.manipulate = manipulate
        with (
            patch(
                "narwhal.diagnostics.management_collection.run_command", side_effect=self.command
            ),
            self.assertRaises(AccessError) as error,
        ):
            await collect_diagnostics(self.access(), "fleet-a")
        self.assertEqual(error.exception.code, "invalid_input")
        self.assert_cleaned()

    async def test_unsupported_manifest_contract_is_preserved(self):
        def manipulate(output, manifest):
            manifest["schema_version"] = 999
            (output / "manifest.json").write_text(json.dumps(manifest))

        self.manipulate = manipulate
        with (
            patch(
                "narwhal.diagnostics.management_collection.run_command", side_effect=self.command
            ),
            self.assertRaises(ContractVersionError),
        ):
            await collect_diagnostics(self.access(), "fleet-a")
        self.assert_cleaned()

    def dev_instance(self):
        instance = self.root / "instance"
        instance.mkdir(mode=0o700)
        for name in ("instance.json", "fleet.json", "lifecycle.json"):
            (instance / name).write_text("{}")
            (instance / name).chmod(0o600)
        self.document["targets"][0].update(
            kind="dev",
            instance_dir=str(instance),
            fleet_file=None,
            adapter={"id": "local-dev-v1", "settings_path": None},
        )
        return instance

    async def test_dev_run_escape_symlink_and_unsafe_files_fail_before_command(self):
        instance = self.dev_instance()
        run = instance / "run"
        run.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        linked = instance / "linked-run"
        linked.symlink_to(run, target_is_directory=True)
        for selected, bad_file in (
            (outside, None),
            (linked, None),
            (run, "unsafe-mode"),
            (run, "dangling-link"),
        ):
            with self.subTest(selected=selected, bad_file=bad_file):
                (instance / "lifecycle.json").write_text(json.dumps({"run": str(selected)}))
                candidate = run / "fleet.json"
                if bad_file == "unsafe-mode":
                    candidate.write_text("{}")
                    candidate.chmod(0o666)
                elif bad_file == "dangling-link":
                    candidate.unlink()
                    candidate.symlink_to(outside / "missing.json")
                with (
                    patch(
                        "narwhal.diagnostics.management_collection.run_command",
                        new_callable=AsyncMock,
                    ) as command,
                    self.assertRaises(AccessError) as error,
                ):
                    await collect_diagnostics(self.access(), "fleet-a")
                self.assertEqual(error.exception.code, "permission_denied")
                command.assert_not_called()
        self.assertFalse((self.root / "exports").exists())
        self.assertFalse((self.root / "state").exists())

    async def test_dev_collection_reads_only_selected_run_inside_instance(self):
        instance = self.dev_instance()
        run = instance / "run"
        run.mkdir(mode=0o700)
        (instance / "lifecycle.json").write_text(json.dumps({"run": str(run)}))
        (run / "router.log").write_text("healthy\n")
        (run / "router.log").chmod(0o600)
        with patch(
            "narwhal.diagnostics.management_collection.run_command", side_effect=self.command
        ):
            collected = await collect_diagnostics(self.access(), "fleet-a")
        self.assertEqual(collected.data["collection_status"], "success")
        manifest = self.stored_json(self.access(), collected.data["manifest_artifact_id"])
        self.assertIn(str(run / "router.log"), [row["source"] for row in manifest["sources"]])
        self.assert_cleaned()

    async def test_missing_instance_or_run_retains_partial_router_collection(self):
        instance = self.dev_instance()
        run = instance / "missing-run"
        (instance / "lifecycle.json").write_text(json.dumps({"run": str(run)}))
        for missing_instance in (False, True):
            with self.subTest(missing_instance=missing_instance):
                if missing_instance:
                    for path in instance.iterdir():
                        path.unlink()
                    instance.rmdir()
                with patch(
                    "narwhal.diagnostics.management_collection.run_command",
                    side_effect=self.command,
                ):
                    collected = await collect_diagnostics(self.access(), "fleet-a")
                self.assertEqual(collected.data["collection_status"], "partial")
                manifest = self.stored_json(self.access(), collected.data["manifest_artifact_id"])
                self.assertEqual(
                    sum(
                        row["kind"] == "http" and row["status"] == "ok"
                        for row in manifest["sources"]
                    ),
                    5,
                )
                self.assert_cleaned()
