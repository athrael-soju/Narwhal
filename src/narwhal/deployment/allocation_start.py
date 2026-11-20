"""Start a backend's engines as plain processes inside a scheduler allocation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationEngine:
    """One engine process on one node of an allocation."""

    iid: str
    model: str
    served_name: str
    # The node address the engine binds and its peers reach.
    host: str
    port: int
    # The port of the engine's KV transfer side channel.
    transfer_port: int
    tensor_parallel: int
    max_sequences: int | None = None
    extra_args: tuple[str, ...] = ()


class AllocationStart(ABC):
    @abstractmethod
    def command(self, python: str, engine: AllocationEngine) -> list[str]:
        """The engine's argument vector, run with the engine image's `python`."""

    def environment(self, engine: AllocationEngine) -> dict[str, str]:
        """Variables the engine process needs beyond the launch environment."""
        return {}
