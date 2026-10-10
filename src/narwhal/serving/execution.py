from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from ..engines.client import EngineError, first_output_timeout
from ..engines.connector import HandoffExpired, PrefillResult, RendezvousConnector
from ..engines.stream import SseEvent, rewrite_sse, sse_token_bearing, sse_token_ids
from ..scheduling.prefill import prefill_seconds
from ..scheduling.scheduler.occupancy import decode_refusal
from ..types import Instance, Phase, Role
from .admission import PlacementRefused, QueueExpired
from .completion import reassemble
from .dispatch import placement_hold
from .handoff import handoff_bound
from .lifecycle import RequestLifecycle
from .outcomes import (
    NoEngine,
    RequestExpired,
    ResponseLimitExceeded,
    RouterHeld,
    error_response,
    failure_reason,
)
from .records import (
    deadline_response,
    forward_headers,
    not_ready_response,
    refuse_request,
    ttft_refusal_cause,
)
from .response import RelayBatch, RequestStreamResponse
from .retry import leg_failure_reason
from .seats import decode_seat_map

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter


@dataclass
class PreparedAttempt:
    prefill: Instance
    decode: Instance
    kv: PrefillResult | None
    expires_at: float | None = None
    rendezvous: dict[str, Any] | None = None
    prefill_task: asyncio.Task[None] | None = None


def request_error(router: NarwhalRouter, body: dict[str, Any]) -> JSONResponse | None:
    asked = body.get("model")
    if asked and asked != router.cfg.model:
        return error_response(
            404,
            "invalid_request_error",
            f"model {asked!r} is not served here; this router serves {router.cfg.model!r}",
            code="model_not_found",
        )
    for field in ("n", "best_of"):
        if int(body.get(field) or 1) > 1:
            return error_response(
                400,
                "invalid_request_error",
                f"{field} > 1 cannot be served across a split: the prefill leg "
                "runs with max_tokens=1 and the two legs would disagree on sampling width",
            )
    return None


def _status_of(exc: BaseException) -> int:
    if isinstance(exc, RequestExpired | QueueExpired | HandoffExpired):
        return 504
    if isinstance(exc, NoEngine):
        return 503
    if isinstance(exc, EngineError):
        return 504 if exc.status in (408, 504) else 502
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return 504
    return 502


def _failed_leg(
    state: RequestLifecycle, inst: Instance, exc: Exception, *, decode: bool, started: float
) -> None:
    if isinstance(exc, RequestExpired | ResponseLimitExceeded):
        return
    router = state.router
    # A retry avoids every engine whose leg failed for this request.
    if decode:
        state.failed_engines["decode"].add(inst.iid)
        # Without output, the producer's KV handoff may be the fault.
        if not state.output_started and state.prefill_iid is not None:
            state.failed_engines["prefill"].add(state.prefill_iid)
    else:
        state.failed_engines["prefill"].add(inst.iid)
    router.verifier.leg_failed(
        inst.iid,
        exc,
        prefill_iid=state.prefill_iid if decode else None,
        decode_leg=decode,
        progressed=router.monitor.output_since(inst.iid, started),
    )
    reason = leg_failure_reason(exc)
    if decode and first_output_timeout(exc):
        router.controller.safety.note_risk_event("first_token_timeout")
    if reason is not None and reason != "local_pool":
        router.scheduler.quarantine(inst.iid, router.cfg.failure_quarantine_s)


async def _place(
    state: RequestLifecycle,
    *,
    prefill: bool,
    claim: Callable[[Instance], None],
    handoff_deadline: float | None = None,
) -> Instance:
    router, req = state.router, state.request
    phase = Phase.PREFILL if prefill else Phase.DECODE
    hold = placement_hold(router, phase)
    if hold:
        raise RouterHeld(hold)
    req.phase = phase
    failed = state.failed_engines["prefill" if prefill else "decode"]
    if router.cfg.serving.queue_capacity:
        state.phase = "queue"
        began = router._clock()
        deadline, wait_s = state.deadline, None
        if prefill:
            wait_s = state.queue_budget_s
        elif handoff_deadline is not None:
            deadline = min(deadline, handoff_deadline)
        try:
            return await state.wait(
                lambda: router.dispatcher.place(
                    req,
                    deadline=deadline,
                    claim=claim,
                    exclude=failed,
                    wait_s=wait_s,
                    check=(lambda: price_waiting(state)) if prefill else None,
                )
            )
        except QueueExpired as exc:
            # The wait timer can fire just before the bound on the router clock.
            if (
                exc.at_deadline
                and handoff_deadline is not None
                and handoff_deadline < state.deadline
            ):
                raise HandoffExpired(
                    "KV handoff bound reached while waiting for a decode seat"
                ) from exc
            raise
        finally:
            state.waited("prefill" if prefill else "decode", router._clock() - began)
    try:
        inst = router.scheduler.schedule(req, exclude=set(failed))
    except RuntimeError as exc:
        raise NoEngine(
            "no schedulable engines" + (" after prefill" if not prefill else "")
        ) from exc
    claim(inst)
    return inst


