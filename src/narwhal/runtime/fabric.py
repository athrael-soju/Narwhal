from __future__ import annotations

from typing import ClassVar

from . import listeners


class FabricLifecycle:
    release_after_s: ClassVar[tuple[float, ...]] = ()
    release_retry_s: ClassVar[float] = 0.0
    # True when the engine's fabric listener enables IPv6 dual-stack whatever the kernel default.
    fabric_dual_stack: ClassVar[bool] = False

    def check_engine_bind(self, host: str, port: int, *, fabric: bool = False) -> None:
        listeners.check_engine_bind(host, port, dual_stack=fabric and self.fabric_dual_stack)
