"""The serving core: one request against the fleet, and the monitoring loop.

The FastAPI app lives in `app`, the journal in `journal`, the response
shapes in `schemas`; this module owns the router itself.

One request becomes two sub-requests, each scheduled by Algorithm 1 (Arrow §5.2).
Algorithm 2's monitoring loop runs as a background task on the update interval.
Every request is journalled on the way out.

TTFT is cut at `q1 + p1` (Arrow §4.2), when the prefill leg returns o1, so the KV
transfer and queueing delays land in TPOT's first interval `t2` (Arrow §4.3). The
decode leg regenerates o1 rather than resuming at o2, which is vLLM's
disaggregated protocol, so the client's first byte arrives later than TTFT says.
That figure is journalled separately as `first_byte_s`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from . import state as handoff_state
from .config import FleetConfig
from .connector import lookup as lookup_connector
from .dialect import lookup as lookup_dialect
from .engine import EngineClient, EngineError, sse_text, sse_token_count
from .health import DriftTracker
from .journal import RunJournal
from .metrics import Histogram, buckets_for
from .monitor import InstanceMonitor
from .planner import Planner
from .profiler import ProfileStore
from .scheduler import GlobalScheduler
from .tenant import TenantLedger
from .types import Instance, Phase, Request, Role
from .wandb_export import Exporter

log = logging.getLogger("narwhal.server")


def _status_of(exc: BaseException) -> int:
    """The HTTP status a failed leg earns: 504 for time, 502 for fault.

    `EngineError.status` carries the upstream verdict; anything time-shaped
    is a gateway timeout and everything else a bad gateway, so a client's
    retry policy sees the truth instead of a uniform 502.
    """
    if isinstance(exc, EngineError):
        return 504 if exc.status in (408, 504) else 502
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return 504
    return 502


class _Refused(Exception):
    """The gate's verdict for one waiter: its cheapest placement misses TTFT."""

    def __init__(self, predicted_s: float) -> None:
        super().__init__(f"cheapest placement prices TTFT at {predicted_s:.2f}s")
        self.predicted_s = predicted_s


