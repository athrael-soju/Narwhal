from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path


class EngineLauncher(ABC):
    @abstractmethod
    def build(
        self, record: dict, env: Mapping[str, str], output: Path, *, backend: str
    ) -> tuple[dict, dict[str, str]]: ...

    @abstractmethod
    def check(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def capture_cache(self, run: Path, plan: dict) -> None: ...
