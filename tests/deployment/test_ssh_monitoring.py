"""Require labelled container and volume ownership before monitoring retention."""

import copy
import json
import os
import tempfile
import time
import unittest
from dataclasses import dataclass
from importlib import import_module
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
        data_root = Path("/private/$literal/monitoring-data/project")
        project, override = monitoring.owned_override(OWNER, data_root)
        self.assertEqual(project, "narwhal-20000000000040008000000000000001")
        self.assertEqual(set(override["services"]), {"prometheus", "grafana"})
        self.assertEqual(set(override["volumes"]), {"prom-data", "grafana-data"})
        for group in override.values():
            for row in group.values():
                self.assertEqual(row["labels"], _labels(OWNER))
        for name, row in override["volumes"].items():
            self.assertEqual(row["driver"], "local")
            self.assertEqual(
                row["driver_opts"],
                {"type": "none", "o": "bind", "device": str(data_root / name).replace("$", "$$")},
            )

    def request(self, directory):
        root = Path(directory) / "remote-$literal"
        operation = root / "operations" / OWNER["operation_id"]
        job = operation / "jobs" / OWNER["launch_token"]
        for path in (root, root / "operations", operation, operation / "jobs", job):
            path.mkdir(mode=0o700)
        fleet = operation / "fleet.json"
        fleet.write_text("{}")
        return {
            "owner": OWNER,
            "operation_root": str(operation),
            "result_path": str(job / "result.json"),
            "fleet": str(fleet),
            "router_url": "http://127.0.0.1:8000",
            "ready_seconds": 10,
            "freshness_s": 60,
            "host_id": "router",
        }

    def test_volume_backing_is_empty_private_and_bound_to_the_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            request = self.request(directory)
            data = monitoring._data_root(request)
            self.assertEqual(
                data,
                Path(request["operation_root"])
                / "monitoring-data"
                / ("narwhal-" + OWNER["launch_token"].replace("-", "")),
            )
            self.assertEqual({path.name for path in data.iterdir()}, {"prom-data", "grafana-data"})
            for path in (data.parent, data, *data.iterdir()):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_uid, os.getuid())
            for path in data.iterdir():
                self.assertEqual(list(path.iterdir()), [])
            retained = data / "prom-data" / "retained"
            retained.write_text("existing evidence")
            with self.assertRaises(FileExistsError):
                monitoring._data_root(request)
            self.assertEqual(retained.read_text(), "existing evidence")

    def test_volume_backing_rejects_changed_operation_or_result_identity(self):
        for change in ("operation", "result", "launch", "relative", "traversal"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                request = self.request(directory)
                root = Path(request["operation_root"])
                if change == "operation":
                    request["operation_root"] = str(root.with_name(OWNER["launch_token"]))
                elif change == "result":
                    request["result_path"] = str(root / "other" / "result.json")
                elif change == "launch":
                    request["owner"] = {**OWNER, "launch_token": "../../outside"}
                elif change == "relative":
                    request["operation_root"] = "operations/" + OWNER["operation_id"]
                else:
                    request["operation_root"] = str(root / ".." / OWNER["operation_id"])
                with self.assertRaises(ValueError):
                    monitoring._data_root(request)
                self.assertFalse((root / "monitoring-data").exists())

    def test_volume_backing_rejects_symlinks_and_nonprivate_parents(self):
        for change in ("root", "data", "project", "volume", "mode", "owner"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                request = self.request(directory)
                root = Path(request["operation_root"])
                data = root / "monitoring-data"
                project = data / ("narwhal-" + OWNER["launch_token"].replace("-", ""))
                outside = Path(directory) / "outside"
                outside.mkdir(mode=0o700)
                marker = outside / "evidence"
                marker.write_text("untouched")
                if change == "root":
                    moved = root.with_name("original")
                    root.rename(moved)
                    root.symlink_to(moved, target_is_directory=True)
                elif change == "data":
                    data.symlink_to(outside, target_is_directory=True)
                elif change == "project":
                    data.mkdir(mode=0o700)
                    project.symlink_to(outside, target_is_directory=True)
                elif change == "volume":
                    data.mkdir(mode=0o700)
                    project.mkdir(mode=0o700)
                    (project / "prom-data").symlink_to(outside, target_is_directory=True)
                elif change == "mode":
                    root.chmod(0o755)
                if change == "owner":
                    with (
                        patch(
                            "narwhal.deployment.ssh_files.os.getuid", return_value=os.getuid() + 1
                        ),
                        self.assertRaisesRegex(ValueError, "private ownership"),
                    ):
                        monitoring._data_root(request)
                else:
                    with self.assertRaises((OSError, ValueError)):
                        monitoring._data_root(request)
                self.assertEqual(marker.read_text(), "untouched")
                self.assertEqual(outside.stat().st_mode & 0o777, 0o700)
                self.assertEqual(list(outside.iterdir()), [marker])

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
            root=Path("/registered/operations") / OWNER["operation_id"],
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
        self.assertEqual(session.gate.call_args.args[2]["operation_root"], str(session.root))
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

        for change in (None, "readiness", "driver", "options", "device", "labels", "malformed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                request = self.request(directory)
                root = Path(request["result_path"]).parent
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
                data = (
                    Path(request["operation_root"])
                    / "monitoring-data"
                    / ("narwhal-" + OWNER["launch_token"].replace("-", ""))
                )
                project, override = monitoring.owned_override(OWNER, data)
                volumes = [
                    {
                        "Name": project + "_" + name,
                        "Driver": "local",
                        "Labels": _labels(OWNER),
                        "Options": {"type": "none", "o": "bind", "device": str(data / name)},
                    }
                    for name in ("prom-data", "grafana-data")
                ]
                if change == "driver":
                    volumes[0]["Driver"] = "other-driver"
                elif change == "options":
                    volumes[0]["Options"] = {**volumes[0]["Options"], "o": "bind,ro"}
                elif change == "device":
                    volumes[0]["Options"] = {**volumes[0]["Options"], "device": "/foreign/data"}
                elif change == "labels":
                    volumes[0]["Labels"] = {"io.narwhal.management.operation": "other"}
                elif change == "malformed":
                    volumes[0] = "invalid"
                with (
                    patch.object(
                        monitoring.importlib,
                        "import_module",
                        side_effect=lambda name, *args, startup=startup: (
                            startup
                            if name == "tools.observability.start"
                            else import_module(name, *args)
                        ),
                    ),
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
                    if change == "readiness":
                        observe.return_value = {
                            "readiness": "fail",
                            "sources": [{"raw_body": b"partial"}],
                        }
                    if change is not None:
                        with self.assertRaises(ValueError):
                            monitoring.start_remote(request)
                        if change == "readiness":
                            failure = json.loads((root / "monitoring.failed.json").read_bytes())
                            self.assertEqual(failure["sources"][0]["retained_prefix"], "partial")
                    else:
                        result = monitoring.start_remote(request)
                        self.assertEqual(
                            {row["name"] for row in result["volumes"]},
                            {row["Name"] for row in volumes},
                        )
                        self.assertEqual(
                            json.loads((root / "compose.owner.json").read_bytes()), override
                        )
                        for row in result["volumes"]:
                            name = row["name"].removeprefix(project + "_")
                            self.assertEqual(
                                row["options"],
                                {"type": "none", "o": "bind", "device": str(data / name)},
                            )
                            self.assertEqual(row["backing_path"], str(data / name))
