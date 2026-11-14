from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from ...runtime.fabric import FabricLifecycle
from ...runtime.role_switch import RouterRoleSwitch
from .. import EngineBackend
from .connector import SlotFileConnector
from .dialect import LlamacppDialect
from .identity import LlamacppIdentity
from .kv_events import NoKvEvents
from .metrics import LlamacppMetrics

if TYPE_CHECKING:
    from ...deployment.direct_start import DirectStart


def _direct() -> DirectStart:
    from .dev import LlamacppDirect

    return LlamacppDirect()


def backend() -> EngineBackend:
    return EngineBackend(
        name="llamacpp",
        label="llama.cpp",
        dialect=LlamacppDialect(),
        connectors={"slot-file": SlotFileConnector()},
        default_connector="slot-file",
        identity=LlamacppIdentity(),
        kv_events=NoKvEvents(),
        metrics=LlamacppMetrics(),
        fabric=FabricLifecycle(),
        launcher=None,
        # One server process serves both roles, so the router alone holds each engine's role.
        role_switch=RouterRoleSwitch(),
        icon=resources.files(__name__).joinpath("icon.png").read_bytes(),
        direct=_direct,
    )
