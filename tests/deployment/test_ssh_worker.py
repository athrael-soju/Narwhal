"""Exercise real CPU job ownership without connecting to fleet hosts."""

import base64
import contextlib
import json
import os
import selectors
import signal
import subprocess
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

    def test_durable_submit_keeps_running_after_input_channel_closes(self):
        job = self.job("import time; time.sleep(30)")
        process = subprocess.run(
            [sys.executable, "-m", "narwhal.deployment.ssh_worker"],
            input=json.dumps({"command": "submit", "root": str(self.root), "job": job}).encode(),
            capture_output=True,
            timeout=5,
            check=True,
        )
        self.assertTrue(json.loads(process.stdout)["ok"])
        self.assertEqual(process.stderr, b"")
        record = self.wait(job["job_id"], {"running"})
        self.assertTrue(worker._alive(record["child"]))
        worker.cancel(self.root, job["job_id"])
        self.assertEqual(self.wait(job["job_id"])["state"], "cancelled")

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


class ProbeChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.marker = self.root / "ready"

    def start(self, setup, *, parameters=None):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from narwhal.deployment import ssh_worker as worker\n"
                "import contextlib,json,os,signal,sys,threading\nfrom pathlib import Path\n"
                + setup
                + "\nraise SystemExit(worker.main())\n",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

        self.addCleanup(cleanup)
        process.stdin.write(
            json.dumps(
                {
                    "command": "probe",
                    "kind": "checkpoint",
                    "parameters": parameters or {},
                    "timeout_ms": 30_000,
                }
            ).encode()
            + b"\n"
        )
        process.stdin.flush()
        return process

    def await_marker(self):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.marker.exists():
                return
            time.sleep(0.01)
        self.fail("Probe did not enter its observed work")

    def close_channel(self, process):
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, b"")
        reply = json.loads(stdout)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "stage_cancelled")

    def test_probe_finishes_with_input_held_open_and_restores_signal_handler(self):
        setup = """
before=signal.getsignal(signal.SIGUSR1)
def observe(kind,parameters,deadline):
 return {'held_open':True}
worker.probe=observe
original=worker.main
def checked():
 result=original()
 assert signal.getsignal(signal.SIGUSR1)==before
 assert len(threading.enumerate())==1
 return result
worker.main=checked
"""
        process = self.start(setup)
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selected.select(timeout=5))
        self.assertEqual(
            json.loads(process.stdout.readline()), {"ok": True, "data": {"held_open": True}}
        )
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr.read(), b"")

    def test_channel_eof_stops_checkpoint_threads_and_closes_their_files(self):
        model = self.root / "model"
        model.mkdir()
        for name in ("config.json", "a.safetensors", "b.safetensors", "c.safetensors"):
            (model / name).write_bytes(b"{}")
        setup = (
            f"marker=Path({str(self.marker)!r})\n"
            + """
release=threading.Event()
entered=threading.Barrier(4, action=lambda: marker.write_text('ready'), timeout=5)
streams=[]
original_open=os.fdopen
original_signal=signal.pthread_kill
def interrupt(thread,number):
 try: original_signal(thread,number)
 finally: release.set()
signal.pthread_kill=interrupt
@contextlib.contextmanager
def opened(fd,mode):
 with original_open(fd,mode) as stream:
  streams.append(stream)
  class Reader:
   first=True
   def fileno(self): return stream.fileno()
   def read(self,size):
    if self.first:
     self.first=False
     entered.wait()
     assert release.wait(5)
    return stream.read(size)
  yield Reader()
os.fdopen=opened
original_main=worker.main
def checked():
 result=original_main()
 assert len(streams)==4 and all(stream.closed for stream in streams)
 assert len(threading.enumerate())==1
 return result
worker.main=checked
"""
        )
        process = self.start(setup, parameters={"model_dir": str(model)})
        self.await_marker()
        self.close_channel(process)

    def test_channel_eof_reaps_a_probe_subprocess_group(self):
        helper = (
            "import os,time; from pathlib import Path; "
            f"Path({str(self.marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )
        setup = f"""
def observe(kind,parameters,deadline):
 return {{'body':worker._command([sys.executable,'-c',{helper!r}],deadline).decode()}}
worker.probe=observe
"""
        process = self.start(setup)
        self.await_marker()
        pid = int(self.marker.read_text())
        identity = worker._identity(pid)

        def cleanup_helper():
            if worker._alive(identity):
                os.killpg(pid, signal.SIGKILL)

        self.addCleanup(cleanup_helper)
        self.close_channel(process)
        self.assertIsNone(worker._proc(pid))


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

    def test_checkpoint_hashes_four_files_concurrently_in_sorted_manifest_order(self):
        from tools.deployment.checkpoint_manifest import inspect_checkpoint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text("{}")
            for number in range(7):
                (root / f"weights-{number}.safetensors").write_bytes(bytes([number]) * 31)
            expected = inspect_checkpoint(root)
            barrier = threading.Barrier(4, timeout=2)
            lock = threading.Lock()
            active = peak = 0
            original = os.fdopen

            @contextlib.contextmanager
            def opened(fd, mode):
                nonlocal active, peak
                with original(fd, mode) as stream:
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    first = True

                    class Reader:
                        def fileno(self):
                            return stream.fileno()

                        def read(self, size):
                            nonlocal first
                            self_test.assertEqual(size, 8 * 1024 * 1024)
                            if first:
                                first = False
                                barrier.wait()
                            return stream.read(size)

                    try:
                        yield Reader()
                    finally:
                        with lock:
                            active -= 1

            self_test = self
            with patch.object(worker.os, "fdopen", side_effect=opened):
                observed = worker._checkpoint(root, time.monotonic() + 5)
            self.assertEqual(observed, expected)
            self.assertEqual(peak, 4)
            self.assertEqual(active, 0)

    def test_checkpoint_deadline_closes_parallel_file_descriptors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text("{}")
            for number in range(3):
                (root / f"weights-{number}.safetensors").write_bytes(b"weights")
            opened_streams = []
            original = os.fdopen
            expired = threading.Event()
            barrier = threading.Barrier(4, action=expired.set, timeout=5)

            @contextlib.contextmanager
            def opened(fd, mode):
                with original(fd, mode) as stream:
                    opened_streams.append(stream)

                    class Reader:
                        def fileno(self):
                            return stream.fileno()

                        def read(self, size):
                            barrier.wait()
                            return stream.read(size)

                    yield Reader()

            with (
                patch.object(worker.os, "fdopen", side_effect=opened),
                patch.object(
                    worker.time, "monotonic", side_effect=lambda: 600 if expired.is_set() else 0
                ),
                self.assertRaises(worker.WorkerError) as raised,
            ):
                worker._checkpoint(root, 300)
            self.assertEqual(raised.exception.code, "stage_timeout")
            self.assertTrue(expired.is_set())
            self.assertEqual(len(opened_streams), 4)
            self.assertTrue(all(stream.closed for stream in opened_streams))

    def test_checkpoint_rejects_file_replacement_during_parallel_hashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text("{}")
            for number in range(3):
                (root / f"weights-{number}.safetensors").write_bytes(b"weights")
            original = os.fdopen

            @contextlib.contextmanager
            def opened(fd, mode):
                with original(fd, mode) as stream:
                    selected = Path(os.readlink(f"/proc/self/fd/{fd}")) == config
                    first = True

                    class Reader:
                        def fileno(self):
                            return stream.fileno()

                        def read(self, size):
                            nonlocal first
                            data = stream.read(size)
                            if selected and first:
                                first = False
                                replacement = root / "replacement"
                                replacement.write_text("{}")
                                os.replace(replacement, config)
                            return data

                    yield Reader()

            with (
                patch.object(worker.os, "fdopen", side_effect=opened),
                self.assertRaises(worker.WorkerError) as raised,
            ):
                worker._checkpoint(root, time.monotonic() + 1)
            self.assertEqual(raised.exception.code, "stale_plan")

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
