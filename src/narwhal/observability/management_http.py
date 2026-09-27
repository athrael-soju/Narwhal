"""Read bounded monitoring responses from registered HTTP origins."""

from __future__ import annotations

import asyncio
import json
import math
import os
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx

from narwhal.deployment.management_access import AccessError, now
from narwhal.deployment.management_registry import ManagementTarget
from narwhal.diagnostics.bundle import Redactor

MAX_HTTP_BYTES = 8_388_608
MAX_STATUS_BYTES = 262_144


def endpoint(target: ManagementTarget, name: str) -> str:
    """Resolve one registered endpoint while rejecting credentials and redirects."""
    reference = getattr(target.endpoints, name + "_env")
    value = os.environ.get(reference, "") if reference else ""
    return origin(value)


def origin(value: str) -> str:
    """Validate an HTTP origin without accepting arbitrary URL suffixes."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError
        _ = parsed.port
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise AccessError(
            "source_unavailable", "Registered monitoring endpoint is invalid"
        ) from None
    return value.rstrip("/")


def _constant(value: str) -> None:
    raise ValueError("Invalid JSON number")


def _number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Invalid JSON number")
    return number


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    source: str,
    *,
    deadline: float,
    redactor: Redactor,
    maximum: int = MAX_STATUS_BYTES,
    params: dict[str, str] | None = None,
    seconds: float = 5,
) -> dict[str, Any]:
    """Retain partial evidence within a streaming byte cap and total source deadline."""
    row: dict[str, Any] = {
        "source": source,
        "observed_at": now(),
        "status": "ok",
        "http_status": None,
        "data": None,
        "artifact_id": None,
    }
    body = bytearray()
    remaining = min(seconds, deadline - monotonic())
    try:
        if remaining <= 0:
            raise TimeoutError
        async with (
            asyncio.timeout(remaining),
            client.stream(
                "GET",
                url,
                params=params,
                timeout=remaining,
                headers={"Accept-Encoding": "identity"},
            ) as response,
        ):
            row["http_status"] = response.status_code
            # A readiness refusal is a successful observation of failed readiness.
            if (response.is_redirect or not response.is_success) and not (
                source == "router_ready" and response.status_code == 503
            ):
                row.update(status="unavailable", error_code="source_unavailable")
            if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
                row.update(status="error", error_code="unsupported_media_type")
                return row
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                available = maximum - len(body)
                body.extend(chunk[:available])
                if len(chunk) > available:
                    row.update(status="truncated", error_code="source_truncated")
                    break
    except (TimeoutError, httpx.TimeoutException):
        row.update(status="timeout", error_code="source_unavailable")
    except httpx.HTTPError:
        row.update(status="unavailable", error_code="source_unavailable")
    if body:
        try:
            async with asyncio.timeout(max(0, deadline - monotonic())):
                safe = await asyncio.to_thread(redactor.body, bytes(body))
        except TimeoutError:
            row.update(status="timeout", error_code="source_unavailable")
            return row
        except (ValueError, UnicodeError, RecursionError):
            row.update(status="error", error_code="invalid_source")
            return row
        if len(safe) > maximum:
            safe = safe[:maximum].decode("utf-8", errors="ignore").encode()
            row.update(status="truncated", error_code="source_truncated")
        if row["status"] != "ok":
            row["raw_body"] = safe
        else:
            try:
                row["data"] = json.loads(safe, parse_constant=_constant, parse_float=_number)
            except (ValueError, UnicodeError, RecursionError):
                if source == "prometheus_ready":
                    row["data"] = safe.decode("utf-8", errors="replace")
                else:
                    row.update(status="error", error_code="invalid_source", raw_body=safe)
    elif row["status"] == "ok":
        row.update(status="error", error_code="invalid_source")
    return row
