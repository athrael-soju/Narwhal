"""Bounded reason labels, client error types and error responses for request outcomes."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.responses import JSONResponse

from ..engines.client import EngineError
from ..engines.connector import HandoffExpired
from .admission import PlacementRefused, QueueExpired


class RequestExpired(Exception):
    """The original request exhausted its end-to-end deadline."""


class NoEngine(Exception):
    """No eligible engine can receive this phase in queue-free serving."""


class RouterHeld(Exception):
    """A lifecycle hold or a router readiness fence ends a waiting or prefilled request.

    The message is the readiness reason the 503 response carries.
    """


class ResponseLimitExceeded(ValueError):
    """A response exceeds local retention policy, without backend failure evidence."""


# Every value appears on `/metrics` from process start, so each counter's series stay fixed.
FAILED_REASONS = (
    "no_engine",
    "engine_unreachable",
    "engine_connection",
    "engine_timeout",
    "engine_overloaded",
    "engine_rejected",
    "engine_error",
    "local_pool",
    "invalid_response",
    "response_limit",
    "internal",
)
REJECTED_REASONS = ("inflight_limit", "saturated", "not_ready", "unclassified")
# The one message of every 429 at the router in-flight limit.
INFLIGHT_LIMIT_MESSAGE = "router in-flight limit reached"
EXPIRED_REASONS = ("deadline", "queue_timeout", "handoff")
REFUSED_CAUSES = ("queue", "prompt", "aggregate_unpriced", "slot_wait", "kv_capacity", "tpot")
OUTCOME_REASONS = {
    "failed": FAILED_REASONS,
    "rejected": REJECTED_REASONS,
    "expired": EXPIRED_REASONS,
    "refused": REFUSED_CAUSES,
}
# Request phases whose name is the error type of an engine fault before output starts.
FAULT_PHASES = ("admission", "prefill", "decode")
# Every HTTP status and error `type` a completion route answers before output starts.
# The HTTP API error tables list exactly these pairs.
ERROR_RESPONSES = frozenset(
    {
        (400, "invalid_request_error"),
        (404, "invalid_request_error"),
        (413, "request_too_large"),
        (429, "server_overloaded_error"),
        (503, "standby"),
        (503, "backend_unavailable"),
        (503, "no_schedulable_engines"),
        (504, "queue_expired"),
        (504, "request_expired"),
        (504, "expired"),
        (504, "handoff_expired"),
        *((status, phase) for status in (502, 504) for phase in FAULT_PHASES),
    }
)


def error_response(
    status: int,
    error_type: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> JSONResponse:
    """Return a completion route's error body for a registered status and error type.

    `fields` adds body fields such as `code` and `param`. An unregistered pair raises
    ValueError, so the request ends as an internal router error.
    """
    if (status, error_type) not in ERROR_RESPONSES:
        raise ValueError(f"unregistered error response: HTTP {status} {error_type}")
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={"error": {"message": message, "type": error_type, **fields}},
    )


def failure_reason(exc: BaseException, *, deadline_passed: bool) -> str:
    """Classify an exception that ends an attempt or a request."""
    if isinstance(exc, RequestExpired):
        return "deadline"
    if isinstance(exc, QueueExpired):
        return "deadline" if deadline_passed or exc.at_deadline else "queue_timeout"
    if isinstance(exc, PlacementRefused):
        return exc.cause
    if isinstance(exc, NoEngine):
        return "no_engine"
    if isinstance(exc, RouterHeld):
        return "not_ready"
    if isinstance(exc, HandoffExpired):
        return "handoff"
    if isinstance(exc, ResponseLimitExceeded):
        return "response_limit"
    if isinstance(exc, EngineError):
        cause = exc.__cause__
        # A wrapped transport failure keeps its own classification.
        if isinstance(cause, httpx.HTTPError | TimeoutError):
            return failure_reason(cause, deadline_passed=deadline_passed)
        if exc.status in (408, 429):
            return "engine_overloaded"
        if exc.status == 504:
            return "engine_timeout"
        if 400 <= exc.status < 500:
            return "engine_rejected"
        return "engine_error"
    if isinstance(exc, httpx.PoolTimeout):
        return "local_pool"
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return "engine_unreachable"
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "engine_timeout"
    if isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError):
        return "engine_connection"
    if isinstance(exc, ValueError):
        return "invalid_response"
    return "internal"