class _BatchGate:
    """Gathers prefill placements for a window, assigns them jointly.

    The first arrival opens the window; it closes after `window_s` or at
    `batch_max` waiters, whichever comes first, and one pass of
    `schedule_batch` places everyone. A lone request in a quiet window
    pays the window once and is then placed exactly as greedy would.

    With a `budget` set, a waiter whose cheapest placement in the window is
    already over it is refused rather than placed: dead-on-arrival work
    never dispatches ahead of work that can still make it.
    """

    def __init__(
        self,
        scheduler: GlobalScheduler,
        window_s: float,
        batch_max: int,
        budget_s: float | None = None,
        weight_of: Callable[[Request], float] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._window_s = window_s
        self._max = batch_max
        self._budget_s = budget_s
        self._weight_of = weight_of
        self._waiters: list[tuple[Request, asyncio.Future]] = []
        self._closing: asyncio.Task | None = None

    async def place(self, req: Request) -> Instance:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.append((req, fut))
        if len(self._waiters) >= self._max:
            self._flush()
        elif self._closing is None or self._closing.done():
            self._closing = asyncio.ensure_future(self._close_after())
        return await fut

    async def _close_after(self) -> None:
        await asyncio.sleep(self._window_s)
        self._flush()

    def _flush(self) -> None:
        if not self._waiters:
            return
        batch, self._waiters = self._waiters, []
        # A gathered window places heavier classes first. The sort is
        # stable on arrival order inside one class, so the window's own clock
        # stays the tie-breaker the gate already had.
        weight_of = self._weight_of
        if weight_of is not None:
            batch.sort(key=lambda rf: weight_of(rf[0]), reverse=True)
        go = []
        for r, fut in batch:
            price = (
                self._scheduler.cheapest_prefill_price(r) if self._budget_s is not None else None
            )
            if price is not None and self._budget_s is not None and price > self._budget_s:
                # A waiter whose client went away holds a cancelled future;
                # setting an exception on it raises InvalidStateError and, in
                # the _close_after path, strands every other waiter unresolved.
                if not fut.done():
                    fut.set_exception(_Refused(price))
            else:
                go.append((r, fut))
        placed = self._scheduler.schedule_batch([r for r, _ in go]) if go else {}
        for r, fut in go:
            if not fut.done():
                fut.set_result(placed[r.rid])


class ArrowRouter:
    """Holds the fleet state and serves one request against it."""

    def __init__(
        self,
        cfg: FleetConfig,
        journal: RunJournal,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_concurrent: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.journal = journal
        # Opt-in payload sidecar (--journal-payloads); None records nothing.
        self.payloads = None
        # A standby router holds the door (503) and lets
        # the monitoring loop idle until its watch applies the primary's
        # handoff and clears this. takeover_gap_s is the measured MTTR.
        self.standby = False
        self.takeover_gap_s: float | None = None
        # One clock for the monitor, the scheduler and every timestamp on the
        # request path, so a test drives Algorithm 2 and reads exact latencies.
        self._clock = clock
        self.profiles = ProfileStore(cfg.profiles_path)
        self.monitor = InstanceMonitor(clock=clock, profiles=self.profiles)
        for spec in cfg.engines:
            self.monitor.add(Instance(iid=spec.iid, url=spec.url, role=spec.role))
        self.scheduler = GlobalScheduler(
            self.monitor,
            self.profiles,
            cfg.slo,
            cfg.thresholds,
            clock=clock,
            eject_after=cfg.eject_after,
            flip_history=cfg.flip_history,
            prefill_affinity=cfg.prefill_affinity,
            flip_offline_s=cfg.flip_offline_s,
            pinned=frozenset(e.iid for e in cfg.engines if e.pin),
            min_prefill=cfg.min_prefill,
            prefix_coop=cfg.prefix_coop,
            prefix_halflife_s=cfg.prefix_halflife_s,
            health=DriftTracker(
                clock=clock,
                window_s=cfg.health_window_s,
                band=cfg.health_drift_band,
                min_samples=cfg.health_min_samples,
                probation_windows=cfg.health_probation_windows,
                evict_windows=cfg.health_evict_windows,
                recovery_windows=cfg.health_recovery_windows,
                penalty_s=cfg.health_probation_penalty_s,
                relative_band=cfg.health_relative_band,
                min_ttft_s=cfg.health_min_ttft_s,
            ),
        )
        self.engines = EngineClient(
            timeout_s=cfg.request_timeout_s,
            prefill_timeout_s=cfg.prefill_timeout_s,
            read_timeout_s=cfg.decode_read_timeout_s,
            max_connections=cfg.max_connections,
            pool_timeout_s=cfg.pool_timeout_s,
            connect_timeout_s=cfg.connect_timeout_s,
            health_timeout_s=cfg.health_timeout_s,
            transport=transport,
            kv=lookup_connector(cfg.connector),
            dialect=lookup_dialect(cfg.dialect),
        )
        self.served = 0
        self.failed = 0
        self.refused = 0
        # The admission door's two honest signals: `rejected` is pool
        # exhaustion, `refused` is the cost model pricing every landing over
        # the TTFT budget. Same 429, different stories.
        self.rejected = 0
        # In-flight prefill legs by rid (task, instance), so a re-placement
        # pass can cancel a queued leg and the serve loop can re-drive it.
        self._legs: dict[str, tuple[asyncio.Task, str]] = {}
        # Rids whose cancellation came from a re-placement, to be told apart
        # from a client that went away.
        self._replaced: set[str] = set()
        self.ttft = Histogram(buckets_for(cfg.slo.ttft_s))
        self.tpot = Histogram(buckets_for(cfg.slo.tpot_s))
        self.exporter = Exporter.from_config(cfg)
        self.planner: Planner | None = None
        if cfg.controller == "planner":
            self.planner = Planner(
                self.monitor,
                self.scheduler,
                clock=clock,
                interval_s=cfg.plan_interval_s,
                window_s=cfg.plan_window_s,
                confirmations=cfg.plan_confirmations,
                utilization=cfg.plan_utilization,
                min_arrivals=cfg.plan_min_arrivals,
                demand_floor=cfg.plan_demand_floor,
                deadband=cfg.plan_deadband,
                fast_step_s=cfg.plan_fast_step_s,
                attainment_floor=cfg.plan_attainment_floor,
            )
            self.scheduler.controller_owns_flips = True
        # The batching gate: prefill placements gather here for one
        # short window and are assigned jointly. None under greedy
        # placement, which is the default and the Arrow paper's behaviour. A
        # predictive door also gives the gate the budget, so a window's
        # dead-on-arrival work is refused instead of placed.
        self._gate: _BatchGate | None = None
        if cfg.placement == "batched":
            budget = None
            if cfg.admission == "predictive":
                budget = cfg.slo.ttft_s * (1.0 + cfg.admission_margin)
            self._gate = _BatchGate(
                self.scheduler,
                cfg.batch_window_ms / 1000.0,
                cfg.batch_max,
                budget_s=budget,
                weight_of=lambda r: self.tenants.weight_of(r.tenant),
            )
        # The instance that answered /tokenize last, asked first next time.
        self._tokenizer: str | None = None
        self.max_concurrent = max_concurrent if max_concurrent is not None else cfg.max_connections
        self.tenants = TenantLedger(
            self.max_concurrent,
            cfg.tenants,
            auth_required=cfg.tenant_auth_required,
            anonymous_weight=cfg.tenant_anonymous_weight,
        )
        self.inflight = 0

    def unprofiled(self) -> list[str]:
        """Instance ids with no measured profile; the lifespan refuses them."""
        return [i for i in self.monitor.instances if self.profiles.get(i) is None]

    async def input_length(self, body: dict[str, Any]) -> int:
        """Token count for the prefill cost, exact where the engine allows it.

        One instance is asked, not the fleet: every engine loads the same
        tokenizer, so a second answer says nothing the first did not, while
        walking them charges every request the timeout of each wedged node it
        passes. A probe that fails moves to the next instance for the request
        after it, and the request in hand takes the character estimate.
        """
        if self.cfg.tokenize:
            live = [
                i for i in self.monitor.instances.values() if i.iid not in self.scheduler.ejected
            ]
            if live:
                k = next((j for j, i in enumerate(live) if i.iid == self._tokenizer), 0)
                n = await self.engines.token_count(live[k].url, body, self.cfg.tokenize_timeout_s)
                if n is not None:
                    self._tokenizer = live[k].iid
                    return n
                self._tokenizer = live[(k + 1) % len(live)].iid
        raw = body.get("prompt")
        if raw is None:
            raw = "".join(str(m.get("content", "")) for m in body.get("messages", []) or [])
        if isinstance(raw, list):
            return len(raw)
        return max(1, int(len(str(raw)) / self.cfg.chars_per_token))

    async def serve(
        self, endpoint: str, body: dict[str, Any], headers: dict[str, str]
    ) -> StreamingResponse | JSONResponse:
        """Admit one request against the engine pool, or refuse it.

        A request holds one engine connection at a time, so `max_concurrent`
        admitted requests fit the pool exactly. The request past that one waits
        inside the pool for a slot, and that wait lands in its own TTFT while
        Algorithm 1 prices the instance as if the request had been placed. A
        429 costs the client a retry and costs the measurement nothing.
        """
        rid = str(uuid.uuid4())
        tenant = self.tenants.resolve(headers)
        name = self.tenants.name_of(tenant)
        if self.tenants.specs and self.tenants.auth_required and tenant is None:
            # The tenant door. A request that names nobody gets the OpenAI shape;
            # the refusal counts on the anonymous book, visibly.
            self.rejected += 1
            self.tenants.door_refused(name)
            return JSONResponse(
                status_code=401,
                headers={"x-request-id": rid},
                content={
                    "error": {
                        "message": "this fleet serves named tenants; present the API key "
                        "your operator issued for it",
                        "type": "authentication_error",
                    }
                },
            )
        if not self.tenants.can_admit(tenant):
            # The pool is the outer bound; the tenant's share is the fair one,
            # and the flooding class pays for its own share first.
            self.rejected += 1
            return JSONResponse(
                status_code=429,
                headers={"retry-after": "1", "x-request-id": rid},
                content={
                    "error": {
                        "message": (
                            f"{self.inflight} requests are in flight against an engine "
                            f"pool of {self.max_concurrent} connections"
                        ),
                        "type": "server_overloaded_error",
                    }
                },
            )
        self.inflight += 1
        self.tenants.admitted(name)
        try:
            response = await self._serve(rid, endpoint, body, headers, tenant=name)
        except BaseException:
            self.inflight -= 1
            self.tenants.completed(name, served=False)
            raise
        # The header is the thread that joins a client, a log line and a
        # journal row: the same rid is on all three.
        response.headers["x-request-id"] = rid
        if isinstance(response, StreamingResponse):
            # The generator has not run yet; `_held` releases the slot when
            # the stream ends, however it ends - and bills the tenant on how
            # it actually ended, which the outcome dict carries.
            outcome = getattr(response, "narwhal_outcome", None)
            response.body_iterator = self._held(name, response.body_iterator, outcome)
            return response
        self.inflight -= 1
        # A 429 out of _serve is the predictive door (pool exhaustion and 401
        # both return before the seat is taken): the tenant's book reads it
        # as a refusal, not as the fleet failing the request.
        self.tenants.completed(
            name,
            served=response.status_code == 200,
            refused=response.status_code == 429,
        )
        return response

    async def _held(
        self, name: str, stream: AsyncIterable[Any], outcome: dict[str, Any] | None = None
    ) -> AsyncIterator[Any]:
        """Hold the admission slot until the streamed response ends."""
        try:
            async for line in stream:
                yield line
        finally:
            self.inflight -= 1
            served = outcome is None or outcome.get("error") is None
            self.tenants.completed(name, served=served)

    async def _serve(
        self,
        rid: str,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        tenant: str = "anonymous",
    ) -> StreamingResponse | JSONResponse:
        arrived = self._clock()
        client_rid = next((v for k, v in headers.items() if k.lower() == "x-request-id"), None)
        asked = body.get("model")
        if asked and asked != self.cfg.model:
            # Serving a name that was never loaded attributes the numbers to
            # the wrong model. Refuse it the way the engines themselves do.
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"model {asked!r} is not served here; "
                        f"this router serves {self.cfg.model!r}",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )
        body = {**body, "model": self.cfg.model}
        wants_stream = bool(body.get("stream"))

        for field_name in ("n", "best_of"):
            if int(body.get(field_name) or 1) > 1:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": (
                                f"{field_name} > 1 cannot be served across a split: the "
                                f"prefill leg runs with max_tokens=1 and the two legs "
                                f"would disagree on sampling width"
                            ),
                            "type": "invalid_request_error",
                        }
                    },
                )

        input_len = await self.input_length(body)
        if self.planner is not None:
            self.planner.saw_arrival(input_len)
        prefix_key = None
        prefix_len = None
        if self.cfg.prefill_affinity or self.cfg.prefix_coop:
            # One key feeds both prefix games: the affinity ablation's override
            # and the cooperative priced reuse, which are never on together. The span
            # priced is the span the hash can prove: tails past the hashed
            # head are invisible from the door.
            raw = body.get("prompt")
            if raw is None:
                raw = "".join(str(m.get("content", "")) for m in body.get("messages", []) or [])
            import zlib

            prefix_key = zlib.crc32(str(raw)[:2048].encode("utf-8", "ignore"))
            prefix_len = min(input_len, max(1, int(2048 / self.cfg.chars_per_token)))
        req = Request(
            rid=rid,
            input_len=input_len,
            phase=Phase.PREFILL,
            prefix_key=prefix_key,
            prefix_len=prefix_len,
            tenant=tenant,
        )

        while True:
            # A re-placed leg pays the batching window twice for nothing;
            # it schedules greedily from here.
            gate_placed = False
            if self._gate is not None and req.replaced == 0:
                try:
                    prefill = await self._gate.place(req)
                except _Refused as exc:
                    return self._refusal(
                        rid, client_rid, arrived, req, exc.predicted_s, tenant=tenant
                    )
                gate_placed = True
            else:
                prefill = self.scheduler.schedule(req)
            # A gate placement was budget-checked jointly with its window;
            # re-pricing it here after earlier waiters dispatched would count
            # window peers as resident work and refuse admitted requests on
            # coroutine resume order.
            if self.cfg.admission == "predictive" and not gate_placed:
                priced = self.scheduler.cost(req, prefill)
                if not self.scheduler.meets_slo(req, priced, ttft_margin=self.cfg.admission_margin):
                    return self._refusal(rid, client_rid, arrived, req, priced[1], tenant=tenant)
            # The prediction the drift signal grades this placement against
            # Algorithm 1's own quoted cost for it, taken before the
            # dispatch makes the request resident - after, the quote would
            # price the request twice. A re-driven leg re-quotes at its new
            # home, so the residual grades the placement that served.
            predicted_s = self.scheduler.cost(req, prefill)[1]
            # Admitted: only now does the placement teach the prefix maps -
            # a refused request computes nothing anywhere.
            self.scheduler.remember(req, prefill)
            self.monitor.dispatched(prefill.iid, req)
            # The leg runs as its own task: a re-placement pass can cancel a
            # queued leg, and the loop re-drives it at a freshly priced home.
            leg = asyncio.ensure_future(self.engines.prefill(prefill.url, endpoint, body, headers))
            self._legs[rid] = (leg, prefill.iid)
            try:
                kv_params = await leg
            except asyncio.CancelledError:
                self.monitor.finished(prefill.iid, rid)
                self._legs.pop(rid, None)
                task = asyncio.current_task()
                if rid not in self._replaced or (task is not None and task.cancelling()):
                    # The client went away - the serve task itself is being
                    # cancelled - so abort the engine's work and propagate,
                    # even if a re-placement pass marked this rid in the
                    # same breath: nobody is left to serve the re-drive.
                    self._replaced.discard(rid)
                    leg.cancel()
                    raise
                self._replaced.discard(rid)
                req.replaced += 1
                log.info("re-placed queued prefill %s: %s had priced it out", rid, prefill.iid)
                continue
            except (EngineError, Exception) as exc:
                self.monitor.finished(prefill.iid, rid)
                self._legs.pop(rid, None)
                self.failed += 1
                self._leg_failed(prefill.iid, exc)
                detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
                self._journal_failure(
                    rid,
                    client_rid,
                    arrived,
                    input_len,
                    prefill.iid,
                    None,
                    detail,
                    tenant=req.tenant,
                )
                return JSONResponse(
                    status_code=_status_of(exc),
                    content={"error": {"message": detail, "type": "prefill"}},
                )
            self._legs.pop(rid, None)
            # A re-placement mark that lost the race to this leg's completion
            # must not linger: the set would grow and the pass would keep
            # counting phantom moves against its cap.
            self._replaced.discard(rid)
            break
        # o1 exists, so TTFT ends here (Arrow §4.2) and the TPOT clock starts.
        prefilled_at = self._clock()
        self.scheduler.record_answer(prefill.iid)
        self.scheduler.note_ttft(prefill.iid, prefilled_at - arrived, predicted_s)
        self.monitor.first_token(prefill.iid, rid)

        req.phase = Phase.DECODE
        decode = self.scheduler.schedule(req)
        self.monitor.dispatched(decode.iid, req)
        crossed = decode.iid != prefill.iid

        outcome: dict[str, Any] = {"error": None, "status": 200}
        stream = self._run_decode(
            rid=rid,
            client_rid=client_rid,
            arrived=arrived,
            prefilled_at=prefilled_at,
            input_len=input_len,
            endpoint=endpoint,
            body=body,
            headers=headers,
            prefill=prefill,
            decode=decode,
            crossed=crossed,
            kv_params=kv_params if crossed else None,
            req=req,
            outcome=outcome,
            emit_error_frame=wants_stream,
        )
        if wants_stream:
            response = StreamingResponse(stream, media_type="text/event-stream")
            # The stream commits to 200 before the decode legs run; the
            # outcome dict is how the true ending reaches the tenant books.
            response.narwhal_outcome = outcome  # type: ignore[attr-defined]
            return response

        # A client that did not ask to stream still gets a streamed engine leg,
        # because the monitor is defined on the token stream (Arrow §5.2). Reassemble
        # only what actually succeeded: a failed leg is a failed request, not
        # an empty completion with finish_reason "stop".
        chunks = [line async for line in stream]
        if outcome["error"] is not None:
            return JSONResponse(
                status_code=outcome["status"],
                content={"error": {"message": outcome["error"], "type": "decode"}},
            )
        out = _reassemble(chunks)
        # The engine's own usage wins when its final chunk carried one.
        out.setdefault(
            "usage",
            {
                "prompt_tokens": input_len,
                "completion_tokens": outcome.get("tokens", 0),
                "total_tokens": input_len + outcome.get("tokens", 0),
            },
        )
        return JSONResponse(content=out)

    async def _run_decode(
        self,
        *,
        rid: str,
        client_rid: str | None,
        arrived: float,
        prefilled_at: float,
        input_len: int,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        prefill: Instance,
        decode: Instance,
        crossed: bool,
        kv_params: dict[str, Any] | None,
        req: Request,
        outcome: dict[str, Any] | None = None,
        emit_error_frame: bool = False,
    ) -> AsyncIterator[str]:
        first_at: float | None = None
        last_at: float | None = None
        tokens = 0
        out_text: list[str] = []
        out_chars = 0
        error: str | None = None
        tried: list[str] = []
        try:
            while True:
                tried.append(decode.iid)
                try:
                    async for line in self.engines.decode(
                        decode.url,
                        endpoint,
                        body,
                        headers,
                        kv_params,
                        first_token_timeout_s=self.cfg.first_token_timeout_s,
                    ):
                        n = sse_token_count(line)
                        for _ in range(n):
                            self.monitor.output_token(decode.iid, rid)
                        if n:
                            last_at = self._clock()
                            if first_at is None:
                                first_at = last_at
                        tokens += n
                        if self.payloads is not None and out_chars < self.payloads.max_chars:
                            piece = sse_text(line)
                            if piece:
                                out_text.append(piece)
                                out_chars += len(piece)
                        yield line + "\n\n"
                    self.scheduler.record_answer(decode.iid)
                    break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}".rstrip(": ")
                    log.warning("decode leg failed | rid=%s iid=%s | %s", rid, decode.iid, error)
                    self._leg_failed(decode.iid, exc)
                    # Nothing reached the client yet, so another instance may
                    # still serve this request. Once a token has been streamed
                    # the response is committed and the failure stands.
                    nxt = self._reroute(rid, decode, req, tokens, tried)
                    if nxt is None:
                        self.failed += 1
                        if outcome is not None:
                            outcome["error"] = error
                            outcome["status"] = _status_of(exc)
                        if emit_error_frame:
                            # The stream is committed at 200, so the failure
                            # travels as the terminal event instead of a
                            # clean-looking end of output.
                            yield (
                                "data: "
                                + json.dumps({"error": {"message": error, "type": "decode"}})
                                + "\n\n"
                            )
                        break
                    decode = nxt
                    crossed = decode.iid != prefill.iid
                    kv_params = kv_params if crossed else None
                    error = None
        finally:
            # Always retire the request: a leak inflates this instance's cost
            # for every later Algorithm 1 decision.
            self.monitor.finished(decode.iid, rid)
            if outcome is not None:
                outcome["tokens"] = tokens
            if error is None:
                self.served += 1
            self.scheduler.note_outcome(
                error is None and (prefilled_at - arrived) <= self.scheduler.slo.ttft_s,
                error is None
                and (
                    not (last_at and tokens > 1)
                    or (last_at - prefilled_at) / (tokens - 1) <= self.scheduler.slo.tpot_s
                ),
            )
            self.ttft.observe(prefilled_at - arrived)
            if last_at and tokens > 1:
                self.tpot.observe((last_at - prefilled_at) / (tokens - 1))
            if self.payloads is not None:
                self.payloads.write(rid, _prompt_text(body), "".join(out_text))
            self.journal.write(
                {
                    "rid": rid,
                    "client_rid": client_rid,
                    "arrived": arrived,
                    "input_len": input_len,
                    "output_len": tokens,
                    "wanted_len": int(body.get("max_tokens") or 0),
                    "ttft_s": prefilled_at - arrived,
                    "tpot_s": ((last_at - prefilled_at) / (tokens - 1))
                    if last_at and tokens > 1
                    else None,
                    "first_byte_s": (first_at - arrived) if first_at else None,
                    "prefill_iid": prefill.iid,
                    "decode_iid": decode.iid,
                    "crossed": crossed,
                    "attempts": tried,
                    "error": error,
                    "tenant": req.tenant,
                }
            )

    def _reroute(
        self, rid: str, failed: Instance, req: Request, tokens: int, tried: list[str]
    ) -> Instance | None:
        """Pick another decode instance, or None if the request must fail."""
        if tokens or len(tried) >= self.cfg.decode_attempts:
            return None
        self.monitor.finished(failed.iid, rid)
        try:
            nxt = self.scheduler.schedule(req, exclude=set(tried))
        except RuntimeError:
            return None
        self.monitor.dispatched(nxt.iid, req)
        log.warning(
            "decode leg on %s failed with nothing streamed; retrying on %s", failed.iid, nxt.iid
        )
        return nxt

    def _leg_failed(self, iid: str, exc: BaseException) -> None:
        """Feed one failed leg to the breaker.

        A 4xx names the caller's body rather than a sick engine, and an
        engine that returns one has answered, so it clears the count instead
        of raising it.
        """
        status = exc.status if isinstance(exc, EngineError) else 500
        if 400 <= status < 500:
            self.scheduler.record_answer(iid)
            return
        # A dead engine presents as connection failures; an overloaded one as
        # timeouts. Only the first shape ejects directly.
        connection_shaped = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
        verdict = self.scheduler.record_failure(iid, connection_shaped=connection_shaped)
        if verdict == "eject":
            log.warning(
                "ejected %s after %d consecutive failed legs; it takes no "
                "dispatch until /health answers",
                iid,
                self.scheduler._eject_after,
            )
        elif verdict == "verify":
            # Timeout-shaped failures at threshold: the engine is suspect,
            # not condemned. /health decides - a flooded engine answers it,
            # a wedged listener does not.
            asyncio.get_running_loop().create_task(self._verify_suspect(iid))

    async def _verify_suspect(self, iid: str) -> None:
        """Probe a timeout-suspect engine's /health; eject only on failure."""
        inst = self.monitor.instances.get(iid)
        if inst is None:
            return
        if await self.engines.healthy(inst.url):
            self.scheduler.record_answer(iid)
            log.info("suspect %s answered /health; failures were load-shaped, not ejected", iid)
            return
        if self.scheduler.eject(iid):
            log.warning("ejected %s: timeout-shaped failures and /health did not answer", iid)

    def _journal_failure(
        self,
        rid: str,
        client_rid: str | None,
        arrived: float,
        input_len: int,
        prefill_iid: str,
        decode_iid: str | None,
        detail: str,
        *,
        tenant: str = "anonymous",
    ) -> None:
        self.scheduler.note_outcome(False, True)
        self.journal.write(
            {
                "rid": rid,
                "client_rid": client_rid,
                "arrived": arrived,
                "input_len": input_len,
                "output_len": 0,
                "wanted_len": 0,
                "attempts": [],
                "ttft_s": None,
                "tpot_s": None,
                "first_byte_s": None,
                "prefill_iid": prefill_iid,
                "decode_iid": decode_iid,
                "crossed": False,
                "error": detail,
                "tenant": tenant,
            }
        )

    def _refusal(
        self,
        rid: str,
        client_rid: str | None,
        arrived: float,
        req: Request,
        priced_s: float,
        *,
        tenant: str = "anonymous",
    ) -> JSONResponse:
        """Refuse at the door what the fleet cannot serve inside the budget.

        `rejected` counts pool exhaustion; `refused` counts the cost model
        itself pricing every landing over the TTFT budget. The journal row
        carries `refused: true` so refused reads apart from served-late and
        destroyed, and the planner hears the pressure.

        Two different refusals wear that one status, and the client needs
        them apart. Work ahead of a request drains, so a queued refusal
        quotes Retry-After as the priced overrun and the client is right to
        come back. A prompt whose own prefill already misses the budget
        drains into nothing: no wait makes it servable, so it carries no
        Retry-After and names what would - a shorter prompt, or a budget
        that fits the fleet. `refused_cause` splits the two in the journal.
        """
        budget = self.scheduler.slo.ttft_s * (1.0 + self.cfg.admission_margin)
        floor_s = self.scheduler.cheapest_own_prefill(req)
        over_alone = floor_s is not None and floor_s > budget
        self.refused += 1
        # Refused work is offered work the fleet could not take on time: the
        # planner reads it as TTFT pressure, same as a miss it watched die.
        self.scheduler.note_outcome(False, True)
        if over_alone:
            detail = (
                f"refused: this prompt's own prefill prices TTFT at {floor_s:.2f}s "
                f"against the {budget:.2f}s budget"
            )
            message = (
                f"this prompt's own prefill prices TTFT at {floor_s:.2f}s against the "
                f"{budget:.2f}s budget; no queue drains that, so shorten the prompt "
                f"or raise the TTFT budget"
            )
            headers: dict[str, str] = {}
            log.info(
                "refused %s at the door: the prompt alone prices %.2fs vs %.2fs budget",
                rid,
                floor_s,
                budget,
            )
        else:
            detail = (
                f"refused: cheapest placement prices TTFT at {priced_s:.2f}s "
                f"against the {budget:.2f}s budget"
            )
            message = (
                f"cheapest placement prices TTFT at {priced_s:.2f}s against "
                f"the {budget:.2f}s budget; retry as the priced queue drains"
            )
            headers = {"retry-after": str(max(1, math.ceil(priced_s - budget)))}
            log.info("refused %s at the door: priced %.2fs vs %.2fs budget", rid, priced_s, budget)
        self.journal.write(
            {
                "rid": rid,
                "client_rid": client_rid,
                "arrived": arrived,
                "input_len": req.input_len,
                "output_len": 0,
                "wanted_len": 0,
                "attempts": [],
                "ttft_s": None,
                "tpot_s": None,
                "first_byte_s": None,
                "prefill_iid": None,
                "decode_iid": None,
                "crossed": False,
                "refused": True,
                "refused_cause": "prompt" if over_alone else "queue",
                "tenant": tenant,
                "error": detail,
            }
        )
        return JSONResponse(
            status_code=429,
            headers=headers,
            content={"error": {"message": message, "type": "server_overloaded_error"}},
        )

    def apply_queue_replacements(self) -> int:
        """One re-placement pass over queued prefill legs.

        The scheduler nominates, deepest miss first; this applies at most
        `replace_per_pass` of them, counting only legs actually in flight -
        the cap bounds the pass's blind spot, and legs absent from `_legs`
        already completed. The pass marks and cancels; the serve loop owning
        each leg re-places it at a freshly priced home.
        """
        moved = 0
        for rid, _src, dst in self.scheduler.queue_replacements(slack_s=self.cfg.replace_slack_s):
            if moved >= self.cfg.replace_per_pass:
                break
            entry = self._legs.get(rid)
            if entry is None or entry[0].done():
                # Already completed: a cancel would be a no-op, and counting
                # it against replace_per_pass would starve real moves.
                continue
            self._replaced.add(rid)
            entry[0].cancel()
            log.info("re-placing queued prefill %s onto %s", rid, dst)
            moved += 1
        return moved

    def state(self) -> dict[str, Any]:
        """The /arrow/state payload. StateOut pins this shape."""
        return {
            "served": self.served,
            "failed": self.failed,
            "controller": self.cfg.controller,
            # A rising reject count is the pool refusing work the fleet cannot
            # hold, which reads as lost throughput rather than as slow requests.
            # A rising refused count is the fleet saying so in advance.
            "admission": {
                "inflight": self.inflight,
                "limit": self.max_concurrent,
                "rejected": self.rejected,
                "refused": self.refused,
            },
            # The per-customer ledger. Empty until tenants are named in
            # the config or unauthenticated traffic shows up at a named door.
            "tenants": self.tenants.snapshot(),
            "pools": {
                "prefill": sorted(i.iid for i in self.monitor.pool(Role.PREFILL)),
                "decode": sorted(i.iid for i in self.monitor.pool(Role.DECODE)),
            },
            "load": {
                "prefill": round(self.scheduler.pool_load(Role.PREFILL), 4),
                "decode": round(self.scheduler.pool_load(Role.DECODE), 4),
            },
            "thresholds": {
                "expand": self.cfg.thresholds.expand,
                "shrink": self.cfg.thresholds.shrink,
                "cooldown_s": self.cfg.thresholds.cooldown_s,
                "sustained_intervals": self.cfg.thresholds.sustained_intervals,
                "dwell_s": self.cfg.thresholds.dwell_s,
                "panic_ratio": self.cfg.thresholds.panic_ratio,
            },
            "slo": {"ttft_s": self.cfg.slo.ttft_s, "tpot_s": self.cfg.slo.tpot_s},
            "first_token_timeout_s": self.cfg.first_token_timeout_s,
            "resident": {
                iid: {"prefill": len(i.prefill), "decode": len(i.decode)}
                for iid, i in self.monitor.instances.items()
            },
            # Config-pinned roles and the prefill floor: the standing answer
            # to "why does this engine never flip".
            "pinned": sorted(self.scheduler.pinned),
            "min_prefill": self.scheduler.min_prefill,
            # These instances keep their pool label and take no dispatch. The
            # breaker holds them out of every candidate list and both loads.
            "ejected": sorted(self.scheduler.ejected),
            # The probation list: healthy enough to serve, drifting enough
            # to deprioritize until the windows clear or condemn it.
            "probation": sorted(
                self.scheduler.health.probation_set() if self.scheduler.health else []
            ),
            "unserved": self.scheduler.unserved,
            "panic_bypasses": self.scheduler.panic_bypasses,
            "poa": {
                "regret": self.scheduler.placement_regret(),
                "regime": self.scheduler.regime(),
                "samples": len(self.scheduler._regrets),
            },
            "flips_refused": [
                {"at": at, "to": to, "why": why}
                for at, to, why in self.scheduler.flips_refused[-20:]
            ],
            "flips": [
                {
                    "at": f.at,
                    "iid": f.iid,
                    "to": f.to.value,
                    "by": f.by,
                    "prefill_inflight": f.prefill_inflight,
                    "decode_inflight": f.decode_inflight,
                    "drained_s": f.drained_s,
                }
                for f in self.scheduler.flips
            ],
        }


