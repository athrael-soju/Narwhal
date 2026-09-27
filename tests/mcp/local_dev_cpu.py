"""CPU substitutions for the installed lifecycle smoke, never imported by production."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def install() -> None:
    """Substitute only GPU/runtime work and propagate this temporary test bootstrap."""
    from narwhal import provenance
    from narwhal.deployment import native_engine
    from narwhal.dev import lifecycle, management_prepare, template

    root = Path(__file__).parent
    original_popen = subprocess.Popen

    class Popen(original_popen):
        def __init__(self, *args, **kwargs):
            environment = dict(kwargs.get("env") or os.environ)
            environment["PYTHONPATH"] = str(root)
            if os.environ.get("NARWHAL_CPU_FAKE_PROVENANCE") == "1":
                environment["NARWHAL_CPU_FAKE_PROVENANCE"] = "1"
            kwargs["env"] = environment
            super().__init__(*args, **kwargs)

    subprocess.Popen = Popen
    if os.environ.get("NARWHAL_CPU_FAKE_PROVENANCE") == "1":
        provenance.verified_source = lambda: {
            "commit": "1" * 40,
            "distribution_version": provenance._version(),
            "wheel_sha256": "2" * 64,
            "bundle_sha256": None,
        }
    memory = {"total_mib": 32768, "used_mib": 0}
    template._gpu_rows = lambda: [{"name": "CPU lifecycle fixture", "uuid": "GPU-cpu-fixture"}]
    template.gpu_memory = lambda gpu: memory
    template._check_runtime = lambda packages: None
    template.check_plugin = lambda runtime: None
    template._address = lambda interface: "127.0.0.1"
    lifecycle.check_plugin = lambda runtime: None
    lifecycle.gpu_memory = lambda gpu: memory
    native_engine.gpu_memory = lambda gpu: memory

    def observe(context, spec, config, env):
        context.assert_current()
        return {
            "gpu": {"name": "CPU lifecycle fixture", "uuid": "GPU-cpu-fixture", "total_mib": 32768},
            "interface": config["fabric_interface"],
            "address": "127.0.0.1",
            "runtime_packages": spec["runtime"]["expected_packages"],
        }, {"gpu_used_mib": 0, "gpu_free_mib": 32768}

    management_prepare._observe = observe

    def launch(instance, run, config, spec, state):
        fleet = lifecycle.read(instance / "fleet.json")
        fleet["profiles"]["path"] = str(run / "profiles.json")
        lifecycle.write(run / "fleet.json", fleet)
        lifecycle.write(run / "profiles.json", {"cpu_fixture": True})
        owner = state.get("management_owner")
        for index in range(config["engine_count"]):
            name = f"engine-{index + 1}"
            selected = run / name
            selected.mkdir(mode=0o700)
            if owner:
                lifecycle.write(selected / "management-owner.json", owner)
            port = config["ports"]["engine_first"] + index
            child = subprocess.Popen(
                [sys.executable, "-m", "local_dev_cpu", str(port), str(instance)],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key
                    not in {
                        "NARWHAL_MANAGEMENT_CONTEXT_FD",
                        "NARWHAL_MANAGEMENT_REGISTRY",
                    }
                },
            )
            identity = native_engine.process_identity(child.pid)
            native_engine.write_process_identity(selected, identity)
            lifecycle._wait(f"http://127.0.0.1:{port}/health", identity, seconds=5)
            if index == 0:
                (instance.parent / "up-started").write_text(str(run))
                deadline = time.monotonic() + 20
                while (instance.parent / "hold-up").exists():
                    if time.monotonic() >= deadline:
                        raise ValueError("CPU fixture launch gate expired")
                    time.sleep(0.05)
            port = config["ports"]["attestation_first"] + index
            receipt = lifecycle._spawn(
                instance,
                state,
                "local_dev_cpu",
                [str(port), str(instance)],
                f"sidecar-{index + 1}",
                dict(os.environ),
            )
            lifecycle._wait(f"http://127.0.0.1:{port}/health", receipt["identity"], seconds=5)
        receipt = lifecycle._spawn(
            instance,
            state,
            "local_dev_cpu",
            [str(config["ports"]["router"]), str(instance)],
            "router",
            dict(os.environ),
        )
        lifecycle._wait(config["router_url"] + "/health", receipt["identity"], seconds=5)

    lifecycle._launch = launch
    original_run = lifecycle._run

    def run(root, module, args, log):
        if log != "preflight":
            return original_run(root, module, args, log)
        selected = Path(args[args.index("--evidence-out") + 1])
        lifecycle.write(selected, {"cpu_fixture": True, "scope": "no GPU or KV qualification"})

    lifecycle._run = run

    async def verified(*args, **kwargs):
        return []

    lifecycle.verify_directed_kv_evidence = verified


def router_state(instance: Path) -> dict:
    """Return a schema-valid CPU router picture with one optional active request."""
    from narwhal.serving.schemas import StateOut

    busy = int((instance.parent / "busy-state").exists())
    engines = [row["iid"] for row in json.loads((instance / "fleet.json").read_text())["engines"]]
    return StateOut.model_validate(
        {
            "schema": "narwhal.state",
            "schema_version": 1,
            "served": 0,
            "failed": 0,
            "invalid_requests": 0,
            "controller": "reactive",
            "token_accounting": "cpu-fixture",
            "ha": {"ready": True, "standby": False, "epoch": 0, "holder": "", "blocked": ""},
            "lifecycle": {
                "router": {"controls_fleet": True, "ready": True},
                "wave": {"id": "", "active": False, "ready_to_stop": False},
                "engines": {},
                "events": [],
            },
            "admission": {
                "inflight": busy,
                "queued": 0,
                "waiting_prefill": 0,
                "waiting_decode": 0,
                "limit": 1,
                "rejected": 0,
                "refused": 0,
            },
            "serving": {"http_retained": busy},
            "http_pools": {
                "data_connections": 1,
                "control_connections": 1,
                "pool_timeout_s": 1.0,
            },
            "pools": {"prefill": engines[:-1], "decode": engines[-1:]},
            "load": {"prefill": 0.0, "decode": 0.0},
            "thresholds": {
                "expand": 1.0,
                "shrink": 0.5,
                "cooldown_s": 1.0,
                "sustained_intervals": 1,
                "dwell_s": 1.0,
                "panic_ratio": 2.0,
            },
            "slo": {"ttft_s": 1.0, "tpot_s": 1.0},
            "first_token_timeout_s": 1.0,
            "resident": {
                name: {"prefill": 0, "decode": busy if name == engines[-1] else 0}
                for name in engines
            },
            "below_floor": {"active": False, "live_prefill": 1},
            "ejected": [],
            "unserved": 0,
            "panic_bypasses": 0,
            "flips_refused": [],
            "flips": [],
            "decode_floor": {"min_decode": 1, "live_decode": 1},
        }
    ).model_dump(by_alias=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/narwhal/state":
            payload = json.dumps(router_state(Path(sys.argv[2]))).encode()
        else:
            payload = b"cpu_fixture 1\n" if self.path == "/metrics" else b'{"status":"ready"}'
        self.send_response(200)
        self.send_header(
            "Content-Type", "text/plain" if self.path == "/metrics" else "application/json"
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        instance = Path(sys.argv[2])
        answer = "6" if (instance.parent / "fail-verify").exists() else "5"
        payload = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
