"""Exercise real CPU job ownership without connecting to fleet hosts."""

import base64
import contextlib
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment import ssh_worker as worker


class RemoteJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "jobs"
        self.jobs = []
        self.addCleanup(self.cleanup_jobs)

    def cleanup_jobs(self):
        for job in self.jobs:
            with contextlib.suppress(OSError, ValueError):
                worker.cancel(self.root, job["job_id"])
                record = self.wait(job["job_id"])
                pid = record["supervisor"]["pid"]
                for _ in range(100):
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.01)

    def job(self, code, **changes):
        result = {
            "job_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "stage_id": "test",
            "fence": 1,
            "argv": [sys.executable, "-c", code],
            "cwd": str(self.directory),
            "env": {"PATH": os.defpath},
            "timeout_ms": 2000,
            "term_grace_ms": 100,
            "kill_grace_ms": 100,
            **changes,
        }
        self.jobs.append(result)
        return result

    def wait(self, job_id, states=None, timeout=5):
        states = states or worker.TERMINAL
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            record = worker.status(self.root, job_id)
            if record["state"] in states:
                return record
            time.sleep(0.01)
        self.fail(f"Remote test job did not reach {states}: {record['state']}")

    def test_duplicate_submission_runs_once_and_secrets_are_not_persisted(self):
        token = "test-private-token-value"
        job = self.job(
            "import os; from pathlib import Path; p=Path('starts'); "
            "p.write_text(p.read_text()+'1' if p.exists() else '1'); "
            "print(os.environ['API_TOKEN'])",
            env={"PATH": os.defpath, "API_TOKEN": token},
        )
        initial = worker.submit(self.root, job)
        self.assertEqual(worker.submit(self.root, job)["job_id"], initial["job_id"])
        record = self.wait(job["job_id"])
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual((self.directory / "starts").read_text(), "1")
        output = worker.read(self.root, job["job_id"], "stdout")
        self.assertEqual(base64.b64decode(output["data_base64"]).strip(), b"[redacted]")
        self.assertNotIn(token, (self.root / job["job_id"] / "request.json").read_text())
        self.assertNotIn(token, (self.root / job["job_id"] / "receipt.json").read_text())
        with self.assertRaisesRegex(worker.WorkerError, "different inputs"):
            worker.submit(self.root, {**job, "env": {**job["env"], "OTHER": "changed"}})

    def test_timeout_stops_and_reaps_forked_children(self):
        code = (
            "import os,time; from pathlib import Path; child=os.fork(); "
            "Path('child' if child==0 else 'parent').write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
        job = self.job(code, timeout_ms=250)
        worker.submit(self.root, job)
        record = self.wait(job["job_id"])
        self.assertEqual(record["state"], "timed_out")
        self.assertEqual(record["cleanup"]["surviving_processes"], {})
        for name in ("parent", "child"):
            pid = int((self.directory / name).read_text())
            value = worker._proc(pid)
            self.assertTrue(value is None or value[0] in {"Z", "X"})

    def test_service_needs_explicit_retention_then_owned_cancellation(self):
        job = self.job("import time; time.sleep(30)", timeout_ms=150)
        worker.submit(self.root, job)
        self.wait(job["job_id"], {"running"})
        worker.retain(self.root, job["job_id"])
        self.wait(job["job_id"], {"retained"})
        time.sleep(0.2)
        self.assertEqual(worker.status(self.root, job["job_id"])["state"], "retained")
        worker.cancel(self.root, job["job_id"])
        record = self.wait(job["job_id"])
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["cleanup"]["surviving_processes"], {})

    def test_sealed_context_matches_the_durable_owner(self):
        job = self.job(
            "import json; from narwhal.deployment.ssh_worker import inherited_owner; "
            "print(json.dumps(inherited_owner()))"
        )
        worker.submit(self.root, job)
        record = self.wait(job["job_id"])
        self.assertEqual(record["state"], "succeeded")
        output = worker.read(self.root, job["job_id"], "stdout")
        self.assertEqual(json.loads(base64.b64decode(output["data_base64"])), record["owner"])
        with (
            patch.dict(os.environ, {worker.CONTEXT_ENV: "999999"}),
            self.assertRaisesRegex(worker.WorkerError, "context is invalid"),
        ):
            worker.inherited_owner()

    def test_lost_supervisor_is_unknown_and_cleanup_does_not_invent_absence(self):
        job = self.job("import time; time.sleep(30)")
        worker.submit(self.root, job)
        record = self.wait(job["job_id"], {"running"})
        os.kill(record["supervisor"]["pid"], signal.SIGKILL)
        os.waitpid(record["supervisor"]["pid"], 0)
        observed = worker.status(self.root, job["job_id"])
        self.assertEqual(observed["state"], "recovery_required")
        cancelled = worker.cancel(self.root, job["job_id"])
        self.assertEqual(cancelled["state"], "recovery_required")
        self.assertEqual(cancelled["observed_processes"], {})

    def test_output_limit_stops_the_job(self):
        job = self.job(
            "import sys,time; sys.stderr.write('x'*70000); sys.stderr.flush(); time.sleep(30)"
        )
        worker.submit(self.root, job)
        record = self.wait(job["job_id"])
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error"]["code"], "source_truncated")
        self.assertEqual(record["cleanup"]["surviving_processes"], {})
        self.assertLessEqual(record["stderr_bytes"], worker.MAX_STDERR)

    def test_unsafe_job_directory_and_arbitrary_stream_are_rejected(self):
        job = self.job("print('ok')")
        self.root.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            worker.submit(self.root, job)
        self.root.unlink()
        worker.submit(self.root, job)
        self.wait(job["job_id"])
        with self.assertRaises(worker.WorkerError):
            worker.read(self.root, job["job_id"], "../request.json")


