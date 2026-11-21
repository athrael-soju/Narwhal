from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from ...config import EngineContract
from ...engines.attestation import (
    EngineIdentity,
    EngineIdentityReader,
    attested_launch,
    attested_option,
)

VERSION_PATH = "/version"
SERVER_INFO_PATH = "/server_info"
# The engine's own default, in milliseconds, when its configuration sets no transfer timeout.
DEFAULT_TRANSFER_TIMEOUT_MS = 60000


def transfer_timeout_ms(config: str) -> int | None:
    """The `cache_transceiver_config.kv_transfer_timeout_ms` a launch configuration sets."""
    # The launcher writes its configuration as JSON, which YAML reads unchanged.
    try:
        document = json.loads(config)
    except ValueError:
        return None
    transceiver = document.get("cache_transceiver_config") if isinstance(document, dict) else None
    if not isinstance(transceiver, dict):
        return None
    if "kv_transfer_timeout_ms" not in transceiver:
        return DEFAULT_TRANSFER_TIMEOUT_MS
    timeout = transceiver["kv_transfer_timeout_ms"]
    return timeout if type(timeout) is int and timeout > 0 else None


class TrtllmIdentity(EngineIdentityReader):
    reports_process_start = False
    # The cache manager, handshake and attention fields have no TensorRT-LLM counterpart.
    contract_fields = frozenset(EngineContract().fields()) - {
        "connector_version",
        "attention_backend",
        "cross_layers_blocks",
        "hybrid_kv_cache_manager",
        "kv_role",
        "transfer_mode",
        "enforce_handshake_compat",
    }
    sequence_flag = "--max_batch_size"

    async def version(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> str:
        response = await client.get(f"{base.rstrip('/')}{VERSION_PATH}", headers=headers)
        response.raise_for_status()
        payload = response.json()
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{VERSION_PATH} returned no version")
        return version.strip()

    async def read(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: Mapping[str, str] | None = None,
        *,
        process_start: float | None = None,
    ) -> EngineIdentity:
        if process_start is None:
            raise ValueError(
                "TensorRT-LLM publishes no process start; its sidecar reads it on the host"
            )
        return EngineIdentity(await self.version(client, base, headers), process_start)

    def kv_lease(self, attestation: Any) -> int | None:
        # The context worker frees a handoff's KV once its transfer timeout passes unclaimed.
        files = attested_launch(attestation).get("config_files")
        if not isinstance(files, dict) or not files:
            return None
        name = attested_option(attestation, "--config")
        if name:
            config = files.get(name.rpartition("/")[2])
        else:
            # A per-launch path leaves the flag without a value; the launch's one file is it.
            config = next(iter(files.values())) if len(files) == 1 else None
        timeout = transfer_timeout_ms(config) if isinstance(config, str) else None
        return None if timeout is None or timeout < 2000 else timeout // 1000
