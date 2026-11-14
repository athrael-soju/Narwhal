from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ...engines.attestation import EngineIdentity, EngineIdentityReader

PROPS_PATH = "/props"


def context_tokens(props: Any) -> int | None:
    """The context one slot serves: the server divides its context among its slots."""
    settings = props.get("default_generation_settings") if isinstance(props, dict) else None
    limit = settings.get("n_ctx") if isinstance(settings, dict) else None
    return limit if type(limit) is int and limit > 0 else None


def slot_count(props: Any) -> int | None:
    slots = props.get("total_slots") if isinstance(props, dict) else None
    return slots if type(slots) is int and slots > 0 else None


class LlamacppIdentity(EngineIdentityReader):
    reports_process_start = False
    contract_fields = frozenset()

    async def version(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> str:
        response = await client.get(f"{base.rstrip('/')}{PROPS_PATH}", headers=headers)
        response.raise_for_status()
        props = response.json()
        build = props.get("build_info") if isinstance(props, dict) else None
        if not isinstance(build, str) or not build.strip():
            raise ValueError(f"{PROPS_PATH} returned no build_info")
        return build.strip()

    async def read(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: Mapping[str, str] | None = None,
        *,
        process_start: float | None = None,
    ) -> EngineIdentity:
        if process_start is None:
            raise ValueError("llama.cpp publishes no process start; read it on the engine host")
        return EngineIdentity(await self.version(client, base, headers), process_start)

    def kv_lease(self, attestation: Any) -> int | None:
        # A saved slot file stays until the connector overwrites it.
        return None
