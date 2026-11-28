"""Count client errors through the shipped HAProxy configuration across router takeovers.

The check starts two simulated engines, a router pair on one lease, and HAProxy in Docker
with `deploy/ha/haproxy.cfg`. It offers open-loop Poisson load through HAProxy, stops the
active router with a signal, and restarts it on the same port as a standby of the other
router, as soon as it exits or `--restart-after` seconds after the new holder is ready. With
`--both`, it also stops the standby with SIGTERM and restarts both routers at once, each a
standby of the other. It prints one JSON report and exits 1 when a client request got a status
other than 200 or 429, a stream that started after the signal did not complete, or both routers
reported ready at once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from narwhal.deployment import load_trial as trial
from tools.measurement.router_benchmark.client import client_bodies
from tools.measurement.router_benchmark.fleet import Shape, write_fleet
from tools.measurement.router_benchmark.processes import (
    child_env,
    free_port,
    ready_line,
    router_env,
)

SHIPPED_CONFIG = ROOT / "deploy" / "ha" / "haproxy.cfg"
SIMULATED_ENGINE = ROOT / "tools" / "measurement" / "simulated_engine.py"
DEFAULT_IMAGE = "haproxy:3.2-alpine"
SHAPE = Shape(
    engines=2,
    prefill_engines=1,
    input_tokens=64,
    output_tokens=32,
    token_interval_s=0.01,
    frames_per_write=1,
    prefill_s=0.005,
    ttft_slo_s=10.0,
    tpot_slo_s=1.0,
)
# Statuses a takeover may return: 200, and 429 from admission of the held requests.
ACCEPTED = {200, 429}
POLL_S = 0.02


def render_config(text: str, port: int, routers: dict[str, int]) -> str:
    """Return the shipped configuration bound to local ports, with request logging."""
    for name, router_port in routers.items():
        placeholder = f"ROUTER_{name[-1].upper()}_ADDRESS:8000"
        if placeholder not in text:
            raise ValueError(f"{SHIPPED_CONFIG} has no {placeholder}")
        text = text.replace(placeholder, f"127.0.0.1:{router_port}")
    text, binds = re.subn(r"(?m)^(\s*)bind :8000$", rf"\1bind 127.0.0.1:{port}", text)
    logging = "defaults\n    log global\n    option httplog"
    text, defaults = re.subn(r"(?m)^defaults$", logging, text)
    if binds != 1 or defaults != 1:
        raise ValueError(f"{SHIPPED_CONFIG} needs one `bind :8000` and one `defaults` section")
    return text


class Routers:
    """The router pair's processes, one per name, restarted on the same port."""

    def __init__(
        self, out: Path, ports: dict[str, int], lease_ttl: float, takeover_after: int
    ) -> None:
        self.out = out
        self.ports = ports
        self.lease_ttl = lease_ttl
        self.takeover_after = takeover_after
        self.processes: dict[str, subprocess.Popen] = {}
        self.starts: Counter[str] = Counter()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}"

    def start(self, name: str, standby_of: str | None) -> subprocess.Popen:
        self.starts[name] += 1
        tag = f"{name}-{self.starts[name]}"
        argv = [
            sys.executable,
            "-m",
            "narwhal.cli",
            "--fleet",
            str(self.out / name / "fleet.json"),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.ports[name]),
            "--router-id",
            name,
            "--lease-path",
            str(self.out / "router.lease"),
            "--lease-ttl",
            repr(self.lease_ttl),
            "--journal",
            str(self.out / f"{tag}.journal.jsonl"),
        ]
        if standby_of is not None:
            argv += [
                "--standby-of",
                self.url(standby_of),
                "--standby-takeover-after",
                str(self.takeover_after),
            ]
        with open(self.out / f"{tag}.log", "xb") as log:
            process = subprocess.Popen(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=self.out,
                env=router_env(str(ROOT / "src")),
            )
        self.processes[name] = process
        return process


