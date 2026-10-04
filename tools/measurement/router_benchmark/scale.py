"""Several router processes against one set of simulated engines."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from tools.measurement import load_trial as trial
from tools.measurement.benchmark_evidence import metric_values
from tools.measurement.simulated_engine import ACTIVE_METRIC

from .client import client_argv, sleep_until
from .cpus import Allocation, proc_seconds
from .files import open_private, write_private
from .fleet import write_fleet
from .processes import ROOT, child_env, free_port, ready_line
from .report import BUSY_METRIC, per
from .run import Run, settings_of

SCALE_KIND = "narwhal-router-scale"
SCALE_VERSION = 1
ACTIVE = re.compile(rf'^{ACTIVE_METRIC}\{{phase="(prefill|decode)"\}}\s+(\S+)$', re.MULTILINE)


@dataclass(frozen=True)
class ScaleAllocation:
    routers: list[int]
    router_siblings: list[int]
    clients: list[int]
    engines: list[int]
    driver: int


def allocate_scale(
    cpus: list[int], siblings: dict[int, list[int]], routers: int, clients: int, engines: int
) -> ScaleAllocation:
    """Assign routers, their idle siblings, clients, engines and the driver to CPUs."""
    chosen = cpus[:routers]
    router_siblings = sorted(
        {cpu for router in chosen for cpu in siblings.get(router, []) if cpu not in chosen}
    )
    idle = set(router_siblings) & set(cpus)
    free = [cpu for cpu in cpus[routers:] if cpu not in idle]
    need = routers + len(idle) + clients + engines + 1
    if len(cpus) < need:
        raise ValueError(
            f"--cpus holds {len(cpus)} CPUs; the run needs {need}: {routers} routers, "
            f"{len(idle)} idle router siblings, {clients} clients, {engines} engines and the driver"
        )
    return ScaleAllocation(
        routers=chosen,
        router_siblings=router_siblings,
        clients=free[:clients],
        engines=free[clients : clients + engines],
        driver=free[-1],
    )


def engine_active(text: str) -> dict[str, float]:
    """Prefill and decode requests in progress from simulated-engine /metrics text."""
    return {phase: float(value) for phase, value in ACTIVE.findall(text)}


def disagreement_seconds(samples: list[dict]) -> int:
    """Count samples in which the routers' prefill and decode pools differ."""
    return sum(
        len({json.dumps(router["pools"], sort_keys=True) for router in sample["routers"]}) > 1
        for sample in samples
    )


def resident_gap(samples: list[dict]) -> dict:
    """Compare the routers' summed decode residents with the engines' active decode streams."""
    routers, engines, gaps = [], [], []
    for sample in samples:
        counted: Counter[str] = Counter()
        for router in sample["routers"]:
            for iid, resident in router["resident"].items():
                counted[iid] += resident["decode"]
        active = {iid: values["decode"] for iid, values in sample["engines"].items()}
        routers.append(sum(counted.values()))
        engines.append(sum(active.values()))
        gaps.append(sum(abs(counted[iid] - active[iid]) for iid in active))
    count = len(samples)
    return {
        "mean_router_decode": per(sum(routers), count),
        "mean_engine_decode": per(sum(engines), count),
        "mean_abs_gap": per(sum(gaps), count),
    }


def scale_row(
    rate: float,
    offered: int,
    rows: list[dict],
    samples: list[dict],
    edges: dict,
    window_s: float,
) -> dict:
    """One rate: aggregate relay, per-router CPU and busy share, residents and role agreement."""
    completed = [row for row in rows if row["outcome"] == "completed"]
    relayed = sum(row["token_events"] for row in rows)
    starts = [row["scheduled_mono"] + row["schedule_lag_s"] for row in rows]
    ends = [start + row["elapsed_s"] for start, row in zip(starts, rows, strict=True)]
    span = max(ends) - min(starts) if rows else 0.0
    routers = []
    edge_rows = zip(edges["start"], edges["before"], edges["after"], edges["drained"], strict=True)
    for index, (start, first, last, drained) in enumerate(edge_rows):
        cpu_s = last["cpu_s"] - first["cpu_s"]
        busy = None
        if BUSY_METRIC in first["metrics"] and BUSY_METRIC in last["metrics"]:
            busy = (last["metrics"][BUSY_METRIC] - first["metrics"][BUSY_METRIC]) / window_s
        routers.append(
            {
                "index": index,
                "cpu_s": cpu_s,
                "busy_share": busy,
                "saturation_rejections": drained["rejected"] - start["rejected"],
                "refused": drained["refused"] - start["refused"],
            }
        )
    decode = [sum(e["decode"] for e in sample["engines"].values()) for sample in samples]
    return {
        "offered_rps": rate,
        "offered": offered,
        "completed": len(completed),
        "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "relayed_frames": relayed,
        "span_s": span,
        "relayed_frames_per_s": per(relayed, span),
        "requests_per_s": per(len(completed), span),
        "saturation_rejections": sum(router["saturation_rejections"] for router in routers),
        "refused": sum(router["refused"] for router in routers),
        "routers": routers,
        "residents": resident_gap(samples),
        "mean_engine_decode_streams": per(sum(decode), len(decode)),
        "max_engine_decode_streams": max(
            (e["decode"] for sample in samples for e in sample["engines"].values()), default=0.0
        ),
        "role_disagreement_s": disagreement_seconds(samples),
        "samples": len(samples),
    }


