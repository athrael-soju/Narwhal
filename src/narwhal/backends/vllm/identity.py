from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

import httpx

from ...engines.attestation import EngineIdentity, EngineIdentityReader, attested_option

_PROCESS_START = re.compile(
    r"^process_start_time_seconds(?:\{[^}]*\})?\s+([0-9.eE+-]+)(?:\s|$)", re.MULTILINE
)


def parse_process_start(metrics: str) -> float:
    match = _PROCESS_START.search(metrics)
    if match is None:
        raise ValueError("/metrics has no process_start_time_seconds")
    value = float(match.group(1))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("process_start_time_seconds must be positive and finite")
    return value


class VllmIdentity(EngineIdentityReader):
    sequence_flag = "--max-num-seqs"

    async def version(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> str:
        response = await client.get(f"{base.rstrip('/')}/version", headers=headers)
        response.raise_for_status()
        version = response.json().get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("/version returned no version")
        return version.strip()

    async def read(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: Mapping[str, str] | None = None,
        *,
        process_start: float | None = None,
    ) -> EngineIdentity:
        version = await self.version(client, base, headers)
        metrics_response = await client.get(f"{base.rstrip('/')}/metrics", headers=headers)
        metrics_response.raise_for_status()
        return EngineIdentity(version, parse_process_start(metrics_response.text))

    def kv_lease(self, attestation: Any) -> int | None:
        value = attested_option(attestation, "--kv-transfer-config")
        if value is None:
            return None
        try:
            config = json.loads(value)
        except ValueError:
            return None
        if not isinstance(config, dict):
            return None
        extra = config.get("kv_connector_extra_config")
        lease = extra.get("kv_lease_duration") if isinstance(extra, dict) else None
        if config.get("kv_connector") != "NixlConnector" or type(lease) is not int or lease < 1:
            return None
        return lease
