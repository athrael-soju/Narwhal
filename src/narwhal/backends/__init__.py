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
DEFAULT_BACKEND = "vllm"


@dataclass(frozen=True)
class EngineBackend:
    name: str
    # The engine's name in operator output, such as "vLLM".
    label: str
    dialect: EngineDialect
    connectors: Mapping[str, KvHandoff]
    default_connector: str
    identity: EngineIdentityReader
    kv_events: KvEventDecoder
    metrics: EngineMetrics
    fabric: FabricLifecycle
    launcher: Callable[[], EngineLauncher] = field(repr=False)
    role_switch: RoleSwitcher | None = None
    # A small PNG of the engine's logo for operator views.
    icon: bytes = field(default=b"", repr=False)
    # Earlier field names this backend wrote, by section ("contract", "engine").
    renamed_fields: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def role_switcher(self, connector: str) -> RoleSwitcher | None:
        switcher = self.role_switch
        if switcher is None or (
            switcher.connectors is not None and connector not in switcher.connectors
        ):
            return None
        return switcher

    def connector(self, name: str) -> KvHandoff:
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
    return sorted(_entry_points())


@functools.cache
def load(name: str) -> EngineBackend:
    point = _entry_points().get(name)
    if point is None:
        known = ", ".join(names()) or "none; reinstall the package to register them"
        raise ValueError(f"unknown engine backend {name!r}: known ones are {known}")
    backend = point.load()()
    if not isinstance(backend, EngineBackend) or backend.name != name:
        raise ValueError(f"entry point {name!r} does not build the {name!r} EngineBackend")
    return backend


def renamed_fields(section: str) -> dict[str, str]:
    return {
        old: new
        for name in names()
        for old, new in load(name).renamed_fields.get(section, {}).items()
    }
