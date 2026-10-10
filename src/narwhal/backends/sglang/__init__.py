from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from ...runtime import listeners
from ...runtime.fabric import FabricLifecycle
from .. import EngineBackend
from .connector import MooncakeConnector, SglangNixlConnector
from .dialect import SglangDialect
from .identity import SglangIdentity
from .kv_events import SglangKvEvents
from .metrics import SglangMetrics
from .role_switch import SglangRoleSwitch

if TYPE_CHECKING:
    from ...deployment.launch_engine.backend import EngineLauncher


class SglangFabric(FabricLifecycle):
    # A consumer holds a producer's KV only for one rendezvous, so no peer needs a release.
    release_after_s = ()
    release_retry_s = 0.0

    def check_engine_bind(self, host: str, port: int, *, fabric: bool = False) -> None:
        listeners.check_engine_bind(host, port, dual_stack=False)


def _launcher() -> EngineLauncher:
    from .launch import SglangLauncher

    return SglangLauncher()


def backend() -> EngineBackend:
    return EngineBackend(
        name="sglang",
        label="SGLang",
        dialect=SglangDialect(),
        connectors={"mooncake": MooncakeConnector(), "nixl": SglangNixlConnector()},
        default_connector="mooncake",
        identity=SglangIdentity(),
        kv_events=SglangKvEvents(),
        metrics=SglangMetrics(),
        fabric=SglangFabric(),
        launcher=_launcher,
        role_switch=SglangRoleSwitch(),
        icon=resources.files(__name__).joinpath("icon.png").read_bytes(),
    )
