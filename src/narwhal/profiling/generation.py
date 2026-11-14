from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from ..config.model import EngineSpec, FleetConfig
from ..contracts import canonical_digest
from ..engines.attestation import EngineIdentity, fetch_spec_identity, verify_attestation

if TYPE_CHECKING:
    from .model import Profile
    from .store import ProfileStore


@dataclass(frozen=True)
class GenerationEvidence:
    digest: str
    document: dict[str, Any]
    process_start_time_seconds: float
    process_digest: str = ""

    def __post_init__(self) -> None:
        if not self.process_digest:
            object.__setattr__(self, "process_digest", self.digest)


def identity_generation(identity: EngineIdentity) -> GenerationEvidence:
    document: dict[str, Any] = {
        "engine": {
            "version": identity.version,
            "process_start_time_seconds": identity.process_start_time_seconds,
        }
    }
    return GenerationEvidence(
        canonical_digest(document), document, identity.process_start_time_seconds
    )


async def read_generation(
    cfg: FleetConfig,
    spec: EngineSpec,
    *,
    timeout_s: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GenerationEvidence:
    contract = cfg.engine_contract
    if timeout_s is None:
        timeout_s = cfg.health_timeout_s
    identity = await fetch_spec_identity(cfg, spec, timeout_s=timeout_s, transport=transport)
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
    return GenerationEvidence(
        binding_digest(payload),
        payload,
        identity.process_start_time_seconds,
        payload["attestation_digest"],
    )


def binding_digest(payload: dict[str, Any]) -> str:
    return str(payload.get("launch_digest") or payload["attestation_digest"])


def generation_problem(iid: str, saved: str | None, live: str) -> str | None:
    if saved is None:
        return f"{iid} profile has no generation evidence; reprofile before admission"
    if saved != live:
        return f"{iid} profile generation differs from the live engine; reprofile before admission"
    return None


def bound_profiles(store: ProfileStore, iid: str) -> tuple[Profile, ...]:
    """Return the engine's profiles that a generation check applies to."""
    return tuple(profile for profile in store.profiles_for_engine(iid) if not profile.nominal)


def nominal_problem(store: ProfileStore, iid: str) -> str | None:
    """A fleet with an engine contract admits measured profiles only."""
    if any(profile.nominal for profile in store.profiles_for_engine(iid)):
        return f"{iid} has a nominal profile; engine_contract requires a measured one"
    return None


def profile_generation_problems(store: ProfileStore, iid: str, live: str) -> list[str]:
    if not store.profiles_for_engine(iid):
        return [f"{iid} has no loaded profile; reprofile before admission"]
    return list(
        dict.fromkeys(
            problem
            for profile in bound_profiles(store, iid)
            if (problem := generation_problem(iid, profile.generation_digest, live)) is not None
        )
    )
