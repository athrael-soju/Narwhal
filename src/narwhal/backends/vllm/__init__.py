from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from ...engines import kv_events
from ...engines.attestation import (
    EngineIdentity,
    EngineIdentityReader,
    attested_kv_lease,
    attested_sequence_limit,
    read_identity,
)
from ...engines.connector import NixlConnector
from ...engines.dialect import VllmDialect
from ...engines.kv_events import CacheEvent, KvEventDecoder
from ...runtime import listeners, release
from ...runtime.fabric import FabricLifecycle
from ...runtime.role_switch import RouterRoleSwitch
from .. import EngineBackend
from .metrics import VllmMetrics

if TYPE_CHECKING:
    from ...deployment.launch_engine.backend import EngineLauncher


class VllmIdentity(EngineIdentityReader):
    async def read(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> EngineIdentity:
        return await read_identity(client, base, headers)

    def sequence_limit(self, attestation: Any) -> int | None:
        return attested_sequence_limit(attestation)

    def kv_lease(self, attestation: Any) -> int | None:
        return attested_kv_lease(attestation)


class VllmKvEvents(KvEventDecoder):
    def decode_batch(self, payload: bytes) -> list[CacheEvent | None]:
        return kv_events.decode_batch(payload)


class VllmFabric(FabricLifecycle):
    release_after_s: ClassVar[tuple[float, ...]] = release.RELEASE_AFTER_S
    release_retry_s: ClassVar[float] = release.RETRY_AFTER_S

    def check_engine_bind(self, host: str, port: int, *, fabric: bool = False) -> None:
        listeners.check_engine_bind(host, port, nixl=fabric)


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
