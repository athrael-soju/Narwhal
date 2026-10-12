from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ...config import EngineContract
from ...engines.attestation import EngineIdentity, EngineIdentityReader

SERVER_INFO_PATH = "/server_info"
# vLLM's connector, cache manager and handshake fields have no SGLang counterpart.
ATTESTED_FIELDS = frozenset(EngineContract().fields()) - {
    "connector_version",
    "cross_layers_blocks",
    "hybrid_kv_cache_manager",
    "kv_role",
    "transfer_mode",
    "enforce_handshake_compat",
}


async def server_info(
    client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
) -> Any:
    info = await client.get(f"{base.rstrip('/')}{SERVER_INFO_PATH}", headers=headers)
    info.raise_for_status()
    return info.json()


def server_version(payload: Any) -> str:
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("/server_info returned no version")
    return version.strip()


class SglangIdentity(EngineIdentityReader):
    reports_process_start = False
    contract_fields = ATTESTED_FIELDS
    sequence_flag = "--max-running-requests"

    async def version(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> str:
        # /server_info also carries the engine's API keys; only the version leaves this call.
        return server_version(await server_info(client, base, headers))

    async def read(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: Mapping[str, str] | None = None,
        *,
        process_start: float | None = None,
    ) -> EngineIdentity:
        if process_start is None:
            raise ValueError("SGLang publishes no process start; its sidecar reads it on the host")
        return EngineIdentity(await self.version(client, base, headers), process_start)

    def kv_lease(self, attestation: Any) -> int | None:
        # SGLang holds a producer's KV for its rendezvous, not for a lease.
        return None
