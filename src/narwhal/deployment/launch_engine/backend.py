"""Engine-neutral launch operations that each backend implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path


class EngineLauncher(ABC):
    """Build, check and capture one backend's engine launch."""

    @abstractmethod
    def build(
        self, record: dict, env: Mapping[str, str], output: Path, *, backend: str
    ) -> tuple[dict, dict[str, str]]:
        """Resolve one engine record into a launch plan and its environment."""

    @abstractmethod
    def check(self, run: Path, plan: dict) -> None:
        """Check the pinned runtime inside the launch environment."""

    @abstractmethod
    def capture_cache(self, run: Path, plan: dict) -> None:
        """Capture the engine's KV cache shape for its attestation."""
