from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ...backends import load as load_backend
from ...config import EngineSpec, FleetConfig
from ...engines.attestation import EngineIdentity, fetch_engine_identity, verify_attestation
from ...engines.client import EngineError
from ...engines.connector import RendezvousConnector
from ...engines.stream import sse_token_count
from ...engines.validation import recovery_pairs, validation_pairs
from ...profiling.generation import binding_digest, profile_generation_problems
from ...types import Role
from .records import LifecycleError, ValidationOutcome

if TYPE_CHECKING:
    from ...serving.router.routing import NarwhalRouter


def _single_pairs(target: EngineSpec, peers: list[EngineSpec]) -> list[tuple[str, str]]:
    try:
        return recovery_pairs(target, peers)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


async def validate_readmission(
    router: NarwhalRouter,
    engines: list[str],
    *,
    wave: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ValidationOutcome:
    outcome = ValidationOutcome()
    cfg: FleetConfig = router.cfg
    contract = cfg.engine_contract
    if cfg.engine_restart_policy == "whole_wave":
        if not wave or set(engines) != set(router.monitor.instances):
            for iid in engines:
                outcome.fail(iid, "engine_restart_policy requires whole-wave readmission")
            return outcome
        if any(
            not router.lifecycle.records[iid].restart_required
            or router.lifecycle.records[iid].old_process_start is None
            for iid in engines
        ):
            for iid in engines:
                outcome.fail(iid, "whole-wave recovery requires recorded pre-restart identities")
            return outcome
    if contract is None or cfg.contract_missing():
        for iid in engines:
            outcome.fail(iid, "readmission requires a complete engine_contract")
        return outcome

    by_id = {spec.iid: spec for spec in cfg.engines}
    targets = [by_id[iid] for iid in engines]
    if wave:
        participants = list(cfg.engines)
        pairs = validation_pairs(participants)
    else:
        peers = [
            by_id[instance.iid]
            for instance in router.scheduler.live_instances(exclude=set(engines))
        ]
        try:
            pairs = _single_pairs(targets[0], peers)
        except LifecycleError as exc:
            outcome.fail(targets[0].iid, str(exc))
            return outcome
        peer_ids = {iid for pair in pairs for iid in pair} - set(engines)
        participants = targets + [by_id[iid] for iid in sorted(peer_ids)]

    participant_ids = {spec.iid for spec in participants}
    if len(cfg.engines) == 1:
        outcome.ok(engines[0], "fabric not applicable: single-engine fleet")
    elif len(participant_ids) < 2 or not pairs:
        for iid in engines:
            outcome.fail(iid, "fabric validation requires an eligible peer")
        return outcome

    timeout = cfg.health_timeout_s
    identities: dict[str, EngineIdentity] = {}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for spec in participants:
            if not await router.engines.healthy(spec.url):
                outcome.fail(spec.iid, "health did not answer 200")
                continue
            outcome.ok(spec.iid, "health")
            try:
                identity = await fetch_engine_identity(
                    spec.url,
                    timeout_s=timeout,
                    transport=transport,
                    headers=router.engines._auth(None),
                    reader=load_backend(cfg.backend).identity,
                    attestation_url=spec.attestation_url,
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"process identity unreadable: {type(exc).__name__}")
                continue
            identities[spec.iid] = identity
            previous = router.lifecycle.process_starts.get(spec.iid)
            if (
                spec.iid not in engines
                and previous is not None
                and (identity.process_start_time_seconds != previous)
            ):
                router.scheduler.eject(spec.iid, "process_identity")
                outcome.fail(spec.iid, "peer process changed before readmission")
            if not spec.attestation_url:
                outcome.fail(spec.iid, "attestation_url is not configured")
                continue
            try:
                response = await client.get(spec.attestation_url)
                response.raise_for_status()
                payload = response.json()
                failures = verify_attestation(payload, contract, identity)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"attestation unreadable: {type(exc).__name__}")
                continue
            if failures:
                outcome.fail(spec.iid, "attestation: " + "; ".join(failures))
            else:
                outcome.ok(spec.iid, f"attestation {contract.fingerprint()}")
                router.attested(spec.iid, payload)
                if spec.iid in engines and (
                    switched := await router.switch_role(
                        spec.iid, router.monitor.instances[spec.iid].role
                    )
                ):
                    outcome.fail(spec.iid, f"role switch failed: {switched}")
                problems = profile_generation_problems(
                    router.profiles, spec.iid, binding_digest(payload)
                )
                for problem in problems:
                    outcome.fail(spec.iid, problem)
                if not problems:
                    outcome.ok(spec.iid, "profile generation")
            try:
                models = await client.get(
                    f"{spec.url}/v1/models", headers=router.engines._auth(None)
                )
                models.raise_for_status()
                names = [entry["id"] for entry in models.json().get("data", [])]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"model list unreadable: {type(exc).__name__}")
            else:
                if cfg.model not in names:
                    outcome.fail(spec.iid, f"serves {names}, expected {cfg.model}")
                else:
                    outcome.ok(spec.iid, "model")

        for spec in targets:
            target_identity = identities.get(spec.iid)
            record = router.lifecycle.records[spec.iid]
            if target_identity is None:
                continue
            if record.restart_required and target_identity.process_start_time_seconds <= float(
                record.old_process_start or 0.0
            ):
                outcome.fail(spec.iid, "engine process did not restart after drain")
                continue
            outcome.starts[spec.iid] = target_identity.process_start_time_seconds
            outcome.ok(
                spec.iid,
                "new process identity" if record.restart_required else "process identity",
            )
            if isinstance(router.engines.kv, RendezvousConnector):
                # A rendezvous engine serves only paired legs; the fabric pairs generate.
                continue
            try:
                response = await client.post(
                    f"{spec.url}/v1/completions",
                    headers=router.engines._auth(None),
                    json={
                        "model": cfg.model,
                        "prompt": "narwhal lifecycle generation check",
                        "max_tokens": 1,
                        "temperature": 0.0,
                        "stream": False,
                    },
                    timeout=cfg.prefill_timeout_s,
                )
                response.raise_for_status()
                choices = response.json().get("choices")
                if not isinstance(choices, list) or not choices:
                    raise ValueError("completion has no choices")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"generation failed: {type(exc).__name__}")
            else:
                outcome.ok(spec.iid, "generation")

    if outcome.failures:
        return outcome

    async def identities_unchanged() -> bool:
        identity_failed = False
        for spec in participants:
            try:
                live = await fetch_engine_identity(
                    spec.url,
                    timeout_s=timeout,
                    transport=transport,
                    headers=router.engines._auth(None),
                    reader=load_backend(cfg.backend).identity,
                    attestation_url=spec.attestation_url,
                )
                if live != identities[spec.iid]:
                    raise ValueError("process changed during validation")
                async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                    response = await client.get(spec.attestation_url)
                    response.raise_for_status()
                payload = response.json()
                if verify_attestation(payload, contract, live):
                    raise ValueError("attestation changed during validation")
                problems = profile_generation_problems(
                    router.profiles, spec.iid, binding_digest(payload)
                )
                for problem in problems:
                    outcome.fail(spec.iid, problem)
                if problems:
                    router.scheduler.eject(spec.iid, "profile_generation")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                identity_failed = True
                outcome.fail(
                    spec.iid,
                    f"identity changed or unavailable during validation: {type(exc).__name__}",
                )
                router.scheduler.eject(spec.iid, "process_identity")
        if identity_failed and cfg.engine_restart_policy == "whole_wave":
            router.lifecycle.require_restart_wave(
                "process identity changed during validation", reset=True
            )
        return not outcome.failures

    body = {
        "model": cfg.model,
        "prompt": "narwhal lifecycle fabric check",
        "max_tokens": 2,
        "temperature": 0.0,
        **router.engines.dialect.decode_probe_extras(2),
    }
    if router.scheduler.availability.roles_bound:
        # Each engine serves only its live role, so a pair runs from prefill to decode.
        roles = {iid: inst.role for iid, inst in router.monitor.instances.items()}
        pairs = [
            (source, target)
            for source, target in pairs
            if roles.get(source) is Role.PREFILL and roles.get(target) is Role.DECODE
        ]
        if not pairs:
            for iid in engines:
                outcome.fail(iid, "fabric validation requires a peer in the opposite role")
            return outcome
    for source, target in pairs:
        if not await identities_unchanged():
            return outcome
        try:
            handoff = await router.engines.start_handoff(
                by_id[source].url,
                "/v1/completions",
                body,
                {},
                producer=router.launches.get(source),
            )
            if not await identities_unchanged():
                return outcome
            tokens = 0
            async for batch in router.engines.decode_handoff(
                handoff,
                by_id[target].url,
                "/v1/completions",
                body,
                {},
                first_token_timeout_s=cfg.first_token_timeout_s,
            ):
                tokens += sum(sse_token_count(event) for event in batch)
            if tokens < 1:
                raise EngineError("decode", by_id[target].url, 502, "no tokens")
        except Exception as exc:
            detail = f"fabric {source}->{target}: {type(exc).__name__}: {exc}"
            outcome.fail(source, detail)
            outcome.fail(target, detail)
        else:
            outcome.ok(source, f"fabric produce to {target}")
            outcome.ok(target, f"fabric consume from {source}")

    if outcome.failures:
        return outcome
    for spec in targets:
        if not await router.engines.healthy(spec.url):
            outcome.fail(spec.iid, "final health failed after fabric validation")
        else:
            outcome.ok(spec.iid, "final health")
    await identities_unchanged()
    return outcome