def _prompt_text(body: dict[str, Any]) -> str:
    """What the client sent, as text: the prompt, or the chat messages."""
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if prompt is not None:
        return json.dumps(prompt, ensure_ascii=False)
    messages = body.get("messages")
    return json.dumps(messages, ensure_ascii=False) if messages is not None else ""


def _reassemble(lines: list[str]) -> dict[str, Any]:
    """Fold a streamed decode leg back into one completion body."""
    text: list[str] = []
    last: dict[str, Any] = {}
    for line in lines:
        raw = line.strip()
        if not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        last = obj
        for choice in obj.get("choices", []) or []:
            piece = choice.get("text")
            if piece is None:
                piece = (choice.get("delta") or {}).get("content")
            if piece:
                text.append(piece)
    joined = "".join(text)
    # The engine says why it stopped; "stop" is only the fallback for a leg
    # that never said. A truncated-at-max_tokens response must read "length".
    reason = next(
        (
            c.get("finish_reason")
            for obj in (last,)
            for c in (obj.get("choices") or [])
            if c.get("finish_reason")
        ),
        "stop",
    )
    if not last:
        return {"choices": [{"text": joined, "index": 0, "finish_reason": "stop"}]}
    out = {k: v for k, v in last.items() if k != "choices"}
    out["object"] = out.get("object", "text_completion").replace(".chunk", "")
    out["choices"] = [{"index": 0, "text": joined, "finish_reason": reason}]
    return out


