#!/usr/bin/env python3
"""Kill a local primary mid-stream and verify fenced standby takeover."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from narwhal.contracts import FLEET, versioned


def _port_block(size: int) -> int:
    for base in range(18101, 19000 - size):
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


def _wait(url: str, path: str, status: int, timeout_s: float = 10.0) -> dict[str, Any]:
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


def _config(path: Path, engine_port: int, state: Path) -> None:
    engines = [
        {
            "iid": f"e{k}",
            "url": f"http://127.0.0.1:{engine_port + k}",
            "role": "prefill" if k < 2 else "decode",
        }
        for k in range(4)
    ]
    path.write_text(
        json.dumps(
            {
                **versioned(FLEET, {}),
                "model": "stub",
                "engines": engines,
                "slo": {"ttft_s": 10.0, "tpot_s": 0.5},
                "profiles": {"path": str(path.parent / "profiles.json")},
                "controller": {"monitor_interval_s": 60.0},
                # Three samples of guaranteed supply inside the drift window
                # at this drill's monitor cadence.
                "recovery": {"state_path": str(state), "health": {"window_s": 180.0}},
                "engine": {"tokenize": False},
            },
            indent=2,
        )
        + "\n"
    )


def _profiles(path: Path) -> None:
    # Synthetic fits match the local CPU stubs.
    from narwhal.profiling.model import Profile
    from narwhal.profiling.store import ProfileStore

    store = ProfileStore(path)
    for k in range(4):
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


def _wait_shadow(path: Path, served: int, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        else:
            if (document.get("counters") or {}).get("served", 0) >= served:
                return
        time.sleep(0.05)
    raise RuntimeError("standby did not persist the primary counters")


def _start(command: list[str], log: Path) -> subprocess.Popen[bytes]:
    output = log.open("wb")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output.close()
    return process


def _stop(process: subprocess.Popen[bytes] | None, *, kill: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5.0)


def _serve_command(
    fleet: Path,
    port: int,
    router_id: str,
    lease: Path,
    *,
    standby_of: str = "",
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "narwhal.cli",
        "--fleet",
        str(fleet),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--router-id",
        router_id,
        "--lease-path",
        str(lease),
        "--lease-ttl",
        "0.8",
        "--lease-renew-interval",
        "0.2",
        "--lease-safety-margin",
        "0.1",
    ]
    if standby_of:
        command += [
            "--standby-of",
            standby_of,
            "--standby-probe-interval",
            "0.05",
            "--standby-takeover-after",
            "2",
            "--standby-max-handoff-age",
            "5",
        ]
    return command


def main(argv: list[str] | None = None) -> int:
    """Run the local process-level failover drill."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=f"runs/local/ha-failover-{int(time.time())}")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = _port_block(6)
    engine_port, primary_port, standby_port = base, base + 4, base + 5
    primary_url = f"http://127.0.0.1:{primary_port}"
    standby_url = f"http://127.0.0.1:{standby_port}"
    lease = out / "router.lease"
    _profiles(out / "profiles.json")
    primary_config = out / "primary.json"
    standby_config = out / "standby.json"
    _config(primary_config, engine_port, out / "primary.state.json")
    _config(standby_config, engine_port, out / "standby.state.json")

    stub = primary = standby = recovered = None
    try:
        stub = _start(
            [
                sys.executable,
                "tools/drills/stub_fleet.py",
                "--base-port",
                str(engine_port),
                "--instances",
                "4",
                "--model",
                "stub",
            ],
            out / "stub.log",
        )
        for port in range(engine_port, engine_port + 4):
            _wait(f"http://127.0.0.1:{port}", "/health", 200)
        primary = _start(
            _serve_command(primary_config, primary_port, "router-a", lease),
            out / "primary.log",
        )
        _wait(primary_url, "/ready", 200)
        standby = _start(
            _serve_command(
                standby_config,
                standby_port,
                "router-b",
                lease,
                standby_of=primary_url,
            ),
            out / "standby.log",
        )
        _wait(standby_url, "/ready", 503)

        body = {"model": "stub", "prompt": "baseline", "max_tokens": 4}
        if httpx.post(f"{primary_url}/v1/completions", json=body, timeout=10.0).status_code != 200:
            raise RuntimeError("primary baseline request failed")
        before = httpx.get(f"{primary_url}/narwhal/state").json()
        roles = before["pools"]
        _wait_shadow(out / "standby.state.json", before["served"])

        with ExitStack() as stack:
            client = stack.enter_context(httpx.Client(timeout=10.0))
            stream = stack.enter_context(
                client.stream(
                    "POST",
                    f"{primary_url}/v1/completions",
                    json={"model": "stub", "prompt": "stream", "max_tokens": 256, "stream": True},
                )
            )
            lines = stream.iter_lines()
            first = next(line for line in lines if line.startswith("data:"))
            if "[DONE]" in first:
                raise RuntimeError("stream ended before primary kill")
            _stop(primary, kill=True)
            primary = None
            _wait(standby_url, "/ready", 200)

        after = httpx.get(f"{standby_url}/narwhal/state").json()
        if after["pools"] != roles or after["served"] < before["served"]:
            raise RuntimeError("standby did not preserve roles and counters")
        if httpx.post(f"{standby_url}/v1/completions", json=body, timeout=10.0).status_code != 200:
            raise RuntimeError("standby did not admit new traffic")

        recovered = _start(
            _serve_command(primary_config, primary_port, "router-a", lease),
            out / "recovered.log",
        )
        _wait(primary_url, "/health", 200)
        _wait(primary_url, "/ready", 503)
        old = httpx.post(f"{primary_url}/v1/completions", json=body, timeout=5.0)
        if old.status_code != 503:
            raise RuntimeError(f"recovered primary admitted traffic: HTTP {old.status_code}")

        summary = {
            "primary_epoch": before["ha"]["epoch"],
            "standby_epoch": after["ha"]["epoch"],
            "served_before": before["served"],
            "served_after": after["served"],
            "roles_preserved": after["pools"] == roles,
            "recovered_primary_status": old.status_code,
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        for process in (recovered, standby, primary, stub):
            _stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
