"""A router process over simulated engines and their attestation sidecars refuses
unauthenticated membership and lifecycle requests, and sends no engine key to a host that a
join names before that host's attestation verifies."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from narwhal.profiling.store import ProfileStore
from tests import CONTROL_HEADERS, CONTROL_TOKEN
from tests.fixtures import ROOT, profile

SIMULATED_ENGINE = ROOT / "tools" / "measurement" / "simulated_engine.py"
ENGINE_KEY = "engine-key-" + "7f" * 24
CONTRACT = {
    "engine_version": "simulated",
    "image_digest": "sha256:" + "0" * 64,
    "transfer_version": "test",
    "connector_version": 1,
    "model_architecture": "TestForCausalLM",
    "model_dtype": "float32",
    "kv_heads": 1,
    "head_size": 1,
    "hidden_layers": 1,
    "attention_backend": "TEST",
    "kv_cache_dtype": "auto",
    "cross_layers_blocks": False,
    "hybrid_kv_cache_manager": False,
    "connector": "NixlConnector",
    "kv_role": "kv_both",
    "transfer_mode": "pull",
    "speculative_config": "disabled",
    "enforce_handshake_compat": True,
}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url, timeout_s=30.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"{url} did not answer within {timeout_s}s")


class Attacker(ThreadingHTTPServer):
    """Answers like a vLLM engine's identity routes and records each request's headers."""

    def __init__(self):
        self.log = []
        attacker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                attacker.log.append((self.command, self.path, self.headers.get("authorization")))
                if self.path == "/version":
                    self.reply(200, json.dumps({"version": "simulated"}))
                elif self.path == "/metrics":
                    self.reply(200, "process_start_time_seconds 1.0\n")
                else:
                    self.reply(404, "{}")

            do_POST = do_DELETE = do_GET

            def reply(self, status, body):
                data = body.encode()
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"


class RouterProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.root = root = Path(folder.name)
        cls.processes = []
        cls.addClassCleanup(cls.stop_processes)
        engines = []
        for iid, role in (("e0", "prefill"), ("e1", "decode"), ("e2", "decode")):
            engine = cls.start(
                [sys.executable, str(SIMULATED_ENGINE), "--iid", iid], iid, ready_line=True
            )
            ready = json.loads(engine.stdout.readline())
            document = root / f"attestation-{iid}.json"
            document.write_text(
                json.dumps(
                    {
                        "schema": "narwhal.attestation",
                        "schema_version": 1,
                        "contract": CONTRACT,
                        "sources": {
                            name: "test"
                            for name, value in CONTRACT.items()
                            if isinstance(value, bool) or value not in (None, "", 0)
                        },
                    }
                )
            )
            port = free_port()
            cls.start(
                [
                    *(sys.executable, "-m", "narwhal.engines.attestation"),
                    *("--document", str(document), "--engine-base", ready["url"]),
                    *("--backend", "vllm", "--port", str(port)),
                ],
                f"sidecar-{iid}",
            )
            attestation = f"http://127.0.0.1:{port}/v1/attestation"
            wait_for(attestation)
            payload = httpx.get(attestation).json()
            engines.append(
                {"iid": iid, "url": ready["url"], "attestation_url": attestation, "role": role}
            )
            store = ProfileStore(root / "profiles.json")
            store.put(replace(profile(iid), generation_digest=payload["attestation_digest"]))
        fleet = json.loads((ROOT / "tests/data/fleet.json").read_text())
        fleet.update(
            model="simulated",
            engines=engines,
            engine_contract=CONTRACT,
            engine={"tokenize": False, "engine_api_key_env": "NARWHAL_TEST_ENGINE_KEY"},
            profiles={"path": str(root / "profiles.json")},
            recovery={"state_path": str(root / "state.json")},
        )
        (root / "fleet.json").write_text(json.dumps(fleet))
        port = free_port()
        cls.start(
            [
                *(sys.executable, "-m", "narwhal.cli"),
                *("--fleet", str(root / "fleet.json"), "--port", str(port)),
            ],
            "router",
            {"NARWHAL_TEST_ENGINE_KEY": ENGINE_KEY, "NARWHAL_ROUTER_TOKEN": CONTROL_TOKEN},
        )
        cls.router = f"http://127.0.0.1:{port}"
        wait_for(f"{cls.router}/health")
        cls.attacker = Attacker()
        threading.Thread(target=cls.attacker.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.attacker.shutdown)

    @classmethod
    def start(cls, argv, name, env=None, *, ready_line=False):
        log = open(cls.root / f"{name}.log", "w")  # noqa: SIM115 - closed with the process
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE if ready_line else log,
            stderr=log,
            text=True,
            env={**os.environ, **(env or {})},
            start_new_session=True,
        )
        cls.processes.append((process, log))
        return process

    @classmethod
    def stop_processes(cls):
        for process, _ in cls.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process, log in cls.processes:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            log.close()

    def setUp(self):
        self.attacker.log.clear()

    def call(self, method, path, headers=None, **kwargs):
        return httpx.request(method, self.router + path, headers=headers, timeout=30, **kwargs)

    def join_body(self):
        return {
            "iid": "intruder",
            "url": self.attacker.url,
            "role": "decode",
            "attestation_url": f"{self.attacker.url}/v1/attestation",
        }

    def test_unauthenticated_control_requests_change_nothing(self):
        requests = (
            ("POST", "/narwhal/engines", {"json": self.join_body()}),
            ("DELETE", "/narwhal/engines/e2", {"params": {"deadline_s": 5}}),
            ("POST", "/narwhal/lifecycle/drain", {"json": {"engines": ["e0"]}}),
            ("POST", "/narwhal/lifecycle/readmit", {"json": {"engines": ["e0"]}}),
        )
        for method, path, kwargs in requests:
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, **kwargs).status_code, 401)
        self.assertEqual(self.attacker.log, [])
        engines = self.call("GET", "/narwhal/engines").json()["engines"]
        self.assertEqual([row["iid"] for row in engines], ["e0", "e1", "e2"])
        lifecycle = self.call("GET", "/narwhal/lifecycle").json()["engines"]
        self.assertEqual(lifecycle["e0"]["state"], "active")

    def test_an_authenticated_join_of_an_attacker_sends_it_no_key(self):
        response = self.call(
            "POST", "/narwhal/engines", headers=CONTROL_HEADERS, json=self.join_body()
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(self.attacker.log)
        self.assertEqual([entry for entry in self.attacker.log if entry[2]], [])

    def test_the_token_drains_an_engine(self):
        # e1 only: the other tests expect e0 in placement.
        response = self.call(
            "POST", "/narwhal/lifecycle/drain", headers=CONTROL_HEADERS, json={"engines": ["e1"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["engines"]["e1"]["state"], "drained")


if __name__ == "__main__":
    unittest.main()