async def _readmit(router: ArrowRouter, after_s: float) -> list[str]:
    """Probe the ejected instances, and take back the ones answering /health.

    Readmission is the only way out of the breaker on a fleet that recovers,
    because an ejected instance is dispatched nothing that could prove it
    well.
    """
    due = router.scheduler.probe_due(after_s)
    if not due:
        return []
    answers = await asyncio.gather(
        *(router.engines.healthy(router.monitor.instances[iid].url) for iid in due),
        return_exceptions=True,
    )
    back = [iid for iid, ok in zip(due, answers, strict=True) if ok is True]
    for iid in back:
        router.scheduler.record_answer(iid)
        log.info("readmitted %s: /health answered", iid)
    return back


async def _sweep_liveness(router: ArrowRouter) -> list[str]:
    """Probe the live instances, and eject the ones that stop answering.

    The breaker learns from served traffic, so on an idle fleet a dead engine
    is never dispatched anything that could fail and it keeps its role
    indefinitely. This is the traffic-free path to the same verdict: ask
    /health directly, on a cadence, and eject on `liveness_misses` consecutive
    silences. With the default of two misses, one is a blip and never enough,
    because a sweep that ejects on a single timeout would take engines out for
    a hiccup no request ever noticed.
    """
    misses = router.scheduler.liveness_misses
    live = [iid for iid in router.monitor.instances if iid not in router.scheduler.ejected]
    if not live:
        return []
    answers = await asyncio.gather(
        *(router.engines.healthy(router.monitor.instances[iid].url) for iid in live),
        return_exceptions=True,
    )
    gone = []
    for iid, ok in zip(live, answers, strict=True):
        if ok is True:
            misses.pop(iid, None)
            continue
        misses[iid] = misses.get(iid, 0) + 1
        if misses[iid] < router.cfg.liveness_misses:
            log.info(
                "liveness %s: /health silent (%d of %d)",
                iid,
                misses[iid],
                router.cfg.liveness_misses,
            )
            continue
        if router.scheduler.eject(iid):
            misses.pop(iid, None)
            gone.append(iid)
            log.warning(
                "ejected %s: /health silent on %d consecutive sweeps, no traffic needed",
                iid,
                router.cfg.liveness_misses,
            )
    return gone


