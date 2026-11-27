"""Start a backend's engines as plain native processes, with no launch plan or attestation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

# Seconds one measuring request may take.
PROBE_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class DirectEngine:
    """One engine process of a fleet that shares a host."""

    model: Path
    served_name: str
    host: str
    port: int
    # The template's runtime section.
    runtime: dict[str, Any]
    # A directory every engine of the fleet reads and writes.
    shared_dir: Path
    accelerated: bool
    threads: int


@dataclass(frozen=True)
class NominalFigures:
    prefill_token_s: float
    token_interval_s: float
    kv_capacity_tokens: int
    max_requests: int


class DirectStart(ABC):
    @abstractmethod
    def check(self, binary: Path, runtime: dict[str, Any]) -> str:
        """Return the binary's version; raise ValueError when the runtime section refuses it."""

    @abstractmethod
    def command(self, binary: Path, engine: DirectEngine) -> list[str]: ...

    def environment(
        self, binary: Path, inherited: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Variables the engine process needs beyond `inherited`, the caller's environment
        by default."""
        return {}

    @abstractmethod
    def probe(self, served_name: str) -> tuple[str, dict[str, Any]]:
        """The endpoint and body of a short request that measures an engine."""

    def probe_requests(self, runtime: dict[str, Any]) -> int:
        """How many probe requests one engine serves at once when the fleet is measured."""
        return 1

    @abstractmethod
    def figures(self, reply: dict[str, Any], runtime: dict[str, Any]) -> NominalFigures:
        """The engine's nominal profile figures from its reply to `probe`."""


def measure(start: DirectStart, fleet: dict, runtime: dict) -> dict[str, NominalFigures]:
    """Each engine's figures with every engine serving its full number of requests.

    Engines that share a device are measured together, because an engine measured alone
    reports a speed it loses as soon as its neighbours serve.
    """
    endpoint, body = start.probe(fleet["model"])
    each = start.probe_requests(runtime)
    if each < 1:
        raise ValueError("an engine is measured with at least one request")
    engines = [engine for engine in fleet["engines"] for _ in range(each)]

    def one(engine: dict) -> NominalFigures:
        reply = client.post(engine["url"] + endpoint, json=body)
        reply.raise_for_status()
        return start.figures(reply.json(), runtime)

    client = httpx.Client(
        timeout=PROBE_TIMEOUT_S,
        trust_env=False,
        limits=httpx.Limits(max_connections=len(engines)),
    )
    pool = ThreadPoolExecutor(max_workers=len(engines))
    try:
        pending = [pool.submit(one, engine) for engine in engines]
        wait(pending, return_when=FIRST_EXCEPTION)
        # A failed request raises here while the others may still be in flight.
        measured = [future.result() for future in pending if future.done()]
        if len(measured) != len(pending):
            raise ValueError("an engine measurement ended early")
    finally:
        # Closing the client ends requests still in flight, so a hung engine holds nothing.
        pool.shutdown(wait=False, cancel_futures=True)
        client.close()
    figures = {}
    for index, engine in enumerate(fleet["engines"]):
        own = measured[index * each : (index + 1) * each]
        figures[engine["iid"]] = replace(
            own[0],
            prefill_token_s=sum(f.prefill_token_s for f in own) / each,
            token_interval_s=sum(f.token_interval_s for f in own) / each,
        )
    return figures
