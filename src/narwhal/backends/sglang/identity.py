from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

import httpx

from ...engines.attestation import EngineIdentity, EngineIdentityReader

# SGLang publishes no process start time. It measures its startup phases once per launch, so
# their sum tells one engine process from the next.
_STARTUP = re.compile(
    r"^sglang:startup_time_seconds(?:\{[^}]*\})?\s+([0-9.eE+-]+)(?:\s|$)", re.MULTILINE
)


def parse_generation(metrics: str) -> float:
    values = [float(value) for value in _STARTUP.findall(metrics)]
    if not values:
        raise ValueError("/metrics has no sglang:startup_time_seconds")
    total = math.fsum(values)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("sglang:startup_time_seconds must be positive and finite")
    return total


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
    async def read(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> EngineIdentity:
        base = base.rstrip("/")
        # /server_info also carries the engine's API keys; only the version leaves this call.
        info = await client.get(f"{base}/server_info", headers=headers)
        info.raise_for_status()
        version = server_version(info.json())
        metrics = await client.get(f"{base}/metrics", headers=headers)
        metrics.raise_for_status()
        return EngineIdentity(version, parse_generation(metrics.text))

    def sequence_limit(self, attestation: Any) -> int | None:
        value = launch_option(attestation, "--max-running-requests")
        if value is not None and value.isdigit() and int(value) > 0:
            return int(value)
        return None

    def kv_lease(self, attestation: Any) -> int | None:
        # SGLang holds a producer's KV for its rendezvous, not for a lease.
        return None