async def _monitor_once(router: ArrowRouter) -> Instance | None:
    """One Algorithm 2 pass, then readmission. Returns the instance it flipped.

    Separate from the loop so a caller drives one interval with no sleep.
    The flip itself logs inside `GlobalScheduler.flip`, which covers both
    algorithms' paths; this pass carries the per-pass line and the telemetry
    point. Under planner control the passes go to the planner, nothing flips
    inline, and the return is None.
    """
    interval = router.cfg.monitor_interval_s
    flipped = None
    try:
        if router.planner is not None:
            # The plan loop replaces Algorithm 2's trigger; readmission, the
            # drift instrument, and the telemetry below are controller-independent.
            router.planner.sample()
            router.planner.fast_step()
            router.planner.pass_due()
            router.scheduler.health_pass()
            router.scheduler.settle_drains()
            router.monitor.roll_interval()
        else:
            flipped = router.scheduler.monitoring_pass()
    except Exception:
        log.exception("monitoring pass failed")
        return None
    if router.cfg.queue_rebalance:
        try:
            router.apply_queue_replacements()
        except Exception:
            log.exception("re-placement pass failed")
    await _readmit(router, interval * router.cfg.readmit_every)
    if router.cfg.liveness_every and router.scheduler.sweep_due(
        interval * router.cfg.liveness_every
    ):
        try:
            await _sweep_liveness(router)
        except Exception:
            log.exception("liveness sweep failed")
    lp = router.scheduler.pool_load(Role.PREFILL)
    ld = router.scheduler.pool_load(Role.DECODE)
    n_prefill = len(router.monitor.pool(Role.PREFILL))
    n_decode = len(router.monitor.pool(Role.DECODE))
    log.info(
        "loop | Lp=%.3f Ld=%.3f | %dP%dD | unserved=%d",
        lp,
        ld,
        n_prefill,
        n_decode,
        router.scheduler.unserved,
    )
    if router.exporter is not None:
        router.exporter.log_pass(
            {
                "load/prefill": lp,
                "load/decode": ld,
                "pool/prefill": n_prefill,
                "pool/decode": n_decode,
                "served": router.served,
                "failed": router.failed,
                "unserved": router.scheduler.unserved,
                "flips": len(router.scheduler.flips),
            }
        )
    # Refresh the control-plane handoff every pass, so a crash never
    # leaves more than one interval of actuation unrecorded. A write failure
    # is the next restart's small problem, never this pass's.
    try:
        handoff_state.write(router.cfg.state_path, handoff_state.snapshot(router))
    except OSError as exc:
        log.warning("state handoff not written: %s", exc)
    return flipped


async def _monitor_loop(router: ArrowRouter) -> None:
    """Arrow §5.5's update interval. One pass per interval, closing the window."""
    while True:
        await asyncio.sleep(router.cfg.monitor_interval_s)
        if router.standby:
            # A standby actuates nothing - the primary owns the fleet's
            # picture until the takeover applies it here.
            continue
        await _monitor_once(router)