def _price_admission(state: RequestLifecycle, prefill: Instance) -> float:
    router, req = state.router, state.request
    placement = router.scheduler.prefill_admission_price(req, prefill)
    elapsed = max(0.0, router._clock() - state.arrived)
    profile = router.profiles.get(prefill.iid)
    own = prefill_seconds(profile, req) if profile is not None else None
    finite = math.isfinite(placement)
    state.admission_price = {
        "attempt": state.attempts + 1,
        # Resident prefill work and any probation penalty on the priced engine.
        "backlog_s": placement - own if finite and own is not None else None,
        "own_prefill_s": own,
        "elapsed_s": elapsed,
        "price_s": placement + elapsed if finite else None,
    }
    return placement + elapsed


def _check_ttft(state: RequestLifecycle, priced: float) -> None:
    router, req = state.router, state.request
    if not router.scheduler.meets_slo(req, (0.0, priced), ttft_margin=router.cfg.admission_margin):
        raise PlacementRefused(priced, cause=ttft_refusal_cause(router, req, priced))


def price_waiting(state: RequestLifecycle) -> float | None:
    router, req = state.router, state.request
    if router.cfg.admission != "predictive" or state.attempts:
        return None
    scheduler = router.scheduler
    candidates = scheduler.role_pool(
        Role.PREFILL, scheduler.live_instances(exclude=state.failed_engines["prefill"])
    )
    if not candidates:
        return None
    phase, req.phase = req.phase, Phase.PREFILL
    try:
        placements = {inst.iid: scheduler.prefill_admission_price(req, inst) for inst in candidates}
        cheapest = min(candidates, key=lambda inst: (placements[inst.iid], inst.iid))
        priced = _price_admission(state, cheapest)
        _check_ttft(state, priced)
    finally:
        req.phase = phase
    budget = router.cfg.slo.ttft_s * (1.0 + router.cfg.admission_margin)
    return state.arrived + budget - placements[cheapest.iid]


