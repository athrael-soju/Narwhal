from __future__ import annotations

from importlib import resources

from ...runtime.fabric import FabricLifecycle
from ...runtime.role_switch import RouterRoleSwitch
from .. import EngineBackend
from .connector import TransceiverConnector
from .dialect import TrtllmDialect
from .identity import TrtllmIdentity
from .kv_events import NoKvEvents
from .metrics import TrtllmMetrics


def backend() -> EngineBackend:
    return EngineBackend(
        name="trtllm",
        label="TensorRT-LLM",
        dialect=TrtllmDialect(),
        connectors={
            "ucx": TransceiverConnector("ucx", "UCX"),
            "nixl": TransceiverConnector("nixl", "NIXL"),
        },
        default_connector="ucx",
        identity=TrtllmIdentity(),
        kv_events=NoKvEvents(),
        metrics=TrtllmMetrics(),
        fabric=FabricLifecycle(),
        launcher=None,
        # A worker launched with the context settings serves both leg types, so the router
        # alone holds each engine's role.
        role_switch=RouterRoleSwitch(),
        icon=resources.files(__name__).joinpath("icon.png").read_bytes(),
    )
