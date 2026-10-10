from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ...config import EngineContract
from ...engines.attestation import EngineIdentity, EngineIdentityReader

# vLLM's connector, cache manager and handshake fields have no SGLang counterpart.
ATTESTED_FIELDS = frozenset(EngineContract().fields()) - {
    "connector_version",
    "cross_layers_blocks",
    "hybrid_kv_cache_manager",
    "kv_role",
    "transfer_mode",
    "enforce_handshake_compat",
}


def server_version(payload: Any) -> str:
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("/server_info returned no version")
    return version.strip()


def launch_option(payload: Any, name: str) -> str | None:
    launch = payload.get("launch") if isinstance(payload, dict) else None
    args = launch.get("args") if isinstance(launch, dict) else None
    if not isinstance(args, list) or name not in args:
        return None
    index = args.index(name)
    value = args[index + 1] if index + 1 < len(args) else None
    return value if isinstance(value, str) else None


class SglangIdentity(EngineIdentityReader):
    reports_process_start = False
    contract_fields = ATTESTED_FIELDS

    async def version(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> str:
        # /server_info also carries the engine's API keys; only the version leaves this call.
        info = await client.get(f"{base.rstrip('/')}/server_info", headers=headers)
        info.raise_for_status()
        return server_version(info.json())

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

    def sequence_limit(self, attestation: Any) -> int | None:
        value = launch_option(attestation, "--max-running-requests")
        if value is not None and value.isdigit() and int(value) > 0:
            return int(value)
        return None

    def kv_lease(self, attestation: Any) -> int | None:
        # SGLang holds a producer's KV for its rendezvous, not for a lease.
        return None
