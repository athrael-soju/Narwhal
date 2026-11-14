"""Start a backend's engines as plain native processes, with no launch plan or attestation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

    def environment(self, binary: Path) -> dict[str, str]:
        """Variables the engine process needs beyond the caller's environment."""
        return {}

    @abstractmethod
    def probe(self, served_name: str) -> tuple[str, dict[str, Any]]:
        """The endpoint and body of the one short request that measures an engine."""

    @abstractmethod
    def figures(self, reply: dict[str, Any], runtime: dict[str, Any]) -> NominalFigures:
        """The engine's nominal profile figures from its reply to `probe`."""
