"""Engine-neutral KV fabric lifecycle rules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class FabricLifecycle(ABC):
    """How a backend's KV fabric holds and releases peer state."""

    # Seconds after an engine leaves placement at which peers get a release round.
    release_after_s: ClassVar[tuple[float, ...]] = ()
    release_retry_s: ClassVar[float] = 0.0

    @abstractmethod
    def check_engine_bind(self, host: str, port: int, *, fabric: bool = False) -> None:
        """Raise OSError unless the engine can bind its HTTP port, or its KV port with `fabric`."""
