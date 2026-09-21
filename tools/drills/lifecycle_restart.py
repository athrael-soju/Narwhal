#!/usr/bin/env python3
"""Restart local engine stubs through Narwhal's lifecycle contract."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore


def _port_block(size: int) -> int:
    for base in range(19001, 19900 - size):
        sockets = []
        try:
            for port in range(base, base + size):
                sock = socket.socket()
                sock.bind(("127.0.0.1", port))
                sockets.append(sock)
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
        if len(sockets) == size:
            return base
    raise RuntimeError("no free local port block")


def _wait(url: str, path: str, status: int, timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}{path}", timeout=0.5)
            last = f"HTTP {response.status_code}: {response.text[:120]}"
            if response.status_code == status:
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.05)
    raise RuntimeError(f"{url}{path} did not reach HTTP {status}: {last}")


def _start(command: list[str], log: Path) -> subprocess.Popen[bytes]:
    output = log.open("ab")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output.close()
    return process


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5.0)


def _stub_command(iid: str, port: int) -> list[str]:
    return [
        sys.executable,
        "tools/stub_fleet.py",
        "--single-iid",
        iid,
        "--base-port",
        str(port),
        "--model",
        "stub",
    ]


def _write_inputs(out: Path, base: int, policy: str) -> Path:
    fleet = json.loads(Path("config/fleet.stub.json").read_text())
    fleet["engines"] = [
        {
            "iid": f"e{k}",
            "url": f"http://127.0.0.1:{base + k}",
            "attestation_url": f"http://127.0.0.1:{base + k}/v1/attestation",
            "role": "prefill" if k == 0 else "decode",
        }
        for k in range(3)
    ]
    fleet.setdefault("profiles", {})["path"] = str(out / "profiles.json")
    recovery = fleet.setdefault("recovery", {})
    recovery["state_path"] = str(out / "router.state.json")
    recovery["engine_restart_policy"] = policy
    fleet.setdefault("controller", {})["monitor_interval_s"] = (
        0.1 if policy == "whole_wave" else 60.0
    )
    # Keep enough monitoring samples inside the drift window.
    recovery["health"] = {"window_s": 180.0}
    fleet.setdefault("engine", {})["tokenize"] = False
    config = out / "fleet.json"
    config.write_text(json.dumps(fleet, indent=2) + "\n")
    # Synthetic fits match the local CPU stubs.
    store = ProfileStore(out / "profiles.json")
    for k in range(3):
        store.put(
            Profile(
                f"e{k}",
                2e-8,
                6e-5,
                0.005,
                3e-6,
                0.012,
                decode_min_requests=1,
                decode_max_requests=4096,
                decode_min_kv_tokens=1,
                decode_max_kv_tokens=1_000_000_000,
                decode_fit_mape=0.0,
                decode_cv_mape=0.0,
            )
        )
    return config


def _post(url: str, path: str, body: dict[str, Any], status: int = 200) -> dict[str, Any]:
    response = httpx.post(f"{url}{path}", json=body, timeout=30.0)
    if response.status_code != status:
        raise RuntimeError(f"{path} returned HTTP {response.status_code}: {response.text[:500]}")
    return response.json()


def _last_request(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    requests = [row for row in rows if row.get("rid") and "prefill_iid" in row]
    if not requests:
        raise RuntimeError("router journal contains no completed request")
    return requests[-1]


def main(argv: list[str] | None = None) -> int:
    """Run the process-level lifecycle drill."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=f"runs/local/lifecycle-restart-{int(time.time())}")
    parser.add_argument(
        "--restart-policy", choices=("individual", "whole_wave"), default="individual"
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = _port_block(4)
    engine_ports = [base + k for k in range(3)]
    router_port = base + 3
    router_url = f"http://127.0.0.1:{router_port}"
    config = _write_inputs(out, base, args.restart_policy)
    journal = out / "router.journal.jsonl"

    engines: list[subprocess.Popen[bytes] | None] = [None, None, None]
    router: subprocess.Popen[bytes] | None = None
    try:
        for k, port in enumerate(engine_ports):
            engines[k] = _start(_stub_command(f"e{k}", port), out / f"e{k}.log")
            _wait(f"http://127.0.0.1:{port}", "/health", 200)
        router = _start(
            [
                sys.executable,
                "-m",
                "narwhal.cli",
                "--fleet",
                str(config),
                "--host",
                "127.0.0.1",
                "--port",
                str(router_port),
                "--journal",
                str(journal),
            ],
            out / "router.log",
        )
        _wait(router_url, "/ready", 200)
        body = {"model": "stub", "prompt": "baseline", "max_tokens": 4}
        _post(router_url, "/v1/completions", body)

        if args.restart_policy == "individual":
            single = _post(
                router_url,
                "/narwhal/lifecycle/drain",
                {"engines": ["e0"], "deadline_s": 30},
            )
            if not single["engines"]["e0"]["ready_to_stop"]:
                raise RuntimeError("single engine was not ready before supervisor stop")
            _post(router_url, "/v1/completions", {**body, "prompt": "after drain"})
            routed = _last_request(journal)
            if "e0" in (routed.get("prefill_iid"), routed.get("decode_iid")):
                raise RuntimeError("draining e0 received a new placement")

            _stop(engines[0])
            engines[0] = _start(_stub_command("e0", engine_ports[0]), out / "e0.log")
            _wait(f"http://127.0.0.1:{engine_ports[0]}", "/health", 200)
            single_back = _post(
                router_url,
                "/narwhal/lifecycle/readmit",
                {"engines": ["e0"]},
            )
            if single_back["engines"]["e0"]["state"] != "active":
                raise RuntimeError("single engine did not readmit")
        else:
            _post(router_url, "/narwhal/lifecycle/drain", {"engines": ["e0"]}, status=409)
            _stop(engines[0])
            engines[0] = _start(_stub_command("e0", engine_ports[0]), out / "e0.log")
            _wait(f"http://127.0.0.1:{engine_ports[0]}", "/health", 200)
            _wait(router_url, "/ready", 503)
            _post(router_url, "/narwhal/lifecycle/readmit", {"engines": ["e0"]}, status=409)
            held = httpx.get(f"{router_url}/narwhal/lifecycle").json()
            if not held["wave"]["active"] or any(
                engine["accepts_new"] for engine in held["engines"].values()
            ):
                raise RuntimeError("unplanned restart did not hold the full fleet")

        state_before_wave = httpx.get(f"{router_url}/narwhal/state").json()

        wave = _post(
            router_url,
            "/narwhal/lifecycle/drain",
            {"wave": True, "deadline_s": 30},
        )
        if not wave["wave"]["ready_to_stop"]:
            raise RuntimeError("whole wave was not ready before supervisor stop")
        _wait(router_url, "/ready", 503)
        for k in range(3):
            _stop(engines[k])
            engines[k] = None
        for k, port in enumerate(engine_ports):
            engines[k] = _start(_stub_command(f"e{k}", port), out / f"e{k}.log")
            _wait(f"http://127.0.0.1:{port}", "/health", 200)
        wave_back = _post(router_url, "/narwhal/lifecycle/readmit", {"wave": True})
        _wait(router_url, "/ready", 200)
        _post(router_url, "/v1/completions", {**body, "prompt": "after wave"})
        final_state = httpx.get(f"{router_url}/narwhal/state").json()
        if final_state["served"] <= state_before_wave["served"]:
            raise RuntimeError("router counters did not continue across engine restarts")
        if final_state["pools"] != state_before_wave["pools"]:
            raise RuntimeError("router roles changed across the whole-wave restart")

        summary = {
            "engine_restart_policy": args.restart_policy,
            "single_state": (
                single_back["engines"]["e0"]["state"]
                if args.restart_policy == "individual"
                else "rejected_by_policy"
            ),
            "unplanned_restart_held_fleet": args.restart_policy == "whole_wave",
            "wave_ready_withdrawn_before_stop": True,
            "wave_active_after_readmit": wave_back["wave"]["active"],
            "all_engines_active": all(
                engine["state"] == "active" for engine in wave_back["engines"].values()
            ),
            "served_before_wave": state_before_wave["served"],
            "served_after_wave": final_state["served"],
            "roles_preserved": final_state["pools"] == state_before_wave["pools"],
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        _stop(router)
        for process in engines:
            _stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