class ProbeTests(unittest.TestCase):
    def test_registered_log_tail_rejects_links_fifo_and_credential_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "engine.log"
            path.write_text("request continuation without marker\nevent=ready\n")
            path.chmod(0o600)
            result = worker.probe("log", {"path": str(path), "max_bytes": 20}, time.monotonic() + 1)
            self.assertEqual(base64.b64decode(result["data_base64"]), b"event=ready\n")
            linked = root / "alias.log"
            linked.symlink_to(path)
            fifo = root / "pipe.log"
            os.mkfifo(fifo, 0o600)
            for selected in (linked, fifo, root / ".env"):
                with (
                    self.subTest(selected=selected.name),
                    self.assertRaises((OSError, worker.WorkerError)),
                ):
                    worker.probe(
                        "log", {"path": str(selected), "max_bytes": 20}, time.monotonic() + 1
                    )

    def test_gpu_client_inventory_records_host_pid_and_device_identity(self):
        with tempfile.TemporaryFile() as device:
            path = os.readlink(f"/proc/self/fd/{device.fileno()}").removesuffix(" (deleted)")
            value = worker._gpu_clients(
                [{"device": path, "pci": "0000:01:00.0"}], time.monotonic() + 2
            )
            own = next(row for row in value["processes"] if row["pid"] == os.getpid())
            self.assertEqual(own["devices"], ["0000:01:00.0"])
            self.assertEqual(own["start_ticks"], worker._identity(os.getpid())["start_ticks"])

    def test_generation_probe_rejects_redirects_and_malformed_identity(self):
        class Handler(BaseHTTPRequestHandler):
            response = 200
            metrics = b"process_start_time_seconds 1234.5\n"

            def do_GET(self):
                body = b'{"version":"fixture-1"}' if self.path == "/version" else self.metrics
                if self.path == "/narwhal/state":
                    body = b'{"ha":{"standby":false,"epoch":0}}'
                self.send_response(self.response)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_arguments):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        parameters = {"url": f"http://127.0.0.1:{server.server_port}"}
        self.assertEqual(
            worker.probe("generation", parameters, time.monotonic() + 2),
            {"version": "fixture-1", "process_start_time_seconds": "1234.5"},
        )
        self.assertEqual(
            worker.probe("router_state", parameters, time.monotonic() + 2)["ha"],
            {"standby": False, "epoch": 0},
        )
        Handler.response = 302
        with self.assertRaises(worker.WorkerError):
            worker.probe("generation", parameters, time.monotonic() + 2)
        Handler.response = 200
        Handler.metrics = b"process_start_time_seconds NaN\n"
        with self.assertRaises(worker.WorkerError):
            worker.probe("generation", parameters, time.monotonic() + 2)

    def test_container_cleanup_requires_daemon_and_exact_labels(self):
        container_id = "a" * 64
        owner = {"operation_id": str(uuid4()), "stage_id": "test", "launch_token": str(uuid4())}
        labels = worker._labels(owner)
        record = {"daemon_id": "fixture-daemon", "container_labels": labels}
        active = True

        def command(argv, _deadline):
            nonlocal active
            if argv[1] == "info":
                return b"fixture-daemon"
            if argv[1] == "ps":
                return container_id.encode() if active else b""
            if argv[1] == "inspect":
                return json.dumps(
                    [
                        {
                            "Id": container_id,
                            "Config": {"Labels": labels},
                            "State": {"Running": True, "Pid": os.getpid()},
                        }
                    ]
                ).encode()
            self.assertEqual(argv, ["docker", "rm", "-f", container_id])
            active = False
            return b""

        with patch.object(worker, "_command", side_effect=command):
            rows = worker._containers(record, time.monotonic() + 2)
            self.assertEqual(rows[0]["process"]["pid"], os.getpid())
            self.assertEqual(worker._stop_containers(record, time.monotonic() + 2), [])
        with patch.object(worker, "_command", return_value=b"different-daemon") as observed:
            with self.assertRaisesRegex(worker.WorkerError, "daemon identity changed"):
                worker._stop_containers(record, time.monotonic() + 2)
            self.assertEqual(observed.call_count, 1)

    def test_checkpoint_matches_existing_manifest_and_follows_regular_hf_links(self):
        from tools.deployment.checkpoint_manifest import inspect_checkpoint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkpoint"
            root.mkdir()
            (root / "config.json").write_text("{}")
            blob = Path(temporary) / "blob"
            blob.write_bytes(b"model bytes")
            (root / "weights.safetensors").symlink_to(blob)
            observed = worker.probe("checkpoint", {"model_dir": str(root)}, time.monotonic() + 1)
            self.assertEqual(observed, inspect_checkpoint(root))
            os.mkfifo(root / "not-a-file")
            with self.assertRaises(worker.WorkerError):
                worker.probe("checkpoint", {"model_dir": str(root)}, time.monotonic() + 1)

    def test_image_probe_uses_fixed_inspection_and_returns_only_identity(self):
        image = "sha256:" + "a" * 64
        with patch.object(
            worker,
            "_command",
            return_value=json.dumps(
                [{"Id": image, "RepoDigests": [image], "Config": {"Env": ["SECRET=private"]}}]
            ).encode(),
        ) as command:
            self.assertEqual(
                worker.probe("image", {"image": image}, time.monotonic() + 1),
                {"id": image, "repo_digests": [image]},
            )
            self.assertEqual(command.call_args.args[0], ["docker", "image", "inspect", image])
        with self.assertRaises(worker.WorkerError):
            worker.probe("image", {"image": "latest"}, time.monotonic() + 1)
