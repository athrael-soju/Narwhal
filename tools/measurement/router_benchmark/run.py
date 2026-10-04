"""One benchmark run: simulated engines, one router and a rate sweep."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from tools.measurement import load_trial as trial
from tools.measurement.simulated_engine import SIMULATED_VERSION

from .client import sleep_until
from .cpus import Allocation
from .files import open_private, write_private
from .fleet import Shape, engine_roles, write_fleet
from .processes import ROOT, Children, child_env, free_port, ready_line
from .render import report_text
from .report import REPORT_KIND, VERSION, rate_row, select_point, stop_reason
from .sampling import edge_sample, steady_sample

PACKAGE = Path(__file__).resolve().parent
SIMULATED_ENGINE = ROOT / "tools" / "measurement" / "simulated_engine.py"


def package_digest(directory: Path) -> str:
    """Return the SHA-256 of the `sha256sum` listing of `directory`'s .py files in name order."""
    listing = "".join(
        f"{trial.digest(path)}  {path.name}\n" for path in sorted(directory.glob("*.py"))
    )
    return hashlib.sha256(listing.encode()).hexdigest()


def shape_of(args: argparse.Namespace) -> Shape:
    return Shape(
        engines=args.engines,
        prefill_engines=args.prefill_engines,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        token_interval_s=args.token_interval,
        frames_per_write=args.frames_per_write,
        prefill_s=args.prefill_seconds,
        ttft_slo_s=args.ttft_slo,
        tpot_slo_s=args.tpot_slo,
    )


def settings_of(args: argparse.Namespace) -> dict:
    return {
        **asdict(shape_of(args)),
        "rates": args.rates,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "clients": args.clients,
        "timeout_s": args.timeout,
        "drain_timeout_s": args.drain_timeout,
        "cpus": args.cpus,
        "python": args.python,
    }


