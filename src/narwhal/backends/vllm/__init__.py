from __future__ import annotations

from typing import TYPE_CHECKING

from ...runtime import listeners
from ...runtime.fabric import FabricLifecycle
from ...runtime.role_switch import RouterRoleSwitch
from .. import EngineBackend
from .connector import NixlConnector
from .dialect import VllmDialect
from .identity import VllmIdentity
from .kv_events import VllmKvEvents
from .metrics import VllmMetrics

if TYPE_CHECKING:
    from ...deployment.launch_engine.backend import EngineLauncher


class VllmFabric(FabricLifecycle):
    # The first round follows the launcher's 60 s engine_ttl; the last follows vLLM's 3600 s
    # default. A NIXL consumer keeps a producer's KV mapped until a consume request finds that
    # producer idle past its engine_ttl.
    release_after_s = (65.0, 125.0, 245.0, 485.0, 965.0, 1925.0, 3845.0)
    # vLLM sends lease heartbeats at most every 5 s.
    release_retry_s = 5.0

    def check_engine_bind(self, host: str, port: int, *, fabric: bool = False) -> None:
        # NIXL's ZeroMQ listener enables IPv6 dual-stack.
        listeners.check_engine_bind(host, port, dual_stack=fabric)


def _launcher() -> EngineLauncher:
    from .launch import VllmLauncher

    return VllmLauncher()


def backend() -> EngineBackend:
    return EngineBackend(
        name="vllm",
        dialect=VllmDialect(),
        connectors={"nixl": NixlConnector()},
        identity=VllmIdentity(),
        kv_events=VllmKvEvents(),
        metrics=VllmMetrics(),
        fabric=VllmFabric(),
        launcher=_launcher,
        role_switch=RouterRoleSwitch(),
    )
