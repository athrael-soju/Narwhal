"""Classify failed engine legs and verify suspect engines before lifting holds."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING

import httpx

from ...engines.client import EngineError, InferenceProbe, first_output_timeout, leg_failure_class
from ...types import LEG_OVERLOAD, LEG_STREAM

if TYPE_CHECKING:
    from .routing import NarwhalRouter

log = logging.getLogger("narwhal.server")


class SuspectVerifier:
    """Record leg failures and resolve suspect engines with health or inference probes."""

    def __init__(self, router: NarwhalRouter) -> None:
        self.router = router
        self.sources: dict[str, set[str]] = {}
        self.verified_at: dict[str, float] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def leg_failed(
        self,
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
            self.router.scheduler.record_answer(iid, "transport")
            return
        # Decode read timeouts, including those before headers, need inference verification.
        klass = (
            "stream"
            if decode_leg and isinstance(exc, httpx.ReadTimeout)
            else leg_failure_class(exc)
        )
        if klass is None:
            return
        if klass == LEG_STREAM and progressed and first_output_timeout(exc):
            # A first-token timeout while the engine produced other output is overload.
            klass = LEG_OVERLOAD
        if klass == "stream":
            self.sources.setdefault(iid, set()).add(prefill_iid or "")
        verdict = self.router.scheduler.record_failure(iid, klass)
        if verdict == "eject":
            log.warning(
                "ejected %s after %d consecutive %s failures; it takes no "
                "dispatch until readmission succeeds",
                iid,
                self.router.scheduler.availability.eject_after,
                klass,
            )
        elif verdict in ("verify_health", "verify_inference"):
            if verdict == "verify_inference":
                self.router.scheduler.inference_suspects.add(iid)
                if not self.router.scheduler.quarantine(iid, math.inf):
                    log.warning(
                        "%s stays live during inference verification: it alone serves its role", iid
                    )
            # One pending probe per engine and verdict kind; extra failures
            # while it runs only grow the streak it will resolve.
            key = (iid, verdict)
            if key not in self.router.scheduler.verifying:
                self.router.scheduler.verifying.add(key)
                self.start(iid, verdict)

    def start(self, iid: str, kind: str) -> None:
        """Verify `iid` in a background task."""
        task = asyncio.create_task(self._verify_suspect(iid, kind))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _verify_suspect(self, iid: str, kind: str) -> None:
        """Resolve a suspect engine with a health or inference verification."""
        try:
            inst = self.router.monitor.instances.get(iid)
            if inst is None:
                return
            if kind == "verify_inference":
                await self.verify_inference(iid, inst.url)
            else:
                await self._verify_health(iid, inst.url)
        except Exception:
            log.exception("verification failed for %s; existing hold retained", iid)
        finally:
            self.verified_at[iid] = self.router._clock()
            self.router.scheduler.verifying.discard((iid, kind))

    async def _verify_health(self, iid: str, url: str) -> None:
        """Resolve a health-evidence suspect with an engine health probe."""
        from ...runtime.lifecycle.identity import allow_profile_recovery

        verdict = await self.router.engines.healthy(url)
        if verdict is None:
            log.info("suspect %s probe waited out the control pool; verdict deferred", iid)
            return
        if verdict:
            if not await allow_profile_recovery(self.router, iid):
                return
            self.router.scheduler.record_answer(iid, "health")
            log.info("suspect %s passed health verification; health failure classes cleared", iid)
            return
        if not self.router.scheduler.role_covered_without(iid):
            log.warning("%s stays live after a failed /health probe: it alone serves its role", iid)
            return
        if self.router.scheduler.eject(iid):
            log.warning("ejected %s: timeout-shaped failures and /health did not answer", iid)

    async def verify_inference(self, iid: str, url: str) -> None:
        """Verify a suspect engine with a prefill/decode probe."""
        from ...runtime.lifecycle.identity import allow_profile_recovery

        sources = self.sources.get(iid, {""}).copy()
        for source in sorted(sources):
            producer = self.router.monitor.instances.get(source) if source else None
            if source and producer is None:
                return  # Transfer verification requires the original producer.
            probe = await self.router.engines.probe_inference(
                url,
                prefill_url=producer.url if producer is not None else None,
                deadline_s=self.router.cfg.probe_deadline_s(),
            )
            if not self._resolve_inference_probe(iid, probe):
                return
        if sources != self.sources.get(iid, {""}):
            return  # A newly failed path still needs verification.
        if not await allow_profile_recovery(self.router, iid):
            return
        if sources != self.sources.get(iid, {""}):
            return  # Another path failed while profile evidence was being checked.
        self.sources.pop(iid, None)
        self.router.scheduler.record_answer(iid, "verification")
        log.info("suspect %s passed inference verification; failures cleared", iid)

    def _resolve_inference_probe(self, iid: str, probe: InferenceProbe | None) -> bool:
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
        if not self.router.scheduler.role_covered_without(iid):
            self.router.scheduler.quarantined.pop(iid, None)
            self.router.scheduler.refresh_floor_state()
            log.warning(
                "%s stays live after a failed inference probe (%s): it alone serves its role",
                iid,
                detail,
            )
            return False
        if self.router.scheduler.eject(iid):
            log.warning("ejected %s: inference probe failed (%s)", iid, detail)
        else:
            log.warning("already ejected %s: inference probe failed (%s)", iid, detail)
        return False
