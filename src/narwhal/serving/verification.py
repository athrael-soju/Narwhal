"""Engine failure classification and suspect verification."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING

import httpx

from ..engines.client import EngineError, InferenceProbe, first_output_timeout, leg_failure_class
from ..types import LEG_OVERLOAD, LEG_STREAM

if TYPE_CHECKING:
    from .router import NarwhalRouter

log = logging.getLogger("narwhal.server")


def leg_failed(
    router: NarwhalRouter,
    iid: str,
    exc: BaseException,
    *,
    prefill_iid: str | None = None,
    decode_leg: bool = False,
    progressed: bool = False,
) -> None:
    """Classify a failed leg and update breaker state.

    A 4xx outside 408/429 counts as transport evidence; a local pool timeout counts as none.
    """
    if isinstance(exc, httpx.PoolTimeout):
        log.info("%s leg waited out the local HTTP pool; no breaker evidence", iid)
        return
    status = exc.status if isinstance(exc, EngineError) else 500
    if 400 <= status < 500 and status not in (408, 429):
        router.scheduler.record_answer(iid, "transport")
        return
    # Decode read timeouts, including those before headers, need inference verification.
    klass = (
        "stream" if decode_leg and isinstance(exc, httpx.ReadTimeout) else leg_failure_class(exc)
    )
    if klass is None:
        return
    if klass == LEG_STREAM and progressed and first_output_timeout(exc):
        # A first-token timeout while the engine produced other output is overload.
        klass = LEG_OVERLOAD
    if klass == "stream":
        router._inference_sources.setdefault(iid, set()).add(prefill_iid or "")
    verdict = router.scheduler.record_failure(iid, klass)
    if verdict == "eject":
        log.warning(
            "ejected %s after %d consecutive %s failures; it takes no "
            "dispatch until readmission succeeds",
            iid,
            router.scheduler.availability.eject_after,
            klass,
        )
    elif verdict in ("verify_health", "verify_inference"):
        if verdict == "verify_inference":
            router.scheduler.inference_suspects.add(iid)
            if not router.scheduler.quarantine(iid, math.inf):
                log.warning(
                    "%s stays live during inference verification: it alone serves its role", iid
                )
        # One pending probe per engine and verdict kind; extra failures
        # while it runs only grow the streak it will resolve.
        key = (iid, verdict)
        if key not in router.scheduler.verifying:
            router.scheduler.verifying.add(key)
            start_verification(router, iid, verdict)


def start_verification(router: NarwhalRouter, iid: str, kind: str) -> None:
    """Run one verification of a suspect engine in the background."""
    task = asyncio.create_task(verify_suspect(router, iid, kind))
    router._verification_tasks.add(task)
    task.add_done_callback(router._verification_tasks.discard)


async def verify_suspect(router: NarwhalRouter, iid: str, kind: str) -> None:
    """Resolve a suspect engine with a health or inference verification."""
    try:
        inst = router.monitor.instances.get(iid)
        if inst is None:
            return
        if kind == "verify_inference":
            await verify_inference(router, iid, inst.url)
        else:
            await verify_health(router, iid, inst.url)
    except Exception:
        log.exception("verification failed for %s; existing hold retained", iid)
    finally:
        router._verification_at[iid] = router._clock()
        router.scheduler.verifying.discard((iid, kind))


async def verify_health(router: NarwhalRouter, iid: str, url: str) -> None:
    """Resolve a health-evidence suspect with an engine health probe."""
    from ..runtime.readmission import allow_profile_recovery

    verdict = await router.engines.healthy(url)
    if verdict is None:
        log.info("suspect %s probe waited out the control pool; verdict deferred", iid)
        return
    if verdict:
        if not await allow_profile_recovery(router, iid):
            return
        router.scheduler.record_answer(iid, "health")
        log.info("suspect %s passed health verification; health failure classes cleared", iid)
        return
    if not router.scheduler.role_covered_without(iid):
        log.warning("%s stays live after a failed /health probe: it alone serves its role", iid)
        return
    if router.scheduler.eject(iid):
        log.warning("ejected %s: timeout-shaped failures and /health did not answer", iid)


async def verify_inference(router: NarwhalRouter, iid: str, url: str) -> None:
    """Verify a suspect engine with a prefill/decode probe."""
    from ..runtime.readmission import allow_profile_recovery

    sources = router._inference_sources.get(iid, {""}).copy()
    for source in sorted(sources):
        producer = router.monitor.instances.get(source) if source else None
        if source and producer is None:
            return  # Transfer verification requires the original producer.
        probe = await router.engines.probe_inference(
            url,
            prefill_url=producer.url if producer is not None else None,
            deadline_s=router.cfg.probe_deadline_s(),
        )
        if not resolve_inference_probe(router, iid, probe):
            return
    if sources != router._inference_sources.get(iid, {""}):
        return  # A newly failed path still needs verification.
    if not await allow_profile_recovery(router, iid):
        return
    if sources != router._inference_sources.get(iid, {""}):
        return  # Another path failed while profile evidence was being checked.
    router._inference_sources.pop(iid, None)
    router.scheduler.record_answer(iid, "verification")
    log.info("suspect %s passed inference verification; failures cleared", iid)


def resolve_inference_probe(router: NarwhalRouter, iid: str, probe: InferenceProbe | None) -> bool:
    """Resolve failure or defer an inconclusive probe without lifting its hold."""
    if probe is None:
        log.warning("inference probe unavailable for %s; verification deferred", iid)
        return False
    legs = {"prefill": probe.prefill, "decode": probe.decode}
    if any(leg.inconclusive for leg in legs.values()):
        if probe.decode.inconclusive and probe.prefill.failed is not None:
            log.info(
                "suspect %s inference probe inconclusive: producer leg failed %s, "
                "decode leg untested; verdict deferred",
                iid,
                probe.prefill.failed,
            )
        else:
            log.info(
                "suspect %s inference probe waited out the control pool; verdict deferred",
                iid,
            )
        return False
    failed = {name: leg.failed for name, leg in legs.items() if leg.failed is not None}
    if not failed:
        return True
    detail = ", ".join(f"{name} leg failed {klass}" for name, klass in sorted(failed.items()))
    if not router.scheduler.role_covered_without(iid):
        router.scheduler.quarantined.pop(iid, None)
        router.scheduler.refresh_floor_state()
        log.warning(
            "%s stays live after a failed inference probe (%s): it alone serves its role",
            iid,
            detail,
        )
        return False
    if router.scheduler.eject(iid):
        log.warning("ejected %s: inference probe failed (%s)", iid, detail)
    else:
        log.warning("already ejected %s: inference probe failed (%s)", iid, detail)
    return False