async def _prepare_once(
    state: RequestLifecycle,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> PreparedAttempt:
    router, req = state.router, state.request
    req.prefill_instance = None
    # A hold that drains engines answers before their absence does.
    hold = placement_hold(router, Phase.PREFILL)
    if hold:
        raise RouterHeld(hold)
    for role in (Role.PREFILL, Role.DECODE):
        if not router.scheduler.role_placeable(role):
            raise NoEngine(f"no schedulable engines for the {role.value} role")
        failed = state.failed_engines[role.value]
        if failed and not router.scheduler.role_pool(
            role, router.scheduler.live_instances(exclude=failed)
        ):
            raise NoEngine(f"no {role.value} engine remains after failed attempts")
    retry = state.attempts > 0

    def admit_prefill(prefill: Instance) -> None:
        # Price before the claim, so the engine's resident queue excludes this request.
        # Open admission records the same price without enforcing it.
        priced = _price_admission(state, prefill)
        if router.cfg.admission == "predictive":
            # A retry already passed the TTFT price; it rechecks decode as a fresh handoff.
            if not retry:
                _check_ttft(state, priced)
            check = decode_refusal(
                router.scheduler,
                req,
                ready_s=router.scheduler.prefill_ready_s(req, prefill),
                ttft_s=None if retry else priced,
                ttft_margin=router.cfg.admission_margin,
                seats=decode_seat_map(router),
                expected_output=router.controller.demand.output_estimator(),
            )
            if check is not None:
                raise PlacementRefused(priced, cause=check)
        state.reserve(prefill)

    prefill = await _place(state, prefill=True, claim=admit_prefill)
    if isinstance(router.engines.kv, RendezvousConnector):
        return await _prepare_rendezvous(state, router.engines.kv, prefill, endpoint, body, headers)
    state.phase = "prefill"
    state.begin_attempt()
    state.prefill_iid = prefill.iid
    began = router._clock()
    try:
        kv = await state.wait(
            lambda: router.engines.prefill(
                prefill.url, endpoint, body, headers, request_id=state.leg_id("prefill")
            )
        )
    except Exception as exc:
        _failed_leg(state, prefill, exc, decode=False, started=began)
        raise
    finally:
        state.record_upstream_time("prefill", began)
    state.prefilled_at = router._clock()
    router.scheduler.record_answer(prefill.iid, "prefill")
    router.monitor.first_token(prefill.iid, req.rid)
    # The producer's lease starts when prefill completes.
    bound = handoff_bound(router, prefill.iid)
    expires_at = None if bound is None else state.prefilled_at + bound
    decode = await _place(state, prefill=False, claim=state.reserve, handoff_deadline=expires_at)
    state.decode_iid = decode.iid
    state.phase = "decode"
    return PreparedAttempt(prefill, decode, kv, expires_at)


async def _prepare_rendezvous(
    state: RequestLifecycle,
    kv: RendezvousConnector,
    prefill: Instance,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> PreparedAttempt:
    router = state.router
    # The decode leg must be listening before the prefill leg pushes its KV.
    decode = await _place(state, prefill=False, claim=state.reserve)
    state.begin_attempt()
    state.prefill_iid, state.decode_iid = prefill.iid, decode.iid
    rendezvous = kv.rendezvous(router.launches.get(prefill.iid, {}))
    request_id = state.leg_id("prefill")

    async def run() -> None:
        began = router._clock()
        try:
            await state.wait(
                lambda: router.engines.rendezvous_prefill(
                    prefill.url, endpoint, body, headers, rendezvous, request_id=request_id
                )
            )
        except Exception as exc:
            _failed_leg(state, prefill, exc, decode=False, started=began)
            raise
        finally:
            state.record_upstream_time("prefill", began)
        state.prefilled_at = router._clock()
        router.scheduler.record_answer(prefill.iid, "prefill")
        router.monitor.first_token(prefill.iid, state.request.rid)

    state.phase = "decode"
    task = asyncio.create_task(run())
    return PreparedAttempt(prefill, decode, None, rendezvous=rendezvous, prefill_task=task)


async def _settle_prefill(prepared: PreparedAttempt, *, cancel: bool) -> BaseException | None:
    task = prepared.prefill_task
    if task is None:
        return None
    if cancel and not task.done():
        task.cancel()
    with suppress(Exception, asyncio.CancelledError):
        await task
    return None if task.cancelled() else task.exception()


async def prepare_attempt(
    state: RequestLifecycle,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> PreparedAttempt:
    while True:
        try:
            return await _prepare_once(state, endpoint, body, headers)
        except Exception as exc:
            state.release()
            if not await state.retry(exc):
                raise


def _terminal_failure(state: RequestLifecycle, exc: Exception) -> JSONResponse:
    if isinstance(exc, PlacementRefused):
        response = refuse_request(state, exc)
    elif isinstance(exc, RouterHeld):
        response = not_ready_response(state.router, state, reason=str(exc))
    else:
        response = _failure(state, exc)
    state.outcome["response"] = response
    return response


def _failure(state: RequestLifecycle, exc: Exception) -> JSONResponse:
    reason = failure_reason(exc, deadline_passed=state.router._clock() >= state.deadline)
    if reason == "deadline" and not state.output_started:
        # Ingress answers the same deadline; both give the client one body.
        return deadline_response(state)
    status = _status_of(exc)
    expired = isinstance(exc, RequestExpired | QueueExpired | HandoffExpired)
    detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
    kind = "expired" if expired else state.phase
    if isinstance(exc, HandoffExpired):
        kind = "handoff_expired"
    if isinstance(exc, NoEngine):
        kind = "backend_unavailable" if state.attempts else "no_schedulable_engines"
    if isinstance(exc, NoEngine):
        public_detail = "Engine capacity is unavailable"
    elif isinstance(exc, HandoffExpired):
        public_detail = "KV handoff expired before decode dispatch"
    elif isinstance(exc, RequestExpired | QueueExpired):
        public_detail = "Request deadline expired"
    elif isinstance(exc, ResponseLimitExceeded):
        public_detail = "Response exceeds the configured limit"
    elif isinstance(exc, ValueError):
        public_detail = "Invalid non-streaming upstream response"
    else:
        public_detail = "Upstream request failed"
    # An unregistered status and type raises here, before the outcome settles.
    response = error_response(
        status,
        kind,
        public_detail,
        headers={"retry-after": "1"} if isinstance(exc, NoEngine) else None,
        code=kind,
    )
    terminal = "expired" if expired else "failed"
    # After output starts, the stream's terminal event carries the phase and terminal state.
    error_type, error_code = (state.phase, terminal) if state.output_started else (kind, kind)
    state.finish(
        terminal,
        error=detail,
        status=status,
        reason=reason,
        error_type=error_type,
        error_code=error_code,
    )
    state.outcome["public_error"] = public_detail
    return response


def _failure_response(state: RequestLifecycle) -> JSONResponse:
    response: JSONResponse = state.outcome["response"]
    return response


async def serve_request(
    state: RequestLifecycle,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> StreamingResponse | JSONResponse:
    router, req = state.router, state.request
    body = {**body, "model": router.cfg.model}
    engine_headers = forward_headers(headers)
    try:
        sizing = router._clock()
        try:
            (
                req.input_len,
                req.cached_tokens,
                req.cache_sequences,
                req.cache_identities,
            ) = await state.wait(lambda: router.sizer.size(body))
            req.cache_checked_at = router._clock()
        finally:
            router.sizing_delays.add(router._clock() - sizing)
        # Prefill seats follow the mean sized input length.
        router.input_lengths.add(req.input_len)
        if state.demand_observation is not None:
            req.demand_arrival = router.controller.demand.resize_arrival(
                state.demand_observation,
                req.input_len,
                req.wanted_len,
                at=state.arrived,
                cached_tokens=req.cached_tokens,
            )
            state.demand_observation = None
        prepared = await prepare_attempt(state, endpoint, body, engine_headers)
    except asyncio.CancelledError:
        state.finish("cancelled")
        raise
    except Exception as exc:
        return _terminal_failure(state, exc)
    streaming = bool(body.get("stream"))
    stream = run_decode(state, prepared, endpoint, body, engine_headers, streaming=streaming)
    if streaming:
        try:
            first = await anext(stream, None)
        except BaseException:
            await stream.aclose()
            raise
        if not state.output_started and state.outcome["error"] is not None:
            await stream.aclose()
            return _failure_response(state)
        return RequestStreamResponse(stream, state, first=first)
    events = [event async for batch in stream for event in batch.events]
    if state.outcome["error"] is not None:
        return _failure_response(state)
    try:
        out = reassemble(events, endpoint=endpoint)
    except ValueError as exc:
        return _terminal_failure(state, exc)
    state.finish("completed")
    tokens = state.outcome.get("tokens")
    if tokens is not None:
        out.setdefault(
            "usage",
            {
                "prompt_tokens": req.input_len,
                "completion_tokens": tokens,
                "total_tokens": req.input_len + tokens,
            },
        )
    return JSONResponse(content=out)


async def _decode_attempt(
    state: RequestLifecycle,
    prepared: PreparedAttempt,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> AsyncGenerator[RelayBatch, None]:
    router = state.router
    # A lost router lease fences new prefills; a dispatched original may still decode.
    hold = placement_hold(router, Phase.DECODE)
    if hold:
        raise RouterHeld(hold)
    if prepared.expires_at is not None and router._clock() >= prepared.expires_at:
        raise HandoffExpired("KV handoff expired before decode dispatch")
    state.phase = "decode"
    state.decode_attempts += 1
    router.decode_attempts += 1
    dialect = router.engines.dialect
    strip = () if body.get("return_token_ids") else dialect.engine_output_fields
    engine_body = {**body, **dialect.token_id_fields()} if dialect.token_ids else body
    first_token_timeout_s = router.cfg.first_token_timeout_s
    kv = router.engines.kv
    if prepared.rendezvous is not None and isinstance(kv, RendezvousConnector):
        first_token_timeout_s = min(first_token_timeout_s or math.inf, kv.decode_wait_s)
    upstream = router.engines.decode(
        prepared.decode.url,
        endpoint,
        engine_body,
        headers,
        prepared.kv,
        first_token_timeout_s=first_token_timeout_s,
        request_id=state.leg_id("decode"),
        rendezvous=prepared.rendezvous,
    )
    metadata: list[str] = []
    held: list[SseEvent] = []
    metadata_bytes = 0
    began = router._clock()
    try:
        while True:
            try:
                batch = await state.wait(lambda: anext(upstream))
            except StopAsyncIteration:
                break
            frames: list[str] = []
            events: list[SseEvent] = []
            try:
                for event in batch:
                    ids = sse_token_ids(event) if dialect.token_ids else None
                    if dialect.token_ids and ids is None:
                        raise EngineError(
                            "decode",
                            prepared.decode.url,
                            502,
                            "decode output lacks valid token_ids",
                        )
                    n = len(ids) if ids is not None else 0
                    if sse_token_bearing(event, dialect):
                        now = router._clock()
                        if state.first_at is None:
                            state.first_at = now
                        state.last_at = now
                    for _ in range(n):
                        router.monitor.output_token(prepared.decode.iid, state.rid)
                    state.tokens += n
                    state.decode_tokens_observed += n
                    router.decode_tokens_observed += n
                    frame = rewrite_sse(event, strip=strip) + "\n\n"
                    if state.first_at is None:
                        # Before output commits the attempt, metadata buffers and keepalives drop.
                        if event.line.startswith("data:"):
                            metadata_bytes += len(frame.encode())
                            # Prompt token IDs can exceed 64 KiB; buffered metadata counts
                            # against max_response_bytes.
                            if (
                                len(metadata) >= 64
                                or metadata_bytes > router.cfg.serving.max_response_bytes
                            ):
                                raise ResponseLimitExceeded(
                                    "pre-output metadata exceeds serving.max_response_bytes "
                                    "or the 64-frame limit"
                                )
                            metadata.append(frame)
                            held.append(event)
                        continue
                    frames.extend(metadata)
                    events.extend(held)
                    metadata.clear()
                    held.clear()
                    frames.append(frame)
                    events.append(event)
            except Exception:
                if frames:
                    yield RelayBatch("".join(frames), events)
                raise
            if frames:
                yield RelayBatch("".join(frames), events)
        await _settle_prefill(prepared, cancel=False)
        router.scheduler.record_answer(prepared.decode.iid, "decode")
    except Exception as exc:
        # A decode leg without its prefill fails for the prefill's reason.
        cause = await _settle_prefill(prepared, cancel=True)
        if isinstance(cause, Exception):
            state.phase = "prefill"
            raise cause from exc
        _failed_leg(state, prepared.decode, exc, decode=True, started=began)
        raise
    finally:
        try:
            await upstream.aclose()
        finally:
            state.record_upstream_time("decode", began)
            await _settle_prefill(prepared, cancel=True)


async def run_decode(
    state: RequestLifecycle,
    prepared: PreparedAttempt,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    streaming: bool = False,
) -> AsyncGenerator[RelayBatch, None]:
    try:
        while True:
            buffered: list[RelayBatch] = []
            size = 0
            attempt = _decode_attempt(state, prepared, endpoint, body, headers)
            try:
                async for batch in attempt:
                    if streaming:
                        state.output_started = True
                        state.request.cache_identities = {}
                        yield batch
                    else:
                        size += len(batch.text.encode())
                        if size > state.router.cfg.serving.max_response_bytes:
                            raise ResponseLimitExceeded(
                                "response exceeds serving.max_response_bytes"
                            )
                        buffered.append(batch)
            except Exception as exc:
                state.release()
                if not await state.retry(exc):
                    raise
                prepared = await prepare_attempt(state, endpoint, body, headers)
                continue
            finally:
                await attempt.aclose()
            if not streaming:
                for batch in buffered:
                    yield batch
            # A buffered response succeeds only after endpoint-aware assembly.
            if streaming:
                state.finish("completed")
            return
    except (asyncio.CancelledError, GeneratorExit):
        state.finish("cancelled")
        raise
    except Exception as exc:
        _terminal_failure(state, exc)
        if streaming:
            yield RelayBatch(
                "data: "
                + json.dumps(
                    {
                        "error": {
                            "message": state.outcome["public_error"],
                            "type": state.phase,
                            "code": state.terminal,
                        }
                    }
                )
                + "\n\n"
            )
    finally:
        state.release()