class Timeline:
    """Each router's `/ready` answers, polled directly, as (time, name, state) changes."""

    def __init__(self, routers: Routers) -> None:
        self.routers = routers
        self.events: list[tuple[float, str, str]] = []
        self.state: dict[str, str] = {}

    async def poll(self, http: httpx.AsyncClient, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for name in self.routers.ports:
                try:
                    response = await http.get(self.routers.url(name) + "/ready", timeout=0.5)
                    reason = response.json().get("reason") or "ready"
                    state = f"{response.status_code} {reason}"
                except httpx.ConnectError:
                    state = "refused"
                except httpx.HTTPError as exc:
                    state = type(exc).__name__
                if self.state.get(name) != state:
                    self.state[name] = state
                    self.events.append((time.time(), name, state))
            await asyncio.sleep(POLL_S)

    def both_ready(self) -> list[float]:
        """Return each moment at which both routers answered `/ready` with HTTP 200."""
        state: dict[str, str] = {}
        moments = []
        for moment, name, value in self.events:
            state[name] = value
            if len(state) == len(self.routers.ports) and all(
                item == "200 ready" for item in state.values()
            ):
                moments.append(moment)
        return moments

    def first(self, name: str, after: float, predicate) -> float | None:
        for moment, router, state in self.events:
            if router == name and moment >= after and predicate(state):
                return moment
        return None


async def wait_ready(http: httpx.AsyncClient, url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await http.get(url + "/ready")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError(f"{url} was not ready within {timeout:g} s")


async def wait_standby(http: httpx.AsyncClient, url: str, timeout: float) -> None:
    """Wait until a standby answers `/ready` with its shadowing refusal."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await http.get(url + "/ready")
            if response.status_code == 503 and response.json().get("reason") == "shadowing":
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError(f"{url} did not report shadowing within {timeout:g} s")


async def wait_exit(process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if time.monotonic() > deadline:
            raise RuntimeError(f"router {process.pid} did not exit within {timeout:g} s")
        await asyncio.sleep(0.005)


async def offer(
    http: httpx.AsyncClient, base: str, rate: float, duration: float, seed: int
) -> list[dict]:
    """Offer open-loop Poisson load and return one row per request."""
    requests = max(1, round(rate * duration))
    bodies = client_bodies(SHAPE.input_tokens, SHAPE.output_tokens, requests, 1, 0)
    arrivals = random.Random(seed)
    start = time.monotonic()
    moment = 0.0
    rows: list[dict] = []

    async def send(sequence: int, body: dict, scheduled: float) -> None:
        await asyncio.sleep(max(0.0, scheduled - time.monotonic()))
        row = await trial.request_one(http, base, body, f"t-{sequence}", scheduled, 60.0)
        row["ended_at"] = time.time()
        rows.append(row)

    tasks = []
    for sequence, body in bodies:
        moment += arrivals.expovariate(rate)
        tasks.append(asyncio.create_task(send(sequence, body, start + moment)))
    await asyncio.gather(*tasks)
    return rows


def haproxy_lines(name: str) -> list[str]:
    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
    return [line for line in (logs.stdout + logs.stderr).splitlines() if " narwhal" in line]


async def wait_holder(http: httpx.AsyncClient, routers: Routers, names: list[str]) -> str:
    """Wait until one of `names` answers `/ready` with HTTP 200 and return its name."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        for name in names:
            try:
                if (await http.get(routers.url(name) + "/ready")).status_code == 200:
                    return name
            except httpx.TransportError:
                pass
        await asyncio.sleep(0.02)
    raise RuntimeError(f"none of {names} was ready within 30 s")


async def current_holder(http: httpx.AsyncClient, routers: Routers) -> str:
    """Return the one router that answers `/ready` with HTTP 200, refusing two holders."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        ready = []
        for name in ("router-a", "router-b"):
            try:
                if (await http.get(routers.url(name) + "/ready")).status_code == 200:
                    ready.append(name)
            except httpx.TransportError:
                pass
        if len(ready) == 2:
            raise RuntimeError("both routers answer /ready as the holder")
        if ready:
            return ready[0]
        await asyncio.sleep(0.02)
    raise RuntimeError("no router was ready within 30 s")


async def takeovers(
    args: argparse.Namespace,
    http: httpx.AsyncClient,
    routers: Routers,
    timeline: Timeline,
) -> list[dict]:
    """Stop the active router once per takeover and return each takeover's record."""
    records = []
    await asyncio.sleep(args.warmup)
    for _ in range(args.takeovers):
        # Under load the standby can take the lease during the warmup, so stop whichever
        # router holds it now rather than the one that started as the primary.
        active = await current_holder(http, routers)
        standby = "router-b" if active == "router-a" else "router-a"
        old = routers.processes[active]
        signalled = time.time()
        old.send_signal(signal.SIGKILL if args.signal == "KILL" else signal.SIGTERM)
        record: dict = {"stopped": active, "signal": args.signal, "signalled_at": signalled}
        if args.both:
            # Both routers restart together, each a standby of the other.
            shadow = routers.processes[standby]
            shadow.send_signal(signal.SIGTERM)
            await wait_exit(old, 60.0)
            await wait_exit(shadow, 60.0)
            record["exited_s"] = round(time.time() - signalled, 3)
            routers.start(active, standby_of=standby)
            routers.start(standby, standby_of=active)
            holder = await wait_holder(http, routers, [active, standby])
            record["holder"] = holder
            record["holder_ready_s"] = round(time.time() - signalled, 3)
            other = standby if holder == active else active
            await wait_standby(http, routers.url(other), 30.0)
            records.append(record)
            active = holder
            await asyncio.sleep(args.interval)
            continue
        if args.restart_after is None:
            await wait_exit(old, 60.0)
            record["exited_s"] = round(time.time() - signalled, 3)
            routers.start(active, standby_of=standby)
        # A SIGTERM process answers as the holder until it starts shutting down, so only a
        # killed or restarted router is a candidate.
        candidates = [standby]
        if args.restart_after is None or args.signal == "KILL":
            candidates.append(active)
        holder = await wait_holder(http, routers, candidates)
        record["holder"] = holder
        if args.restart_after is not None and holder != standby:
            raise RuntimeError(f"{holder} took over, not the standby {standby}")
        record["holder_ready_s"] = round(time.time() - signalled, 3)
        if args.restart_after is not None:
            await wait_exit(old, 60.0)
            record["exited_s"] = round(time.time() - signalled, 3)
            await asyncio.sleep(args.restart_after)
            routers.start(active, standby_of=standby)
        other = standby if holder == active else active
        await wait_standby(http, routers.url(other), 30.0)
        records.append(record)
        active = holder
        await asyncio.sleep(args.interval)
    for record in records:
        start = record["signalled_at"]
        name = record["stopped"]
        refused = timeline.first(name, start, lambda state: state != "200 ready")
        answering = timeline.first(name, start, lambda state: state.startswith("503"))
        ready = timeline.first(record["holder"], start, lambda state: state == "200 ready")
        record["old_refused_s"] = refused and round(refused - start, 3)
        record["restarted_answered_s"] = answering and round(answering - start, 3)
        record["holder_ready_polled_s"] = ready and round(ready - start, 3)
    return records


def window_of(row: dict, records: list[dict], span: float) -> int | None:
    for index, record in enumerate(records):
        if record["signalled_at"] - 1.0 <= row["started_at"] <= record["signalled_at"] + span:
            return index
    return None


async def run(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True)
    engine_processes: list[subprocess.Popen] = []
    ports = {"router-a": free_port(), "router-b": free_port()}
    routers = Routers(out, ports, args.lease_ttl, args.takeover_after)
    container = f"narwhal-takeover-check-{os.getpid()}"
    lb_port = free_port()
    try:
        engines = []
        for index in range(SHAPE.engines):
            argv = [
                sys.executable,
                str(SIMULATED_ENGINE),
                "--iid",
                f"e{index}",
                "--token-interval",
                repr(SHAPE.token_interval_s),
                "--prefill-seconds",
                repr(SHAPE.prefill_s),
            ]
            with open(out / f"engine-e{index}.log", "xb") as log:
                process = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=log, cwd=out, env=child_env(None)
                )
            engine_processes.append(process)
            engines.append(await ready_line(process, time.monotonic() + 30.0))
        for name in routers.ports:
            (out / name).mkdir()
            write_fleet(out / name, engines, SHAPE)
        config = render_config(args.config.read_text(), lb_port, routers.ports)
        (out / "haproxy.cfg").write_text(config)
        (out / "haproxy.cfg").chmod(0o644)
        async with httpx.AsyncClient(
            trust_env=False, timeout=60.0, limits=httpx.Limits(max_connections=None)
        ) as http:
            routers.start("router-a", standby_of=None)
            await wait_ready(http, routers.url("router-a"), 60.0)
            routers.start("router-b", standby_of="router-a")
            await wait_standby(http, routers.url("router-b"), 60.0)
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    container,
                    "--network",
                    "host",
                    "-v",
                    f"{out / 'haproxy.cfg'}:/usr/local/etc/haproxy/haproxy.cfg:ro",
                    args.image,
                ],
                check=True,
                capture_output=True,
            )
            await wait_ready(http, f"http://127.0.0.1:{lb_port}", 30.0)
            timeline = Timeline(routers)
            stop = asyncio.Event()
            poller = asyncio.create_task(timeline.poll(http, stop))
            each = (
                args.interval
                + 3.0
                + (args.restart_after or 0.0)
                + (args.lease_ttl if args.both else 0.0)
            )
            duration = args.warmup + args.takeovers * each + 2.0
            load = asyncio.create_task(
                offer(http, f"http://127.0.0.1:{lb_port}", args.rate, duration, args.seed)
            )
            records = await takeovers(args, http, routers, timeline)
            rows = await load
            stop.set()
            await poller
    finally:
        if shutil.which("docker"):
            lines = haproxy_lines(container)
            (out / "haproxy.log").write_text("\n".join(lines) + "\n")
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
        for process in [*routers.processes.values(), *engine_processes]:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in [*routers.processes.values(), *engine_processes]:
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
    errors = []
    statuses: Counter[str] = Counter()
    for row in rows:
        statuses[str(row["status"])] += 1
        index = window_of(row, records, args.interval)
        if row["status"] in ACCEPTED and row["outcome"] in ("completed", "http_error"):
            continue
        started = row["started_at"] - records[index]["signalled_at"] if index is not None else None
        errors.append(
            {
                "takeover": index,
                "started_s": started is not None and round(started, 3),
                "duration_s": round(row["ended_at"] - row["started_at"], 3),
                "status": row["status"],
                "outcome": row["outcome"],
                # A request in flight on the router when it was killed cannot complete.
                "cut": args.signal == "KILL" and started is not None and started < 0,
                "body": row.get("error_body", row.get("error", ""))[:200],
            }
        )
    for record in records:
        record["signalled_at"] = round(record["signalled_at"], 3)
    report = {
        "config": str(args.config),
        "image": args.image,
        "rate": args.rate,
        "requests": len(rows),
        "statuses": dict(statuses),
        "takeovers": records,
        "errors": errors,
        # Moments, in seconds after the first signal, at which both routers reported ready.
        "both_ready": [
            round(moment - records[0]["signalled_at"], 3) for moment in timeline.both_ready()
        ]
        if records
        else [],
        "timeline": [
            [round(moment - records[0]["signalled_at"], 3), name, state]
            for moment, name, state in timeline.events
        ]
        if records
        else [],
        "out": str(out),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    with open(out / "requests.jsonl", "w") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    print(json.dumps({key: report[key] for key in report if key != "timeline"}, indent=2))
    return 1 if any(not error["cut"] for error in errors) or report["both_ready"] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--config", type=Path, default=SHIPPED_CONFIG)
    options.add_argument("--image", default=DEFAULT_IMAGE)
    options.add_argument("--out", type=Path, required=True)
    options.add_argument("--rate", type=float, default=2.0, help="requests per second")
    options.add_argument("--takeovers", type=int, default=2)
    options.add_argument("--signal", choices=("TERM", "KILL"), default="TERM")
    options.add_argument(
        "--restart-after",
        type=float,
        help="seconds after the new holder is ready before the stopped router restarts "
        "(default: restart it as soon as it exits)",
    )
    options.add_argument(
        "--both",
        action="store_true",
        help="also stop the standby with SIGTERM, then restart both routers at once, each a "
        "standby of the other",
    )
    options.add_argument("--warmup", type=float, default=5.0)
    options.add_argument("--interval", type=float, default=12.0, help="seconds between takeovers")
    options.add_argument("--lease-ttl", type=float, default=5.0)
    options.add_argument(
        "--takeover-after",
        type=int,
        default=4,
        help="failed probes, 0.25 s apart, before the standby takes over; raise it to "
        "delay the new holder's readiness after a SIGTERM",
    )
    options.add_argument("--seed", type=int, default=1)
    args = options.parse_args(argv)
    if args.both and args.restart_after is not None:
        options.error("--both restarts both routers at once; drop --restart-after")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
