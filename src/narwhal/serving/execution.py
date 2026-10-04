"""Execute bounded prefill/decode attempts under one original lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from ..engines.client import EngineError, first_output_timeout
from ..engines.connector import HandoffExpired, PrefillResult
from ..engines.stream import SseEvent, rewrite_sse, sse_token_bearing, sse_token_ids
from ..runtime.standby import control_ready
from ..scheduling.scheduler.occupancy import decode_admits
from ..types import Instance, Phase, Role
from .admission import PlacementRefused, QueueExpired
from .completion import reassemble
from .lifecycle import RequestExpired, RequestLifecycle
from .records import forward_headers, refuse_request
from .response import RelayBatch, RequestStreamResponse
from .retry import leg_failure_reason

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter


class NoEngine(Exception):
    """No eligible engine can receive this phase in queue-free serving."""


class ResponseLimitExceeded(ValueError):
    """A response exceeds local retention policy, without backend failure evidence."""


@dataclass
class PreparedAttempt:
    """A fresh producer handoff and its reserved decode destination."""

    prefill: Instance
    decode: Instance
    kv: PrefillResult
    expires_at: float | None = None


def request_error(router: NarwhalRouter, body: dict[str, Any]) -> JSONResponse | None:
    """Reject unsupported models and sampling widths before recording demand."""
    asked = body.get("model")
    if asked and asked != router.cfg.model:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"model {asked!r} is not served here; "
                    f"this router serves {router.cfg.model!r}",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )
    for field in ("n", "best_of"):
        if int(body.get(field) or 1) > 1:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"{field} > 1 cannot be served across a split: the prefill leg "
                        "runs with max_tokens=1 and the two legs would disagree on sampling width",
                        "type": "invalid_request_error",
                    }
                },
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
    state: RequestLifecycle, *, prefill: bool, handoff_deadline: float | None = None
) -> Instance:
    router, req = state.router, state.request
    if prefill and not control_ready(router):
        raise NoEngine("router control is fenced")
    req.phase = Phase.PREFILL if prefill else Phase.DECODE
    if router.cfg.serving.queue_capacity:
        state.phase = "queue"
        began = router._clock()
        try:
            deadline = (
                min(state.deadline, handoff_deadline)
                if handoff_deadline is not None
                else state.deadline
            )
            inst = await state.wait(lambda: router.dispatcher.place(req, deadline=deadline))
            return inst
        except QueueExpired as exc:
            if (
                handoff_deadline is not None
                and router._clock() >= handoff_deadline < state.deadline
            ):
                raise HandoffExpired(
                    "KV handoff expired while waiting for decode capacity"
                ) from exc
            raise
        finally:
            state.queue_wait_s += router._clock() - began
    if router.lifecycle_blocked:
        raise NoEngine(router.lifecycle_blocked)
    try:
        return router.scheduler.schedule(req)
    except RuntimeError as exc:
        raise NoEngine(
            "no schedulable engines" + (" after prefill" if not prefill else "")
        ) from exc


async def _prepare_once(
    state: RequestLifecycle,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> PreparedAttempt:
    router, req = state.router, state.request
    req.prefill_instance = None
    for role in (Role.PREFILL, Role.DECODE):
        if not router.scheduler.role_placeable(role):
            raise NoEngine(f"no schedulable engines for the {role.value} role")
    prefill = await _place(state, prefill=True)
    if router.cfg.admission == "predictive":
        priced = router.scheduler.prefill_admission_price(req, prefill)
        priced += max(0.0, router._clock() - state.arrived)
        if not router.scheduler.meets_slo(
            req, (0.0, priced), ttft_margin=router.cfg.admission_margin
        ):
            raise PlacementRefused(priced)
        if not decode_admits(
            router.scheduler,
            req,
            ready_s=router.scheduler.prefill_ready_s(req, prefill),
            ttft_s=priced,
            ttft_margin=router.cfg.admission_margin,
            concurrency=router.cfg.serving.decode_concurrency,
            expected_output=router.controller.demand.output_estimator(),
        ):
            raise PlacementRefused(priced, decode=True)
    state.phase = "prefill"
    state.begin_attempt()
    state.prefill_iid = prefill.iid
    state.reserve(prefill)
    began = router._clock()
    try:
        kv = await state.wait(
            lambda: router.engines.prefill(
                prefill.url, endpoint, body, state.engine_headers(headers, "prefill")
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
    handoff_s = router.cfg.serving.handoff_timeout_s
    # The handoff age counts from local prefill dispatch, before the remote lease starts.
    expires_at = began + handoff_s if handoff_s else None
    if expires_at is not None and router._clock() >= expires_at:
        raise HandoffExpired("prefill consumed the configured KV handoff age allowance")
    decode = await _place(state, prefill=False, handoff_deadline=expires_at)
    state.reserve(decode)
    state.decode_iid = decode.iid
    state.phase = "decode"
    return PreparedAttempt(prefill, decode, kv, expires_at)


async def prepare_attempt(
    state: RequestLifecycle,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> PreparedAttempt:
    """Prepare fresh ownership; transient prefill failures share the request budget."""
    while True:
        try:
            return await _prepare_once(state, endpoint, body, headers)
        except Exception as exc:
            state.release()
            if not await state.retry(exc):
                raise


def _terminal_failure(state: RequestLifecycle, exc: Exception) -> JSONResponse:
    """Settle the request and keep its error response for streamed and buffered replies."""
    response = (
        refuse_request(state, exc.predicted_s, decode=exc.decode)
        if isinstance(exc, PlacementRefused)
        else _failure(state, exc)
    )
    state.outcome["response"] = response
    return response


def _failure(state: RequestLifecycle, exc: Exception) -> JSONResponse:
    status = _status_of(exc)
    expired = isinstance(exc, RequestExpired | QueueExpired)
    detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
    kind = "expired" if expired else state.phase
    if isinstance(exc, NoEngine):
        kind = "backend_unavailable" if state.attempts else "no_schedulable_engines"
    if isinstance(exc, NoEngine):
        public_detail = "Engine capacity is unavailable"
    elif isinstance(exc, RequestExpired | QueueExpired | HandoffExpired):
        public_detail = "Request deadline expired"
    elif isinstance(exc, ResponseLimitExceeded):
        public_detail = "Response exceeds the configured limit"
    elif isinstance(exc, ValueError):
        public_detail = "Invalid non-streaming upstream response"
    else:
        public_detail = "Upstream request failed"
    state.finish("expired" if expired else "failed", error=detail, status=status)
    state.outcome["public_error"] = public_detail
    return JSONResponse(
        status_code=status,
        headers={"retry-after": "1"} if isinstance(exc, NoEngine) else None,
        content={"error": {"message": public_detail, "type": kind, "code": kind}},
    )


def _failure_response(state: RequestLifecycle) -> JSONResponse:
    response: JSONResponse = state.outcome["response"]
    return response


async def serve_request(
    state: RequestLifecycle,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> StreamingResponse | JSONResponse:
    """Size an admitted request, prepare its first attempt and own its response."""
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
    if router.cfg.engine_restart_policy == "whole_wave" and router.lifecycle_blocked:
        raise NoEngine("whole-wave restart hold")
    if prepared.expires_at is not None and router._clock() >= prepared.expires_at:
        raise HandoffExpired("KV handoff expired before decode dispatch")
    state.phase = "decode"
    state.decode_attempts += 1
    router.decode_attempts += 1
    dialect = router.engines.dialect
    expose_token_ids = bool(body.get("return_token_ids"))
    engine_body = body
    if dialect.token_ids:
        engine_body = {**body, "return_token_ids": True, "stream_interval": 1}
    upstream = router.engines.decode(
        prepared.decode.url,
        endpoint,
        engine_body,
        state.engine_headers(headers, "decode"),
        prepared.kv,
        first_token_timeout_s=router.cfg.first_token_timeout_s,
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
                    frame = rewrite_sse(event, expose_token_ids=expose_token_ids) + "\n\n"
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
        router.scheduler.record_answer(prepared.decode.iid, "decode")
    except Exception as exc:
        _failed_leg(state, prepared.decode, exc, decode=True, started=began)
        raise
    finally:
        try:
            await upstream.aclose()
        finally:
            state.record_upstream_time("decode", began)


async def run_decode(
    state: RequestLifecycle,
    prepared: PreparedAttempt,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    streaming: bool = False,
) -> AsyncGenerator[RelayBatch, None]:
    """Retry complete attempts until output commits; settle the original once."""
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
