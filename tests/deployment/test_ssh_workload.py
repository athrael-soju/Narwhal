"""Exercise fixed Gate G clients, remote journal reconciliation and acceptance gates."""

import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from narwhal.deployment import ssh_workload as workload
from narwhal.deployment.management_access import InspectionAccess
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementTarget
from narwhal.deployment.ssh_settings import LoadRecipe
from narwhal.diagnostics.bundle import Redactor
from narwhal.diagnostics.management_artifacts import ArtifactStore
from tests.deployment.test_ssh_monitoring import OWNER
from tests.diagnostics.test_management_artifacts import REGISTRY_ID, target_document
from tests.measurement import test_benchmark_evidence as benchmark_fixture


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.recipe = LoadRecipe(rates=[1.0, 2.0], requests=3)

    def test_registered_recipe_becomes_fixed_client_with_explicit_units(self):
        point = workload.point_plan(self.recipe, 1, self.root, self.root / "seed.json")
        arguments = point["client_argv"]
        self.assertEqual(arguments[1], str(self.root / "tools/measurement/load_trial.py"))
        self.assertEqual(arguments[arguments.index("--rate") + 1], "2.0")
        self.assertEqual(arguments[arguments.index("--ttft") + 1], "2.0")
        self.assertEqual(arguments[arguments.index("--tpot") + 1], "0.0333")
        self.assertEqual(point["workload"]["seed"], 1729)
        self.assertGreater(point["client_timeout_s"], 3 / 2 + 120)

    def test_altered_command_and_invalid_point_are_rejected_before_ssh(self):
        root = self.root / "rate-01"
        point = workload.point_plan(self.recipe, 0, self.root, self.root / "seed/workload.json")
        request = {
            "source_root": str(self.root),
            "recipe": self.recipe.model_dump(),
            "mode": "point",
            "out": str(root),
            "point": point,
        }
        cases = []
        altered = copy.deepcopy(request)
        altered["point"]["client_argv"] = ["arbitrary-command"]
        cases.append(altered)
        altered = copy.deepcopy(request)
        altered["point"]["id"] = "rate-00"
        cases.extend([altered, request | {"mode": "arbitrary"}])
        for altered in cases:
            with patch.object(workload, "forward") as forward:
                with self.assertRaises(ValueError):
                    workload.local_helper(altered)
                forward.assert_not_called()

    def test_terminal_receipt_requires_proven_absence_without_cleanup_errors(self):
        for state, extra, absent in (
            ("succeeded", {}, True),
            ("failed", {}, True),
            ("cancelled", {}, True),
            ("timed_out", {}, True),
            ("recovery_required", {}, False),
            ("failed", {"cleanup": {"error": "inspection failed"}}, False),
            ("failed", {"container_error": "inspection failed"}, False),
            ("failed", {"observed_processes": [123]}, False),
        ):
            effect = {"owner": OWNER, "host_id": "router", "effect": "confirmed"}
            context = SimpleNamespace(assert_current=Mock(), record_effect=Mock())
            receipt = {"owner": OWNER, "state": state, "job_id": OWNER["launch_token"], **extra}
            session = SimpleNamespace(
                context=context, transport=SimpleNamespace(status=Mock(return_value=receipt))
            )
            with self.subTest(state=state, extra=extra):
                if state == "succeeded":
                    workload._await(session, effect)
                else:
                    with self.assertRaises(OperationError):
                        workload._await(session, effect)
                self.assertEqual(context.record_effect.called, absent)
                self.assertEqual(effect["effect"], "absent" if absent else "confirmed")

    def test_public_exports_are_redacted_immutable_utf8_chunks(self):
        target = ManagementTarget.model_validate_json(json.dumps(target_document(self.root)))
        access = InspectionAccess(SimpleNamespace(targets=(target,)))
        record = {"artifacts": []}
        context = SimpleNamespace(
            registry=SimpleNamespace(registry_id=REGISTRY_ID),
            target=target,
            access=access,
            redactor=Redactor(False),
            assert_current=Mock(),
            update=lambda change: change(record),
        )
        path = self.root / "evidence.json"
        path.write_text(
            json.dumps(
                {"name": "🐋" * 25, "authorization": "Bearer private-value"}, ensure_ascii=False
            )
        )
        path.chmod(0o600)
        with patch.object(workload, "EXPORT_CHUNK_BYTES", 31):
            refs = workload._exports(SimpleNamespace(context=context), [path], "workload")
        self.assertGreater(len(refs), 1)
        store = ArtifactStore(REGISTRY_ID, target)
        content = "".join(store.read(ref["artifact_id"])["text"] for ref in refs)
        self.assertNotIn("private-value", content)
        self.assertEqual(json.loads(content)["name"], "🐋" * 25)
        self.assertEqual(record["artifacts"], refs)
        self.assertTrue(all(ref["size_bytes"] <= 31 for ref in refs))

    def session_for_accept(self):
        return SimpleNamespace(
            recipe=SimpleNamespace(load=self.recipe),
            state={
                "workload": {
                    "operation_id": "current",
                    "complete": True,
                    "points": [
                        {
                            "point": f"rate-{index + 1:02d}",
                            "rate_rps": rate,
                            "evidence": {"diagnostics": []},
                            "summary": {"candidate_pass": True},
                        }
                        for index, rate in enumerate(self.recipe.rates)
                    ],
                },
                "monitoring": {
                    "binding": {},
                    "prometheus_url": "http://127.0.0.1:9090",
                    "grafana_url": "http://127.0.0.1:3000",
                },
            },
            context=SimpleNamespace(
                operation_id="current",
                output_dir=self.root,
                target=SimpleNamespace(freshness_s=60),
                read=Mock(
                    return_value={"stages": [{"stage_id": "fleet-postload", "state": "succeeded"}]}
                ),
            ),
        )

    def test_accept_requires_current_points_and_completed_postload_before_http(self):
        for change in ("old-operation", "missing-rate", "failed-journal", "postload"):
            session = self.session_for_accept()
            if change == "old-operation":
                session.state["workload"]["operation_id"] = "old"
            elif change == "missing-rate":
                session.state["workload"]["points"].pop()
            elif change == "failed-journal":
                session.state["workload"]["points"][0]["evidence"]["diagnostics"] = [
                    {"kind": "missing"}
                ]
            else:
                session.context.read.return_value["stages"][0]["state"] = "failed"
            with patch.object(workload, "_local") as local:
                with self.subTest(change=change), self.assertRaises(OperationError):
                    workload.accept(session)
                local.assert_not_called()

    def test_failed_final_monitoring_is_exported_before_failure(self):
        session = self.session_for_accept()

        def local(_, request):
            Path(request["out"]).write_text(json.dumps({"monitoring": {"readiness": "fail"}}))
            return 1

        with (
            patch.object(workload, "_local", side_effect=local),
            patch.object(
                workload, "_exports", return_value=[{"artifact_id": "retained"}]
            ) as export,
        ):
            with self.assertRaises(OperationError):
                workload.accept(session)
            export.assert_called_once()


