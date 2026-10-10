from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class FabricLifecycle(ABC):
    release_after_s: ClassVar[tuple[float, ...]] = ()
    release_retry_s: ClassVar[float] = 0.0

    @abstractmethod
    def check_engine_bind(self, host: str, port: int, *, fabric: bool = False) -> None: ...
