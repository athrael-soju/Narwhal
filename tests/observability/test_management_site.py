"""Exercise registered SSH observation with bounded synthetic responses."""

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from narwhal.deployment.management_access import AccessError, InspectionAccess
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.observability.management_site import RegisteredSiteProvider


class SiteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write(
            "hosts.json",
            {"hosts": [{"id": "one", "ssh_env": "TEST_SSH", "roles": ["router", "engine-1"]}]},
        )
        self.write(
            "settings.json",
            {
                "schema": "narwhal.ssh-settings",
                "schema_version": 1,
                "source_root": str(self.root),
                "source_commit": "a" * 40,
                "hosts_path": str(self.root / "hosts.json"),
                "launch_path": str(self.root / "launch.json"),
                "known_hosts_path": str(self.root / "known_hosts"),
                "remote_root": "/private/remote",
            },
        )
        self.write("recipe.json", {"schema": "narwhal.ssh-recipe", "schema_version": 1})
        self.write(
            "fleet.json",
            {
                "schema": "narwhal.fleet",
                "schema_version": 1,
                "engines": [{"iid": "n1", "url": "http://192.0.2.1:8000"}],
            },
        )
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": str(uuid4()),
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "fleet",
                    "kind": "fleet",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "artifacts"),
                    "fleet_file": str(self.root / "fleet.json"),
                    "instance_dir": None,
                    "adapter": {"id": "ssh-v1", "settings_path": str(self.root / "settings.json")},
                    "recipes": [
                        {"id": "site", "kind": "fleet", "path": str(self.root / "recipe.json")}
                    ],
                    "credential_env": ["TEST_SECRET"],
                    "logs": [
                        {"id": "engine", "host_id": "one", "source": "/private/engine.log"},
                        {"id": "journal", "host_id": "one", "source": "/private/journal.jsonl"},
                    ],
                }
            ],
        }
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        self.target = self.registry.targets[0]
        self.provider = RegisteredSiteProvider(InspectionAccess(self.registry))
        environment = patch.dict(
            "os.environ", {"TEST_SSH": "root@fixture.invalid", "TEST_SECRET": "private-value"}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    async def test_logs_use_only_registration_and_redact_content_before_export(self):
        raw = b'{"prompt":"request-text", "credential":"private-value", "event":"started"}\n'
        response = {
            "data_base64": base64.b64encode(raw).decode(),
            "offset": 0,
            "size_bytes": len(raw),
            "complete": True,
        }
        with patch(
            "narwhal.observability.management_site.inspect_rpc",
            new_callable=AsyncMock,
            return_value=response,
        ) as rpc:
            result = await self.provider.collect(
                self.target, "log", "engine", deadline=time.monotonic() + 2, max_bytes=65536
            )
        self.assertNotIn(b"request-text", result[0].content)
        self.assertNotIn(b"private-value", result[0].content)
        self.assertIn(b"started", result[0].content)
        self.assertEqual(rpc.call_args.args[3]["parameters"]["path"], "/private/engine.log")

    async def test_unknown_subject_and_request_content_log_are_rejected_before_ssh(self):
        with patch(
            "narwhal.observability.management_site.inspect_rpc", new_callable=AsyncMock
        ) as rpc:
            for kind, subject in (("log", "unknown"), ("log", "journal"), ("inventory", "other")):
                with self.subTest(subject=subject), self.assertRaises(AccessError):
                    await self.provider.collect(
                        self.target, kind, subject, deadline=time.monotonic() + 2, max_bytes=65536
                    )
        rpc.assert_not_called()

    async def test_incomplete_gpu_inventory_remains_an_incomplete_capture(self):
        with patch(
            "narwhal.observability.management_site.inspect_rpc",
            new_callable=AsyncMock,
            return_value={"gpus": [], "gpu_clients": {"complete": False, "processes": []}},
        ):
            result = await self.provider.collect(
                self.target, "inventory", "one", deadline=time.monotonic() + 2, max_bytes=65536
            )
        self.assertFalse(result[0].complete)
        self.assertEqual(result[0].error_code, "source_unavailable")

    async def test_monitoring_binding_uses_recorded_host_local_urls_and_owned_services(self):
        owner = {
            "operation_id": str(uuid4()),
            "stage_id": "fleet-monitor",
            "launch_token": str(uuid4()),
        }
        state = {
            "recipe_id": "site",
            "router": {"host_id": "one", "url": "http://127.0.0.1:8000"},
            "monitoring": {
                "host_id": "one",
                "prometheus_url": "http://127.0.0.1:9090",
                "effect": {"host_id": "one", "owner": owner},
                "containers": {"prometheus": {"cid": "a"}, "grafana": {"cid": "b"}},
                "binding": {
                    "host_id": "one",
                    "datasource_url": "http://127.0.0.1:9090",
                    "prometheus_version": "3.14.0",
                    "grafana_version": "13.2.1",
                    "targets": {"router": "127.0.0.1:8000", "engines": [["n1", "192.0.2.1:8000"]]},
                },
            },
        }
        responses = [
            {"owner": owner, "state": "retained", "supervisor_present": True},
            {
                "containers": [
                    {"container_id": "a", "running": True},
                    {"container_id": "b", "running": True},
                ]
            },
        ]
        with (
            patch("narwhal.observability.management_site.deployment_state", return_value=state),
            patch(
                "narwhal.observability.management_site.inspect_rpc",
                new_callable=AsyncMock,
                side_effect=responses,
            ),
        ):
            binding = await self.provider.monitoring_binding(
                self.target, deadline=time.monotonic() + 2
            )
        self.assertEqual(binding.datasource_url, "http://127.0.0.1:9090")
        self.assertEqual(binding.targets.engines, (("n1", "192.0.2.1:8000"),))
        with (
            patch("narwhal.observability.management_site.deployment_state", return_value=state),
            patch(
                "narwhal.observability.management_site.inspect_rpc",
                new_callable=AsyncMock,
                return_value={
                    "owner": owner,
                    "state": "recovery_required",
                    "supervisor_present": False,
                },
            ),
            self.assertRaises(AccessError),
        ):
            await self.provider.monitoring_binding(self.target, deadline=time.monotonic() + 2)
