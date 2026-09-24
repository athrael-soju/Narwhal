"""Execute bounded prefill/decode attempts under one original lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from ..engines.client import (
    FIRST_OUTPUT_DETAIL,
    EngineError,
)
from ..engines.connector import HandoffExpired, PrefillResult
from ..engines.stream import rewrite_sse, sse_token_bearing, sse_token_ids
from ..runtime.standby import control_ready
from ..types import Instance, Phase, Request
from .admission import PlacementRefused, QueueExpired
from .completion import reassemble
from .lifecycle import RequestExpired, RequestLifecycle
from .records import forward_headers, refuse_request
from .response import RequestStreamResponse
from .retry import leg_failure_reason

if TYPE_CHECKING:
    from .router import NarwhalRouter


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


def _failed_leg(state: RequestLifecycle, inst: Instance, exc: Exception, *, decode: bool) -> None:
    if isinstance(exc, RequestExpired | ResponseLimitExceeded):
        return
    router = state.router
    router._leg_failed(
        inst.iid, exc, prefill_iid=state.prefill_iid if decode else None, decode_leg=decode
    )
    reason = leg_failure_reason(exc)
    if decode and isinstance(exc, EngineError) and exc.detail.startswith(FIRST_OUTPUT_DETAIL):
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
    prefill = await _place(state, prefill=True)
    if router.cfg.admission == "predictive":
        cost = router.scheduler.cost(req, prefill)
        priced = router.scheduler.prefill_admission_price(req, prefill)
        priced += max(0.0, router._clock() - state.arrived)
        if not router.scheduler.meets_slo(
            req, (cost[0], priced), ttft_margin=router.cfg.admission_margin
        ):
            raise PlacementRefused(priced)
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
        _failed_leg(state, prefill, exc, decode=False)
        raise
    finally:
        state.record_upstream_time("prefill", began)
    state.prefilled_at = router._clock()
    router.scheduler.record_answer(prefill.iid, "prefill")
    router.monitor.first_token(prefill.iid, req.rid)
    handoff_s = router.cfg.serving.handoff_timeout_s
    # The remote lease starts during the prefill HTTP call. Starting the age
    # at local dispatch includes that delay and uses one monotonic clock.
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
    if isinstance(exc, PlacementRefused):
        return refuse_request(state, exc.predicted_s)
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


async def serve_request(
    router: NarwhalRouter,
    rid: str,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    arrived: float | None = None,
    offered: bool = False,
    lifecycle: RequestLifecycle | None = None,
) -> StreamingResponse | JSONResponse:
    """Size an original request, prepare its first attempt and own its response."""
    invalid = request_error(router, body)
    if invalid is not None:
        return invalid
    arrived = router._clock() if arrived is None else arrived
    req = Request(
        rid=rid,
        input_len=router.estimate_length(body),
        wanted_len=int(body.get("max_tokens") or 0),
    )
    state = lifecycle or RequestLifecycle(
        router, req, arrived, client_rid=headers.get("x-request-id")
    )
    req = state.request
    body = {**body, "model": router.cfg.model}
    engine_headers = forward_headers(headers)
    try:
        req.input_len = await state.wait(lambda: router.input_length(body))
        if state.demand_observation is not None:
            router.controller.demand.resize_arrival(
                state.demand_observation, req.input_len, req.wanted_len, at=arrived
            )
            state.demand_observation = None
        if not offered:
            router.controller.saw_arrival(req.input_len, wanted_len=req.wanted_len, at=arrived)
        prepared = await prepare_attempt(state, endpoint, body, engine_headers)
    except asyncio.CancelledError:
        state.finish("cancelled")
        raise
    except Exception as exc:
        return _terminal_failure(state, exc)
    streaming = bool(body.get("stream"))
    stream = run_decode(state, prepared, endpoint, body, engine_headers, streaming=streaming)
    if streaming:
        return RequestStreamResponse(stream, state)
    chunks = [line async for line in stream]
    if state.outcome["error"] is not None:
        return JSONResponse(
            status_code=state.outcome["status"],
            content={
                "error": {
                    "message": state.outcome["public_error"],
                    "type": state.phase,
                }
            },
        )
    try:
        out = reassemble(chunks, endpoint=endpoint)
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
) -> AsyncGenerator[str, None]:
    router = state.router
    # A lost router lease fences new prefills. An already dispatched original
    # may drain its decode leg, preserving the warm-standby serving contract.
    if router.cfg.engine_restart_policy == "whole_wave" and router.lifecycle_blocked:
        raise NoEngine("whole-wave restart hold")
    if prepared.expires_at is not None and router._clock() >= prepared.expires_at:
        raise HandoffExpired("KV handoff expired before decode dispatch")
    state.phase = "decode"
    state.decode_attempts += 1
    router.decode_attempts += 1
    engine_body = body
    if router.engines.dialect.token_ids:
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
    metadata_bytes = 0
    began = router._clock()
    try:
        while True:
            try:
                line = await state.wait(lambda: anext(upstream))
            except StopAsyncIteration:
                break
            exact = router.engines.dialect.token_ids
            ids = sse_token_ids(line) if exact else None
            if exact and ids is None:
                raise EngineError(
                    "decode", prepared.decode.url, 502, "decode output lacks valid token_ids"
                )
            n = len(ids) if ids is not None else 0
            progress = sse_token_bearing(line, router.engines.dialect)
            if progress:
                now = router._clock()
                if state.first_at is None:
                    state.first_at = now
                state.last_at = now
            for _ in range(n):
                router.monitor.output_token(prepared.decode.iid, state.rid)
            state.tokens += n
            state.decode_tokens_observed += n
            router.decode_tokens_observed += n
            frame = rewrite_sse(line, expose_token_ids=bool(body.get("return_token_ids"))) + "\n\n"
            if state.first_at is None:
                # Buffer role/usage metadata until output commits the attempt.
                # Discard keepalives while the attempt is uncommitted.
                if line.startswith("data:"):
                    metadata_bytes += len(frame.encode())
                    # Prompt identity can exceed 64 KiB for ordinary long
                    # documents. Charge it to the explicit retained-response
                    # budget, including streams that must buffer before output.
                    if (
                        len(metadata) >= 64
                        or metadata_bytes > router.cfg.serving.max_response_bytes
                    ):
                        raise ResponseLimitExceeded(
                            "pre-output metadata exceeds serving.max_response_bytes "
                            "or the 64-frame limit"
                        )
                    metadata.append(frame)
                continue
            for prior in metadata:
                yield prior
            metadata.clear()
            yield frame
        router.scheduler.record_answer(prepared.decode.iid, "decode")
    except Exception as exc:
        _failed_leg(state, prepared.decode, exc, decode=True)
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
) -> AsyncGenerator[str, None]:
    """Retry complete attempts until output commits; settle the original once."""
    try:
        while True:
            chunks: list[str] = []
            size = 0
            attempt = _decode_attempt(state, prepared, endpoint, body, headers)
            try:
                async for frame in attempt:
                    if streaming:
                        state.output_started = True
                        yield frame
                    else:
                        size += len(frame.encode())
                        if size > state.router.cfg.serving.max_response_bytes:
                            raise ResponseLimitExceeded(
                                "response exceeds serving.max_response_bytes"
                            )
                        chunks.append(frame)
            except Exception as exc:
                state.release()
                if not await state.retry(exc):
                    raise
                prepared = await prepare_attempt(state, endpoint, body, headers)
                continue
            finally:
                await attempt.aclose()
            if not streaming:
                for frame in chunks:
                    yield frame
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
            yield (
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