def select_scale_point(rows: list[dict]) -> dict | None:
    """Return the highest offered rate without saturation rejections."""
    clean = [row for row in rows if row["saturation_rejections"] == 0]
    if not clean:
        return None
    best = max(clean, key=lambda row: row["offered_rps"])
    return {
        key: best[key]
        for key in ("offered_rps", "relayed_frames_per_s", "requests_per_s", "role_disagreement_s")
    }


def scale_text(doc: dict) -> str:
    lines = [
        f"{doc['label']} routers {doc['routers']} router_source {doc['router_source']}",
        "rate_rps frames_per_s requests_per_s rejections refused router_busy role_disagreement_s",
    ]
    for row in doc["rates"]:
        busy = ",".join(
            "-" if router["busy_share"] is None else f"{router['busy_share']:.2f}"
            for router in row["routers"]
        )
        lines.append(
            f"{row['offered_rps']:g} {row['relayed_frames_per_s'] or 0:.1f} "
            f"{row['requests_per_s'] or 0:.2f} {row['saturation_rejections']} {row['refused']} "
            f"{busy} {row['role_disagreement_s']}"
        )
    point = doc["point"]
    lines.append(
        "point: none"
        if point is None
        else f"point: {point['offered_rps']:g} rps, {point['relayed_frames_per_s']:.1f} frames/s, "
        f"{point['requests_per_s']:.2f} requests/s"
    )
    detail = f" ({doc['stop_detail']})" if doc.get("stop_detail") else ""
    lines.append(f"stopped_by: {doc['stopped_by'] or '-'}{detail}")
    return "\n".join(lines) + "\n"