class RemoteCollectionTests(unittest.TestCase):
    def test_real_collector_reconciles_client_journal_and_preserves_missing_rows(self):
        for missing in (False, True):
            fixture = benchmark_fixture.EvidenceTests("runTest")
            fixture.setUp()
            try:
                fixture.missing_journal = missing
                root = fixture.root / "remote-point"
                request = {
                    "directory": str(root),
                    "base": fixture.base,
                    "fleet": str(fixture.root / "fleet.json"),
                    "profiles": str(fixture.root / "profiles.json"),
                    "journal": str(fixture.journal),
                    "owner": OWNER,
                    "seconds": 10,
                    "point": {"id": "rate-01", "workload": {"rate_rps": 1, "requests": 1}},
                    "identity": {"engine_image": "fixture", "gpu_allocation": "fixture"},
                }
                fleet = SimpleNamespace(
                    engines=[SimpleNamespace(iid="e0", url=fixture.base)], engine_headers=lambda: {}
                )
                ready = threading.Event()
                from narwhal.deployment import ssh_gates

                original_write = ssh_gates.write_result

                def write(path, value, original_write=original_write, ready=ready):
                    original_write(path, value)
                    if path.name == "ready.json":
                        ready.set()

                with (
                    patch("narwhal.config.FleetConfig.load", return_value=fleet),
                    patch.object(ssh_gates, "write_result", side_effect=write),
                    ThreadPoolExecutor(max_workers=1) as pool,
                ):
                    future = pool.submit(workload.collect_remote, request)
                    self.assertTrue(
                        ready.wait(5), "Collector never acknowledged its initial sample"
                    )
                    urllib.request.urlopen(fixture.base + "/start/a").read()
                    (root / "client/requests.jsonl").write_text(
                        json.dumps({"client_rid": "a", "sent": True, "outcome": "completed"}) + "\n"
                    )
                    (root / "client/summary.json").write_text("{}")
                    original_write(root / "finish.json", {"owner": OWNER})
                    result = future.result(timeout=5)
                evidence = json.loads((root / "evidence.json").read_bytes())
                with self.subTest(missing=missing):
                    self.assertEqual(result["complete"], not missing)
                    self.assertEqual(bool(evidence["diagnostics"]), missing)
                    self.assertEqual(len(json.loads((root / "samples.json").read_bytes())), 2)
                    self.assertEqual(evidence["client"]["sent"], 1)
                    self.assertIn("prefill", evidence["identity"]["initial_role_split"])
            finally:
                fixture.doCleanups()

    def test_http_sampling_caps_bodies_and_refuses_redirects(self):
        requests = []

        def response(request):
            requests.append(request)
            if request.url.path == "/narwhal/state":
                return httpx.Response(302, headers={"location": "http://other-host/state"})
            return httpx.Response(200, content=b"x" * 100)

        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(response)
        with (
            patch.object(
                workload.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: original_client(transport=transport, **kwargs),
            ),
            patch.object(workload, "MAX_SAMPLE_SOURCE_BYTES", 50),
        ):
            sample = asyncio.run(
                workload._sample(
                    "http://router",
                    {"e0": "http://engine/metrics"},
                    {"authorization": "Bearer credential"},
                    time.monotonic() + 5,
                )
            )
        self.assertEqual(set(sample["errors"]), {"state", "router_metrics", "engine:e0"})
        self.assertEqual(len(requests), 3)
        for request in requests:
            self.assertEqual("authorization" in request.headers, request.url.host == "engine")
