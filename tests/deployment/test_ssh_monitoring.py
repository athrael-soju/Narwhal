"""Require labelled container and volume ownership before monitoring retention."""

import copy
import json
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from narwhal.deployment import ssh_monitoring as monitoring
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.ssh_worker import _labels
from narwhal.observability.management_targets import TargetContract

OWNER = {
    "operation_id": "10000000-0000-4000-8000-000000000001",
    "stage_id": "fleet-monitor",
    "launch_token": "20000000-0000-4000-8000-000000000001",
}


class MonitoringTests(unittest.TestCase):
    def test_compose_override_labels_services_and_retained_data_volumes(self):
        project, override = monitoring.owned_override(OWNER)
        self.assertEqual(project, "narwhal-20000000000040008000000000000001")
        self.assertEqual(set(override["services"]), {"prometheus", "grafana"})
        self.assertEqual(set(override["volumes"]), {"prom-data", "grafana-data"})
        for group in override.values():
            for row in group.values():
                self.assertEqual(row["labels"], _labels(OWNER))

    def session(self, observed=None):
        effect = {"owner": OWNER}
        result = {
            "observation": {"readiness": "pass"},
            "containers": {"prometheus": {"cid": "prom"}, "grafana": {"cid": "graf"}},
        }
        rows = [
            {"container_id": name, "running": True, "labels": _labels(OWNER)}
            for name in ("prom", "graf")
        ]
        return SimpleNamespace(
            state={"router": {"fleet_path": "/fleet", "url": "http://127.0.0.1:8000"}},
            router_host="router",
            context=SimpleNamespace(
                target=SimpleNamespace(freshness_s=60), deadline=time.monotonic() + 10
            ),
            gate=Mock(return_value=(effect, result)),
            transport=SimpleNamespace(
                inspect_containers=Mock(
                    return_value={"containers": rows if observed is None else observed}
                )
            ),
            retain=Mock(),
            save=Mock(),
        )

    def test_start_retains_only_exact_live_owned_container_ids(self):
        session = self.session()
        monitoring.start(session)
        session.retain.assert_called_once()
        self.assertIs(session.state["monitoring"]["effect"]["owner"], OWNER)
        for change in ("id", "owner", "stopped"):
            observed = copy.deepcopy(
                session.transport.inspect_containers.return_value["containers"]
            )
            if change == "id":
                observed[0]["container_id"] = "replacement"
            elif change == "owner":
                observed[0]["labels"] = {}
            else:
                observed[0]["running"] = False
            altered = self.session(observed)
            with self.subTest(change=change), self.assertRaises(OperationError):
                monitoring.start(altered)
            altered.retain.assert_not_called()

    def test_transient_http_error_is_retryable_by_existing_startup_checks(self):
        with (
            patch.object(monitoring, "_get_body", side_effect=httpx.ConnectError("fixture")),
            self.assertRaises(OSError),
        ):
            monitoring._get("http://127.0.0.1:9090", 1)

    def test_remote_result_preserves_owned_volume_ids_and_failed_prefixes(self):
        @dataclass
        class Container:
            cid: str

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fleet.json").write_text("{}")
            request = {
                "owner": OWNER,
                "result_path": str(root / "result.json"),
                "fleet": str(root / "fleet.json"),
                "router_url": "http://127.0.0.1:8000",
                "ready_seconds": 10,
                "freshness_s": 60,
                "host_id": "router",
            }
            stack = SimpleNamespace(_prefix=[])
            startup = SimpleNamespace(
                ComposeStack=Mock(return_value=stack),
                start=Mock(
                    return_value={name: Container(name) for name in ("prometheus", "grafana")}
                ),
                configured_services=lambda _: [
                    SimpleNamespace(name=name, listener=SimpleNamespace(authority=authority))
                    for name, authority in (
                        ("prometheus", "127.0.0.1:9090"),
                        ("grafana", "127.0.0.1:3000"),
                    )
                ],
            )
            project, _ = monitoring.owned_override(OWNER)
            volumes = [
                {"Name": project + "_" + name, "Driver": "local", "Labels": _labels(OWNER)}
                for name in ("prom-data", "grafana-data")
            ]
            with (
                patch.object(monitoring.importlib, "import_module", return_value=startup),
                patch.object(
                    monitoring,
                    "build_targets",
                    return_value=TargetContract("router:8000", (("e0", "engine:8000"),)),
                ),
                patch.object(
                    monitoring,
                    "observe_monitoring",
                    AsyncMock(return_value={"readiness": "pass", "sources": []}),
                ) as observe,
                patch.object(monitoring, "_command", return_value=json.dumps(volumes)),
            ):
                result = monitoring.start_remote(request)
                self.assertEqual(
                    {row["name"] for row in result["volumes"]}, {row["Name"] for row in volumes}
                )
                (root / "compose.owner.json").unlink()
                observe.return_value = {"readiness": "fail", "sources": [{"raw_body": b"partial"}]}
                with self.assertRaises(ValueError):
                    monitoring.start_remote(request)
                failure = json.loads((root / "monitoring.failed.json").read_bytes())
                self.assertEqual(failure["sources"][0]["retained_prefix"], "partial")