class ScaleRun:
    """Simulated engines, `--routers` router processes without a lease, and a rate sweep."""

    def __init__(self, args: argparse.Namespace, out: Path, allocation: ScaleAllocation) -> None:
        self.args, self.out, self.allocation = args, out, allocation
        # The single-router run supplies engine startup and child bookkeeping.
        self.engines_run = Run(
            args,
            args.router_src,
            args.label,
            out,
            Allocation(
                router=allocation.routers[0],
                router_siblings=allocation.router_siblings,
                clients=allocation.clients,
                engines=allocation.engines,
                driver=allocation.driver,
            ),
        )
        self.children = self.engines_run.children
        self.routers: list[subprocess.Popen] = []
        self.bases: list[str] = []

    async def execute(self) -> int:
        args, out = self.args, self.out
        out.mkdir(mode=0o700, parents=True)
        write_private(
            out / "manifest.json",
            {
                "label": args.label,
                "routers": args.routers,
                "settings": settings_of(args),
                "allocation": asdict(self.allocation),
                "router_src": str(Path(args.router_src).resolve()),
                "started_at": time.time(),
            },
        )
        rows: list[dict] = []
        stopped_by = None
        stop_detail = None
        router_source = None
        async with httpx.AsyncClient(trust_env=False, timeout=10.0) as http:
            try:
                await self.engines_run.start_engines()
                fleet = write_fleet(out, self.engines_run.engines, self.engines_run.shape)
                router_source = await self.start_routers(http, fleet)
                for rate in args.rates:
                    directory = out / "rates" / format(rate, "g")
                    try:
                        offered = await self.offer(http, rate, directory)
                    except httpx.HTTPError as error:
                        stopped_by = "sample_failed"
                        stop_detail = f"{type(error).__name__}: {error}".rstrip(": ")
                        break
                    rows_, samples, edges, exits, window = offered
                    row = scale_row(
                        rate, round(rate * args.duration), rows_, samples, edges, window
                    )
                    rows.append(row)
                    trial.private_json(directory / "samples.json", {"samples": samples, **edges})
                    if any(code != 0 for code in exits.values()):
                        stopped_by = "client_failed"
                    elif row["saturation_rejections"] > 0:
                        stopped_by = "saturation"
                    if stopped_by:
                        break
            finally:
                self.children.stop([*self.children.clients.values(), *self.routers])
                self.children.stop(list(self.children.engines.values()))
        report = {
            "kind": SCALE_KIND,
            "version": SCALE_VERSION,
            "label": args.label,
            "routers": args.routers,
            "router_source": router_source,
            "settings": settings_of(args),
            "allocation": asdict(self.allocation),
            "rates": rows,
            "stopped_by": stopped_by,
            "stop_detail": stop_detail,
            "point": select_scale_point(rows),
        }
        trial.private_json(out / "report.json", report)
        text = scale_text(report)
        with open_private(out / "report.txt") as stream:
            stream.write(text)
        print(text, end="", flush=True)
        return 1 if stopped_by == "client_failed" else 0

    async def start_routers(self, http: httpx.AsyncClient, fleet: Path) -> str:
        """Start one router per router CPU with its own fleet state file, port and journal."""
        sources = []
        document = json.loads(fleet.read_text())
        for index, cpu in enumerate(self.allocation.routers):
            own = self.out / f"fleet-{index}.json"
            document["controller"] = {**document.get("controller", {}), "advisory": False}
            document.setdefault("recovery", {})["state_path"] = str(
                self.out / f"state-{index}.json"
            )
            write_private(own, document)
            port = free_port()
            self.bases.append(f"http://127.0.0.1:{port}")
            argv = [
                self.args.python,
                "-m",
                "narwhal.cli",
                "--fleet",
                str(own),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--journal",
                str(self.out / f"journal-{index}.jsonl"),
            ]
            with open(self.out / f"router-{index}.log", "xb") as log:
                self.routers.append(
                    self.children.spawn(
                        argv,
                        cpu,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=self.out,
                        env=child_env(str(Path(self.args.router_src).resolve())),
                    )
                )
        deadline = time.monotonic() + 60.0
        for index, (base, process) in enumerate(zip(self.bases, self.routers, strict=True)):
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"router {index} exited {process.returncode}")
                try:
                    if (await http.get(base + "/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(f"router {index} did not become ready within 60 s")
                await asyncio.sleep(0.2)
            with open(self.out / f"journal-{index}.jsonl") as journal:
                sources.append(json.loads(journal.readline())["meta"]["source"])
        if len(set(sources)) != 1:
            raise RuntimeError("routers report different sources")
        return sources[0]

    async def router_edge(self, http: httpx.AsyncClient) -> list[dict]:
        """Each router's CPU seconds, metrics and admission counters."""
        rows = []
        for base, process in zip(self.bases, self.routers, strict=True):
            cpu_s = proc_seconds(process.pid)
            metrics = metric_values((await http.get(base + "/metrics")).text)
            admission = (await http.get(base + "/narwhal/state")).json()["admission"]
            rows.append(
                {
                    "cpu_s": cpu_s,
                    "metrics": metrics,
                    "rejected": admission["rejected"],
                    "refused": admission["refused"],
                }
            )
        return rows

    async def sample(self, http: httpx.AsyncClient) -> dict:
        states = await asyncio.gather(*(http.get(base + "/narwhal/state") for base in self.bases))
        engines = await asyncio.gather(
            *(http.get(line["url"] + "/metrics") for line in self.engines_run.engines)
        )
        return {
            "mono": time.monotonic(),
            "routers": [
                {"pools": state.json()["pools"], "resident": state.json()["resident"]}
                for state in states
            ],
            "engines": {
                line["iid"]: engine_active(response.text)
                for line, response in zip(self.engines_run.engines, engines, strict=True)
            },
        }

    async def offer(
        self, http: httpx.AsyncClient, rate: float, directory: Path
    ) -> tuple[list[dict], list[dict], dict, dict[str, int], float]:
        args = self.args
        directory.mkdir(mode=0o700, parents=True)
        requests = round(rate * args.duration)
        self.children.clients = {}
        for index, cpu in enumerate(self.allocation.clients):
            base = self.bases[index % len(self.bases)]
            argv = client_argv(args, base, rate, requests, index, directory)
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
        deadline = time.monotonic() + 60.0
        for process in self.children.clients.values():
            if await ready_line(process, deadline) != {"ready": True}:
                raise RuntimeError(f"client {process.pid} printed an unexpected ready line")
        start = await self.router_edge(http)
        start_at = time.monotonic() + 1.0
        for process in self.children.clients.values():
            process.stdin.write((json.dumps({"start_at": start_at}) + "\n").encode())
            process.stdin.close()
        ramp = args.output_tokens * args.token_interval
        await sleep_until(start_at + ramp)
        before = await self.router_edge(http)
        window_start = time.monotonic()
        samples = []
        while time.monotonic() < start_at + args.duration:
            samples.append(await self.sample(http))
            await sleep_until(min(start_at + args.duration, time.monotonic() + 1.0))
        after = await self.router_edge(http)
        window = time.monotonic() - window_start
        exits = await self.engines_run.wait_clients(
            start_at + args.duration + ramp + args.timeout + 30.0
        )
        for base in self.bases:
            await trial.poll_drain(http, base, args.drain_timeout)
        drained = await self.router_edge(http)
        rows = [
            json.loads(line)
            for index in range(args.clients)
            for line in (directory / f"client-{index}.jsonl").read_text().splitlines()
        ]
        edges = {"start": start, "before": before, "after": after, "drained": drained}
        return rows, samples, edges, exits, window
