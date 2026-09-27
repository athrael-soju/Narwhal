"""Read bounded router observations without changing fleet state."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx

from narwhal.contracts import LIFECYCLE, STATE, ContractVersionError, validate_document

from .bundle import Redactor

ROUTES = ("/health", "/ready", "/narwhal/state", "/narwhal/lifecycle")
MAX_SOURCE_BYTES = 262_144
SOURCE_TIMEOUT_S = 5.0
_VERSIONED_ROUTES = {"/narwhal/state": STATE, "/narwhal/lifecycle": LIFECYCLE}


class _NonFiniteJSON(ValueError):
    pass


def _number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonFiniteJSON
    return parsed


def _constant(value: str) -> None:
    raise _NonFiniteJSON


def _origin(router_url: str) -> str:
    try:
        parsed = urlsplit(router_url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in router_url)
        )
        _ = parsed.port
        httpx.URL(router_url)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        raise ValueError("router requires an HTTP(S) URL without credentials, query or fragment")
    return router_url.rstrip("/")


def _observation(route: str) -> dict[str, Any]:
    return {
        "source": route,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "ok",
        "http_status": None,
        "data": None,
        "artifact_id": None,
    }


def _failure(row: dict[str, Any], status: str, code: str, message: str) -> None:
    row.update(status=status, error_code=code, error=message)


def _retain_body(row: dict[str, Any], body: bytearray, redactor: Redactor) -> None:
    if row["http_status"] is None:
        return
    redacted = redactor.body(bytes(body))
    decoded = redacted.decode("utf-8")
    invalid_number = False
    try:
        document = json.loads(decoded, parse_constant=_constant, parse_float=_number)
    except _NonFiniteJSON:
        document = None
        invalid_number = True
    except ValueError:
        document = None
    row["data"] = document if isinstance(document, dict) else decoded
    if row["status"] == "truncated":
        # The adapter exports these already-redacted bytes and removes this private field.
        row["raw_body"] = redacted
    if row["status"] == "ok" and row["source"] in _VERSIONED_ROUTES:
        try:
            validate_document(document, _VERSIONED_ROUTES[row["source"]])
        except ContractVersionError:
            _failure(
                row,
                "error",
                "unsupported_contract",
                "Router response has an unsupported or missing document contract",
            )
    elif row["status"] == "ok" and invalid_number:
        _failure(
            row,
            "error",
            "source_unavailable",
            "Router response contains invalid JSON numbers",
        )


async def _observe(
    client: httpx.AsyncClient,
    base: str,
    route: str,
    row: dict[str, Any],
    deadline: float,
    redactor: Redactor,
) -> None:
    remaining = min(SOURCE_TIMEOUT_S, deadline - monotonic())
    if remaining <= 0:
        _failure(row, "timeout", "source_unavailable", "Router collection deadline expired")
        return
    body = bytearray()
    try:
        async with (
            asyncio.timeout(remaining),
            client.stream("GET", base + route, timeout=remaining) as response,
        ):
            row["http_status"] = response.status_code
            if response.is_redirect:
                _failure(
                    row, "unavailable", "source_unavailable", "Router redirect was not followed"
                )
            elif not response.is_success and not (
                route == "/ready" and response.status_code == 503
            ):
                _failure(row, "unavailable", "source_unavailable", "Router endpoint is unavailable")
            async for chunk in response.aiter_bytes():
                available = MAX_SOURCE_BYTES - len(body)
                body.extend(chunk[:available])
                if len(chunk) > available:
                    _failure(
                        row, "truncated", "source_truncated", "Router source byte limit reached"
                    )
                    break
    except (TimeoutError, httpx.TimeoutException):
        _failure(row, "timeout", "source_unavailable", "Router source deadline expired")
    except httpx.RequestError:
        _failure(row, "unavailable", "source_unavailable", "Router source could not be read")
    try:
        _retain_body(row, body, redactor)
    except RecursionError:
        row["data"] = None
        row.pop("raw_body", None)
        _failure(row, "error", "source_unavailable", "Router response exceeds JSON nesting limits")


async def observe_router(
    router_url: str,
    *,
    timeout_s: float = 30,
    freshness_s: int = 60,
    redactor: Redactor,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Collect four GET observations with per-source and total execution bounds.

    The endpoints expose no top-level capture timestamp. Observation time is the
    GET's start time; freshness uses its elapsed age when this collection ends.
    Nested event and engine-start timestamps do not describe snapshot freshness.
    Incomplete bodies preserve the received HTTP status and redacted prefix.
    ``raw_body`` is an internal redacted export payload for truncated sources.
    """
    base = _origin(router_url)
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
        raise ValueError("timeout_s must be finite, positive and at most 30 seconds")
    if type(freshness_s) is not int or not 1 <= freshness_s <= 3600:
        raise ValueError("freshness_s must be an integer from 1 through 3600")
    deadline = monotonic() + timeout_s
    observations: list[tuple[dict[str, Any], float]] = []
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=SOURCE_TIMEOUT_S,
    ) as client:
        for route in ROUTES:
            row = _observation(route)
            started = monotonic()
            observations.append((row, started))
            await _observe(client, base, route, row, deadline, redactor)
    ended = monotonic()
    for row, started in observations:
        if row["status"] == "ok" and ended - started > freshness_s:
            _failure(
                row, "stale", "source_stale", "Router observation exceeded its freshness limit"
            )
    return [row for row, _ in observations]