class Run:
    """One benchmark run: simulated engines, one router and a rate sweep."""

    def __init__(
        self,
        args: argparse.Namespace,
        router_src: Path,
        label: str,
        out: Path,
        allocation: Allocation,
    ) -> None:
        self.args, self.router_src, self.label = args, router_src.resolve(), label
        self.out, self.allocation = out, allocation
        self.shape = shape_of(args)
        self.children = Children(out / "pids.json")
        self.engines: list[dict] = []
        self.roles: dict[str, str] = {}
        self.base = ""
        self.ramp_s = args.output_tokens * args.token_interval

    async def execute(self) -> int:
        """Run the sweep and write the report; return 1 when a client failed."""
        args, out = self.args, self.out
        out.mkdir(mode=0o700, parents=True)
        probe = subprocess.run(
            [args.python, "-c", "import json, narwhal; print(json.dumps(narwhal.__file__))"],
            env=child_env(str(self.router_src)),
            cwd=out,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        router_import = json.loads(probe.stdout)
        if not Path(router_import).resolve().is_relative_to(self.router_src):
            raise RuntimeError(f"narwhal imports from {router_import}, outside {self.router_src}")
        tools = {
            "router_benchmark_sha256": package_digest(PACKAGE),
            "simulated_engine_sha256": trial.digest(SIMULATED_ENGINE),
        }
        manifest = {
            "label": self.label,
            "settings": settings_of(args),
            "allocation": asdict(self.allocation),
            "router_src": str(self.router_src),
            "router_import": router_import,
            **tools,
            "python": args.python,
            "python_version": platform.python_version(),
            "argv": sys.argv,
            "started_at": time.time(),
        }
        write_private(out / "manifest.json", manifest)
        self.children.record()
        rows: list[dict] = []
        stopped_by = None
        stop_detail = None
        async with httpx.AsyncClient(trust_env=False, timeout=10.0) as http:
            try:
                await self.start_engines()
                write_fleet(out, self.engines, self.shape)
                router_source = await self.start_router(http)
                write_private(
                    out / "manifest.json",
                    manifest | {"router_url": self.base, "router_source": router_source},
                )
                warm = await self.offer(
                    http, args.rates[0], args.warmup, out / "rates" / "warmup", sampled=False
                )
                if any(code != 0 for code in warm["client_exit"].values()):
                    raise RuntimeError("a warmup client failed; inspect rates/warmup/client-*.log")
                if warm["drain"] != "idle":
                    raise RuntimeError(f"router drain after warmup ended in {warm['drain']}")
                for rate in args.rates:
                    directory = out / "rates" / format(rate, "g")
                    try:
                        samples = await self.offer(
                            http, rate, args.duration, directory, sampled=True
                        )
                    except httpx.HTTPError as error:
                        stopped_by = "sample_failed"
                        stop_detail = f"{type(error).__name__}: {error}".rstrip(": ")
                        break
                    trial.private_json(directory / "samples.json", samples)
                    client_rows = [
                        json.loads(line)
                        for index in range(args.clients)
                        for line in (directory / f"client-{index}.jsonl").read_text().splitlines()
                    ]
                    row = rate_row(
                        rate,
                        samples["requests"],
                        client_rows,
                        samples,
                        self.allocation,
                        self.roles,
                    )
                    rows.append(row)
                    failed = any(code != 0 for code in samples["client_exit"].values())
                    stopped_by = stop_reason(row, failed)
                    if stopped_by:
                        break
            finally:
                self.children.stop_all()
        report = {
            "kind": REPORT_KIND,
            "version": VERSION,
            "label": self.label,
            "router_src": str(self.router_src),
            "router_import": router_import,
            "router_source": router_source,
            "tools": tools,
            "settings": settings_of(args),
            "allocation": asdict(self.allocation),
            "rates": rows,
            "stopped_by": stopped_by,
            "stop_detail": stop_detail,
            "point": select_point(rows),
        }
        trial.private_json(out / "report.json", report)
        text = report_text(report)
        with open_private(out / "report.txt") as stream:
            stream.write(text)
        print(text, end="", flush=True)
        return 1 if stopped_by == "client_failed" else 0

    async def start_engines(self) -> None:
        args = self.args
        for index, cpu in enumerate(self.allocation.engines):
            iid = f"e{index}"
            argv = [
                args.python,
                str(SIMULATED_ENGINE),
                "--iid",
                iid,
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--token-interval",
                repr(args.token_interval),
                "--frames-per-write",
                str(args.frames_per_write),
                "--prefill-seconds",
                repr(args.prefill_seconds),
            ]
            with open(self.out / f"engine-{iid}.log", "xb") as log:
                self.children.engines[iid] = self.children.spawn(
                    argv,
                    cpu,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    cwd=self.out,
                    env=child_env(None),
                )
            self.children.record()
        deadline = time.monotonic() + 30.0
        for iid, process in self.children.engines.items():
            line = await ready_line(process, deadline)
            if line.get("iid") != iid or line.get("version") != SIMULATED_VERSION:
                raise RuntimeError(f"simulated engine {iid} printed an unexpected ready line")
            self.engines.append(line)
        self.roles = {
            iid: role.value
            for iid, role in engine_roles(self.engines, args.prefill_engines).items()
        }

    async def start_router(self, http: httpx.AsyncClient) -> str:
        """Start the router, wait for readiness and return its source digest."""
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        argv = [
            self.args.python,
            "-m",
            "narwhal.cli",
            "--fleet",
            str(self.out / "fleet.json"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--journal",
            str(self.out / "journal.jsonl"),
        ]
        with open(self.out / "router.log", "xb") as log:
            self.children.router = self.children.spawn(
                argv,
                self.allocation.router,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=self.out,
                env=child_env(str(self.router_src)),
            )
        self.children.record()
        deadline = time.monotonic() + 60.0
        while True:
            if self.children.router.poll() is not None:
                self.children.record()
                raise RuntimeError(
                    f"router exited {self.children.router.returncode}; see router.log"
                )
            try:
                if (await http.get(self.base + "/ready")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("router did not become ready within 60 s; see router.log")
            await asyncio.sleep(0.2)
        with open(self.out / "journal.jsonl") as journal:
            return json.loads(journal.readline())["meta"]["source"]

    async def offer(
        self,
        http: httpx.AsyncClient,
        rate: float,
        duration: float,
        directory: Path,
        *,
        sampled: bool,
    ) -> dict:
        """Offer `rate` for `duration` from the clients; sample the router when `sampled`."""
        args = self.args
        directory.mkdir(mode=0o700, parents=True)
        requests = round(rate * duration)
        self.children.clients = {}
        for index, cpu in enumerate(self.allocation.clients):
            argv = [
                args.python,
                "-m",
                "tools.measurement.router_benchmark.cli",
                "client",
                "--base",
                self.base,
                "--input-tokens",
                str(args.input_tokens),
                "--output-tokens",
                str(args.output_tokens),
                "--rate",
                repr(rate),
                "--requests",
                str(requests),
                "--clients",
                str(args.clients),
                "--index",
                str(index),
                "--timeout",
                repr(args.timeout),
                "--out",
                str(directory / f"client-{index}.jsonl"),
            ]
            with open(directory / f"client-{index}.log", "xb") as log:
                self.children.clients[str(index)] = self.children.spawn(
                    argv,
                    cpu,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    cwd=self.out,
                    env=child_env(str(ROOT)),
                )
            self.children.record()
        deadline = time.monotonic() + 60.0
        for process in self.children.clients.values():
            if await ready_line(process, deadline) != {"ready": True}:
                raise RuntimeError(f"client {process.pid} printed an unexpected ready line")
        result: dict = {"requests": requests}
        if sampled:
            result["before"] = await edge_sample(http, self.base, self.children)
        start_at = time.monotonic() + 1.0
        for process in self.children.clients.values():
            process.stdin.write((json.dumps({"start_at": start_at}) + "\n").encode())
            process.stdin.close()
        if sampled:
            await sleep_until(start_at + self.ramp_s)
            result["t0"] = await steady_sample(
                http, self.base, self.children, self.allocation, self.engines
            )
            await sleep_until(start_at + duration)
            result["t1"] = await steady_sample(
                http, self.base, self.children, self.allocation, self.engines
            )
        result["client_exit"] = await self.wait_clients(
            start_at + duration + self.ramp_s + args.timeout + 30.0
        )
        result["drain"] = (await trial.poll_drain(http, self.base, args.drain_timeout))["condition"]
        if sampled:
            result["after"] = await edge_sample(http, self.base, self.children)
        return result

    async def wait_clients(self, deadline: float) -> dict[str, int]:
        clients = self.children.clients
        exited = 0
        while exited < len(clients) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            count = sum(process.poll() is not None for process in clients.values())
            if count != exited:
                exited = count
                self.children.record()
        self.children.stop(list(clients.values()))
        return {key: process.returncode for key, process in clients.items()}
