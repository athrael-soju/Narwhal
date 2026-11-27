"""The deployment status and restart counts that routers and sidecars export."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.contracts import DEPLOYMENT_STATUS, versioned
from narwhal.deployment import deployment_status
from narwhal.observability import restarts
from narwhal.observability.metrics.deployment import read_status, render_deployment
from narwhal.serving.app import create_app
from narwhal.types import Role

ORDER = ("install", "fabric-edge", "attest-capture", "profile", "calibrate", "preflight", "trial")


def entry(stage, instance="", status="passed", ended=100.0, **products):
    return {
        "stage": stage,
        "instance": instance,
        "status": status,
        "directory": f"stages/{stage}/{instance}/attempt-1".replace("//", "/"),
        "products": products,
        "started_at": ended - 1,
        "ended_at": ended if status == "passed" else None,
    }


class StatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        edge = self.root / "stages/fabric-edge/node-1-to-node-2/attempt-1"
        edge.mkdir(parents=True)
        (edge / deployment_status.EDGE_RESULTS).write_text(
            json.dumps(
                [
                    {"measured_gbps": 80.0, "roles": {"engine-1": {"required_gbps": 2.0}}},
                    {"measured_gbps": 70.0, "roles": {"engine-1": {"required_gbps": 3.0}}},
                ]
            )
        )

    def record(self, preflight="passed"):
        stages = [
            entry("install", "node-1", ended=10),
            entry("fabric-edge", "node-1-to-node-2", ended=20),
            entry("attest-capture", "engine-1", ended=30, binding="sha256:" + "b" * 64),
            entry("profile", ended=40),
            entry("calibrate", ended=50),
            entry("preflight", "node-1", status=preflight, ended=60),
        ]
        return {"run_id": "r" * 32, "stages": {str(i): s for i, s in enumerate(stages)}}

    def build(self, record, previous=None):
        return deployment_status.build(
            record, self.root, ORDER, "preflight", "fleet-a", "ssh", previous
        )

    def test_a_passed_run_records_its_times_edges_and_engines(self):
        status = self.build(self.record())
        self.assertEqual(status["last_passed_at"], 60)
        self.assertEqual(
            (status["qualified_at"], status["profiled_at"], status["calibrated_at"]), (60, 40, 50)
        )
        self.assertEqual(
            status["fabric"],
            [{"edge": "node-1-to-node-2", "measured_gbps": 70.0, "budget_gbps": 3.0}],
        )
        self.assertEqual(status["engines"][0]["iid"], "n1")
        lines = render_deployment(status)
        self.assertIn("narwhal_deployment_last_passed_timestamp_seconds 60.0", lines)
        self.assertIn('narwhal_fabric_edge_budget_gbps{edge="node-1-to-node-2"} 3.0', lines)

    def test_a_failed_stage_keeps_the_last_passed_run_and_exports_its_state(self):
        before = self.build(self.record())
        status = self.build(self.record(preflight="failed"), previous=before)
        self.assertEqual(status["last_passed_at"], 60)
        self.assertIsNone(status["qualified_at"])
        self.assertIn(
            'narwhal_deployment_stage_state{stage="preflight",stage_instance="node-1",'
            'state="failed"} 1',
            render_deployment(status),
        )

    def test_an_unreadable_document_exports_nothing(self):
        path = self.root / "status.json"
        self.assertIsNone(read_status(path))
        path.write_text('{"schema": "other"}')
        self.assertIsNone(read_status(path))
        self.assertEqual(render_deployment(None), [])


class RouterExportTests(unittest.TestCase):
    def test_the_router_exports_the_status_and_its_unit_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "status.json"
            body = {"run_id": "r", "target": "ssh", "stages": [], "fabric": [], "engines": []}
            path.write_text(json.dumps(versioned(DEPLOYMENT_STATUS, body)))
            cfg = FleetConfig(
                model="stub",
                slo=SLO(ttft_s=1.0, tpot_s=0.02),
                engines=[EngineSpec("p", "http://prefill", Role.PREFILL)],
                profiles_path=root / "profiles.json",
                monitor_interval_s=60,
            )
            with (
                patch.dict("os.environ", {restarts.UNIT_ENV: "narwhal-x-router.service"}),
                patch.object(restarts, "systemd_restarts", return_value=3),
            ):
                app = create_app(cfg, deployment_status=path)
                text = TestClient(app).get("/metrics").text
        self.assertIn('narwhal_deployment_info{run_id="r",target="ssh"} 1', text)
        self.assertIn('narwhal_supervisor_restarts{process="router"} 3', text)


class RestartTests(unittest.TestCase):
    def test_pod_restarts_count_each_container(self):
        pod = {
            "status": {
                "containerStatuses": [
                    {"name": "engine", "restartCount": 2},
                    {"name": "attestation", "restartCount": 0},
                ]
            }
        }
        self.assertEqual(restarts.pod_restarts(pod), {"engine": 2, "attestation": 0})

    def test_a_reading_is_cached(self):
        clock = [0.0]
        reads = []
        cached = restarts.Cached(
            lambda: reads.append(1) or {"router": len(reads)}, lambda: clock[0]
        )
        self.assertEqual(cached(), {"router": 1})
        clock[0] = restarts.CACHE_S - 1
        self.assertEqual(cached(), {"router": 1})
        clock[0] = restarts.CACHE_S
        self.assertEqual(cached(), {"router": 2})


if __name__ == "__main__":
    unittest.main()
