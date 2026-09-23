"""Bind measured profiles to the engine process and attested runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import httpx

from ..config.model import EngineContract, EngineSpec
from ..engines.attestation import fetch_engine_identity, verify_attestation


@dataclass(frozen=True)
class GenerationEvidence:
    """Generation digest and the process evidence used to derive it."""

    digest: str
    document: dict[str, Any]


async def read_generation(
    spec: EngineSpec,
    contract: EngineContract | None,
    *,
    timeout_s: float,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GenerationEvidence:
    """Read a process identity and verify its sidecar before returning a generation."""
    identity = await fetch_engine_identity(
        spec.url, timeout_s=timeout_s, headers=headers, transport=transport
    )
    if contract is None:
        document: dict[str, Any] = {
            "engine": {
                "vllm_version": identity.vllm_version,
                "process_start_time_seconds": identity.process_start_time_seconds,
            }
        }
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return GenerationEvidence("sha256:" + sha256(raw).hexdigest(), document)
    if not spec.attestation_url:
        raise ValueError(f"{spec.iid}: attestation_url is required for profile generation")
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        response = await client.get(spec.attestation_url)
        response.raise_for_status()
        payload = response.json()
    failures = verify_attestation(payload, contract, identity)
    if failures:
        raise ValueError(f"{spec.iid}: attestation: {'; '.join(failures)}")
    return GenerationEvidence(payload["attestation_digest"], payload)


def generation_problem(iid: str, saved: str | None, live: str) -> str | None:
    """Name the engine whose saved measurements require a fresh profiling run."""
    if saved is None:
        return f"{iid} profile has no generation evidence; reprofile before admission"
    if saved != live:
        return f"{iid} profile generation differs from the live engine; reprofile before admission"
    return None
