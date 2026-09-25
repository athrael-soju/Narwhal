"""Enforce shared GPU allowance through prepared plans with synthetic Docker and GPU inputs."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from narwhal.deployment import stages
from narwhal.deployment.launch_engine import check, load, prepare, start_shared
from tests.deployment.fixtures import launcher_inputs


class ContainerAllowanceTests(unittest.TestCase):
    @contextlib.contextmanager
    def fleet(self, readings, *, fail_start=None, fail_cleanup=None, reconciled_timeout=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runs = []
            plans = {}
            ids = {}
            external_id = "f" * 64
            containers = {external_id: "running"}
            removals = []
            image = "sha256:" + "a" * 64

            def docker(command, run, log):
                action = command[0]
                if action == "image":
                    return json.dumps([{"Id": image}])
                if action == "run":
                    return (
                        "NARWHAL_TOKENIZER_READY=1\n"
                        'NARWHAL_IMAGE_RUNTIME={"vllm_api_version": "0.29.0"}'
                    )
                if action == "create":
                    cid = ids[run]
                    containers[cid] = "created"
                    return cid
                if action == "start":
                    if run.name == fail_start:
                        if reconciled_timeout:
                            cid = command[1]
                            del containers[cid]
                            raise stages.StageTimeout(
                                "docker-start",
                                {
                                    "budget_seconds": 0.1,
                                    "evidence": str(run / "stage.json"),
                                    "recovery": "inspect recorded container IDs",
                                    "docker_reconciliation": {
                                        "removed": [cid],
                                        "surviving_resources": [],
                                    },
                                },
                            )
                        raise ValueError("synthetic Docker start failure")
                    containers[command[1]] = "running"
                    return command[1]
                if action == "rm":
                    self.assertEqual(command[:2], ["rm", "--force"])
                    cid = command[2]
                    removals.append(cid)
                    if run.name == fail_cleanup:
                        raise ValueError("synthetic Docker removal failure")
                    del containers[cid]
                    return cid
                if action == "inspect":
                    cid = command[-1]
                    self.assertIn(cid, containers)
                    if command[2] == "{{json .State}}":
                        return json.dumps({"Running": containers[cid] == "running", "Pid": 1234})
                    if command[2] == "{{json .Config.Cmd}}":
                        return json.dumps(plans[run]["args"])
                    if command[2] == "{{.Image}}":
                        return image
                self.fail(f"unexpected Docker command: {command}")

            measurements = iter(readings)

            def gpu_memory(gpu_uuid):
                self.assertEqual(gpu_uuid, "GPU-test")
                return {"used_mib": next(measurements), "total_mib": 20000}

            response = MagicMock()
            response.__enter__.return_value.status = 200
            with (
                patch("narwhal.deployment.launch_engine.docker", side_effect=docker),
                patch("narwhal.deployment.launch_engine.gpu_memory", side_effect=gpu_memory),
                patch("narwhal.deployment.launch_engine.urlopen", return_value=response),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for number in (1, 2):
                    source = root / f"source-{number}"
                    source.mkdir()
                    record, env = launcher_inputs(source)
                    record.update(
                        role=f"engine-{number}",
                        gpu_visibility_env="CUDA_VISIBLE_DEVICES",
                        gpu_ids=["GPU-test"],
                        tensor_parallel_size=1,
                        environment={
                            "CUDA_VISIBLE_DEVICES": "GPU-test",
                            "UCX_NET_DEVICES": "fabric0",
                        },
                        shared_device={
                            "group": "node-1:GPU-test",
                            "gpu_uuid": "GPU-test",
                            "device_allowance": 0.5,
                            "gpu_memory_utilization": 0.2,
                        },
                    )
                    record["transfer"]["gpu_tls"] = "cuda"
                    record["runtime"]["extra_args"].extend(["--gpu-memory-utilization", "0.2"])
                    Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(record))
                    env.update(
                        NARWHAL_ENGINE_PORT=str(8000 + number),
                        NARWHAL_ATTEST_PORT=str(8100 + number),
                        NARWHAL_NIXL_SIDE_CHANNEL_PORT=str(5600 + number),
                    )
                    env[f"NARWHAL_NODE_{number}_IP"] = "192.0.2.11"
                    env[f"NARWHAL_NODE_{number}_URL"] = f"http://192.0.2.11:{8000 + number}"
                    run = root / f"engine-{number}"
                    prepare(run, env)
                    plans[run] = load(run)
                    check(run, plans[run])
                    runs.append(run)
                    ids[run] = str(number) * 64
                yield runs, ids, containers, removals, external_id

    def test_observed_overage_removes_owned_containers_and_retains_failure(self):
        with self.fleet((1000, 8000, 8000, 14000)) as fleet:
            runs, ids, containers, removals, external_id = fleet
            with self.assertRaisesRegex(
                ValueError, "13000 MiB above the 1000 MiB baseline exceeds the 10000.0 MiB"
            ):
                start_shared(runs, 30)
            self.assertEqual(removals, [ids[runs[1]], ids[runs[0]]])
            self.assertEqual(containers, {external_id: "running"})
            for index, run in enumerate(runs):
                record = json.loads((run / "shared-start.json").read_text())
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["cleanup_status"], "removed")
                self.assertEqual(record["container_id"], ids[run])
                self.assertEqual(record["gpu_baseline_used_mib"], 1000)
                self.assertEqual(record["device_allowance_mib"], 10000)
                self.assertEqual(record["aggregate_delta_mib"], (7000, 13000)[index])
                self.assertEqual(record["gpu_after"]["used_mib"], (8000, 14000)[index])
                self.assertEqual((run / "shared-start.json").stat().st_mode & 0o777, 0o600)
                self.assertIn("13000 MiB", record["error"])

    def test_rollback_preserves_confirmed_daemon_cleanup_after_start_timeout(self):
        with self.fleet(
            (1000, 5000, 5000, 5000), fail_start="engine-2", reconciled_timeout=True
        ) as fleet:
            runs, ids, containers, removals, external_id = fleet
            with self.assertRaises(stages.StageTimeout) as caught:
                start_shared(runs, 30)
            self.assertEqual(removals, [ids[runs[0]]])
            self.assertEqual(containers, {external_id: "running"})
            self.assertNotIn("container_cleanup_errors", caught.exception.context)
            for run in runs:
                record = json.loads((run / "shared-start.json").read_text())
                self.assertEqual(record["cleanup_status"], "removed")
            failed = json.loads((runs[1] / "shared-start.json").read_text())
            self.assertEqual(failed["failure_stage"], "docker-start")
            self.assertEqual(
                failed["failure_context"]["docker_reconciliation"]["removed"], [ids[runs[1]]]
            )

    def test_observed_boundary_excludes_baseline_and_preserves_running_containers(self):
        with self.fleet((1000, 8000, 8000, 11000)) as fleet:
            runs, ids, containers, removals, external_id = fleet
            start_shared(runs, 30)
            self.assertEqual(removals, [])
            self.assertEqual(set(containers), {external_id, *ids.values()})
            record = json.loads((runs[1] / "shared-start.json").read_text())
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["gpu_baseline_used_mib"], 1000)
            self.assertEqual(record["observed_delta_mib"], 3000)
            self.assertEqual(record["aggregate_delta_mib"], 10000)
            self.assertEqual(record["device_allowance_mib"], 10000)

    def test_first_container_overage_stops_before_creating_second(self):
        with self.fleet((1000, 11001)) as fleet:
            runs, ids, containers, removals, external_id = fleet
            with self.assertRaisesRegex(ValueError, "10001 MiB"):
                start_shared(runs, 30)
            self.assertEqual(removals, [ids[runs[0]]])
            self.assertEqual(containers, {external_id: "running"})
            self.assertFalse((runs[1] / "container.id").exists())
            self.assertFalse((runs[1] / "shared-start.json").exists())

    def test_cleanup_failure_retains_cause_and_continues_removing_owned_containers(self):
        with self.fleet((1000, 8000, 8000, 14000), fail_cleanup="engine-2") as fleet:
            runs, ids, containers, removals, external_id = fleet
            with self.assertRaisesRegex(
                ValueError, "13000 MiB.*container cleanup failed:.*synthetic Docker removal failure"
            ):
                start_shared(runs, 30)
            self.assertEqual(removals, [ids[runs[1]], ids[runs[0]]])
            self.assertEqual(set(containers), {external_id, ids[runs[1]]})
            record = json.loads((runs[1] / "shared-start.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["cleanup_status"], "failed")
            self.assertIn("13000 MiB", record["error"])
            self.assertEqual(record["cleanup_error"], "synthetic Docker removal failure")

    def test_docker_start_failure_removes_created_container_and_prior_ready_container(self):
        with self.fleet((1000, 8000, 8000, 8000), fail_start="engine-2") as fleet:
            runs, ids, containers, removals, external_id = fleet
            with self.assertRaisesRegex(ValueError, "synthetic Docker start failure"):
                start_shared(runs, 30)
            self.assertEqual(removals, [ids[runs[1]], ids[runs[0]]])
            self.assertEqual(containers, {external_id: "running"})
            record = json.loads((runs[1] / "shared-start.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["cleanup_status"], "removed")


if __name__ == "__main__":
    unittest.main()
