"""Engine backends registered through the `narwhal.backends` entry-point group."""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..deployment.launch_engine.backend import EngineLauncher
    from ..engines.attestation import EngineIdentityReader
    from ..engines.connector import KvHandoff
    from ..engines.dialect import EngineDialect
    from ..engines.kv_events import KvEventDecoder
    from ..engines.metrics import EngineMetrics
    from ..runtime.fabric import FabricLifecycle
    from ..runtime.role_switch import RoleSwitcher

GROUP = "narwhal.backends"


@dataclass(frozen=True)
class EngineBackend:
    """One backend's implementation of each engine interface; `role_switch` is optional."""

    name: str
    dialect: EngineDialect
    connectors: Mapping[str, KvHandoff]
    identity: EngineIdentityReader
    kv_events: KvEventDecoder
    metrics: EngineMetrics
    fabric: FabricLifecycle
    launcher: Callable[[], EngineLauncher] = field(repr=False)
    role_switch: RoleSwitcher | None = None

    def connector(self, name: str) -> KvHandoff:
        """Return the named KV connector, or raise ValueError when this backend lacks it."""
        try:
            return self.connectors[name]
        except KeyError:
            raise ValueError(
                f"backend {self.name!r} has no connector {name!r}: "
                f"known ones are {', '.join(sorted(self.connectors))}"
            ) from None


@functools.cache
def _entry_points() -> dict[str, EntryPoint]:
    return {point.name: point for point in entry_points(group=GROUP)}


def names() -> list[str]:
    """Return the registered backend names."""
    return sorted(_entry_points())


@functools.cache
def load(name: str) -> EngineBackend:
    """Build the registered backend named by the fleet config."""
    point = _entry_points().get(name)
    if point is None:
        known = ", ".join(names()) or "none; reinstall the package to register them"
        raise ValueError(f"unknown engine backend {name!r}: known ones are {known}")
    backend = point.load()()
    if not isinstance(backend, EngineBackend) or backend.name != name:
        raise ValueError(f"entry point {name!r} does not build the {name!r} EngineBackend")
    return backend
