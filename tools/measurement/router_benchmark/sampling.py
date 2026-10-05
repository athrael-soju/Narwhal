"""Router, client, engine and core samples."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import httpx

from tools.measurement.benchmark_evidence import metric_values
from tools.measurement.simulated_engine import LATE_TICKS_METRIC

from .cpus import Allocation, cpu_times, proc_seconds
from .processes import Children


def late_ticks(text: str) -> int:
    """Return the late-tick counter from simulated-engine /metrics text."""
    match = re.search(rf"^{LATE_TICKS_METRIC}\s+(\S+)$", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"simulated engine /metrics lacks {LATE_TICKS_METRIC}")
    return int(float(match[1]))


async def edge_sample(http: httpx.AsyncClient, base: str, children: Children) -> dict:
    """Sample router CPU time and router state."""
    mono = time.monotonic()
    router_cpu = proc_seconds(children.router.pid)
    response = await http.get(base + "/narwhal/state")
    response.raise_for_status()
    return {"mono": mono, "router_cpu_s": router_cpu, "state": response.json()}


async def steady_sample(
    http: httpx.AsyncClient,
    base: str,
    children: Children,
    allocation: Allocation,
    engines: list[dict],
) -> dict:
    """Sample process CPU time, router core ticks, router metrics and engine late ticks."""
    mono = time.monotonic()
    router_cpu = proc_seconds(children.router.pid)
    clients = {key: proc_seconds(proc.pid) for key, proc in children.clients.items()}
    engine_cpu = {iid: proc_seconds(proc.pid) for iid, proc in children.engines.items()}
    stat = Path("/proc/stat").read_text()
    cores = {
        str(cpu): cpu_times(stat, cpu) for cpu in (allocation.router, *allocation.router_siblings)
    }
    responses = await asyncio.gather(
        http.get(base + "/metrics"),
        *(http.get(line["url"] + "/metrics") for line in engines),
    )
    for response in responses:
        response.raise_for_status()
    return {
        "mono": mono,
        "router_cpu_s": router_cpu,
        "clients": clients,
        "engines": engine_cpu,
        "cores": cores,
        "router_metrics": metric_values(responses[0].text),
        "late_ticks": {
            line["iid"]: late_ticks(response.text)
            for line, response in zip(engines, responses[1:], strict=True)
        },
    }
