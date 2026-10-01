"""Bind measured profiles to the engine process and attested runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from ..config.model import EngineContract, EngineSpec
from ..contracts import canonical_digest
from ..engines.attestation import EngineIdentity, fetch_engine_identity, verify_attestation

if TYPE_CHECKING:
    from .store import ProfileStore


@dataclass(frozen=True)
class GenerationEvidence:
    """Profile-binding digest, its evidence, and the engine-process digest.

    `digest` is the attested launch digest when the sidecar reports one, otherwise
    `process_digest`.
    """

    digest: str
    document: dict[str, Any]
    process_digest: str = ""

    def __post_init__(self) -> None:
        if not self.process_digest:
            object.__setattr__(self, "process_digest", self.digest)


def identity_generation(identity: EngineIdentity) -> GenerationEvidence:
    """Derive the profile binding for a fleet without an attestation contract."""
    document: dict[str, Any] = {
        "engine": {
            "vllm_version": identity.vllm_version,
            "process_start_time_seconds": identity.process_start_time_seconds,
        }
    }
    return GenerationEvidence(canonical_digest(document), document)


async def read_generation(
    spec: EngineSpec,
    contract: EngineContract | None,
    *,
    timeout_s: float,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GenerationEvidence:
    """Return the engine's profile generation, verifying its sidecar when `contract` is set."""
    identity = await fetch_engine_identity(
        spec.url, timeout_s=timeout_s, headers=headers, transport=transport
    )
    if contract is None:
        return identity_generation(identity)
    if not spec.attestation_url:
        raise ValueError(f"{spec.iid}: attestation_url is required for profile generation")
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        response = await client.get(spec.attestation_url)
        response.raise_for_status()
        payload = response.json()
    failures = verify_attestation(payload, contract, identity)
    if failures:
        raise ValueError(f"{spec.iid}: attestation: {'; '.join(failures)}")
    return GenerationEvidence(binding_digest(payload), payload, payload["attestation_digest"])


def binding_digest(payload: dict[str, Any]) -> str:
    """Return the digest profiles bind to in a verified attestation response."""
    return str(payload.get("launch_digest") or payload["attestation_digest"])


def generation_problem(iid: str, saved: str | None, live: str) -> str | None:
    """Return a reprofile message when `saved` is absent or differs from `live`, else None."""
    if saved is None:
        return f"{iid} profile has no generation evidence; reprofile before admission"
    if saved != live:
        return f"{iid} profile generation differs from the live engine; reprofile before admission"
    return None


def profile_generation_problems(store: ProfileStore, iid: str, live: str) -> list[str]:
    """Return reprofile messages for every loaded variant of engine `iid`."""
    profiles = store.profiles_for_engine(iid)
    if not profiles:
        return [f"{iid} has no loaded profile; reprofile before admission"]
    return list(
        dict.fromkeys(
            problem
            for profile in profiles
            if (problem := generation_problem(iid, profile.generation_digest, live)) is not None
        )
    )
