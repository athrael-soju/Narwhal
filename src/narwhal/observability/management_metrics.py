"""Execute fixed registered Prometheus queries within documented result bounds."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

from narwhal.deployment.management_access import AccessError, now
from narwhal.deployment.management_registry import ManagementQuery
from narwhal.diagnostics.bundle import Redactor

from .management_http import MAX_HTTP_BYTES, fetch, origin
from .management_status import timestamp


def query_parameters(
    query: ManagementQuery,
    *,
    start: str | None,
    end: str | None,
    step_s: int | None,
    limit_series: int,
    at: datetime | None = None,
) -> dict[str, str]:
    """Validate query mode, historical bounds and exact integer limits before HTTP access."""
    at = at or datetime.now(UTC)
    if type(limit_series) is not int or not 1 <= limit_series <= 100:
        raise AccessError("invalid_input", "Metric series limit is invalid")
    parameters = {"query": query.expression}
    if query.kind == "instant":
        if start is not None or end is not None or step_s is not None:
            raise AccessError("invalid_input", "Instant queries do not accept range arguments")
        return parameters
    try:
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError
        if not start.endswith("Z") or not end.endswith("Z"):
            raise ValueError
        first, last = timestamp(start), timestamp(end)
        if not 0 < (last - first).total_seconds() <= 3600 or last > at:
            raise ValueError
        step = 15 if step_s is None else step_s
        if type(step) is not int or not 1 <= step <= 300:
            raise ValueError
    except (ValueError, TypeError, OverflowError, OSError):
        raise AccessError("invalid_input", "Metric query range or step is invalid") from None
    parameters.update(start=start, end=end, step=str(step))
    return parameters


def _sample(value: Any) -> dict[str, str]:
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        raise ValueError("Invalid sample")
    if type(value[0]) not in {int, float} or not math.isfinite(value[0]):
        raise ValueError("Invalid sample timestamp")
    # Prometheus numeric strings, including NaN and infinities, remain strings.
    return {"at": timestamp(value[0]).isoformat().replace("+00:00", "Z"), "value": value[1]}


def metric_series(
    document: dict[str, Any], limit_series: int
) -> tuple[str, list[dict[str, Any]], bool]:
    """Normalize supported response types, retaining at most 10,000 samples."""
    if document.get("status") != "success":
        raise ValueError("Prometheus did not return a successful query")
    kind = document["data"]["resultType"]
    values = document["data"]["result"]
    if kind in {"scalar", "string"}:
        return kind, [{"labels": {}, "samples": [_sample(values)]}], True
    if kind not in {"vector", "matrix"} or not isinstance(values, list):
        raise ValueError("Unsupported Prometheus result type")
    result = []
    remaining = 10_000
    complete = len(values) <= limit_series
    for entry in values[:limit_series]:
        labels = entry["metric"]
        if not isinstance(labels, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
        ):
            raise ValueError("Invalid metric labels")
        samples = [entry["value"]] if kind == "vector" else entry["values"]
        if not isinstance(samples, list):
            raise ValueError("Invalid metric samples")
        if len(samples) > remaining:
            complete = False
        selected = [_sample(value) for value in samples[:remaining]]
        remaining -= len(selected)
        result.append({"labels": labels, "samples": selected})
    return kind, result, complete


async def query_metrics(
    query: ManagementQuery,
    *,
    prometheus_url: str,
    start: str | None = None,
    end: str | None = None,
    step_s: int | None = None,
    limit_series: int = 100,
    deadline: float,
    freshness_s: int,
    redactor: Redactor,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Return normalized data, retained source observation and explicit incomplete causes."""
    params = query_parameters(query, start=start, end=end, step_s=step_s, limit_series=limit_series)
    remaining = max(0.001, deadline - monotonic())
    params["timeout"] = f"{remaining:.3f}s"
    route = "/api/v1/query" if query.kind == "instant" else "/api/v1/query_range"
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        observation = await fetch(
            client,
            origin(prometheus_url) + route,
            "metrics_query",
            params=params,
            deadline=deadline,
            redactor=redactor,
            maximum=MAX_HTTP_BYTES,
            seconds=remaining,
        )
    data: dict[str, Any] = {
        "result_type": "unavailable",
        "series": [],
        "complete": False,
        "observed_at": now(),
    }
    errors = []
    if observation["status"] != "ok":
        errors.append(
            {
                "code": observation.get("error_code", "source_unavailable"),
                "message": "Metric query evidence is incomplete",
                "context": {},
            }
        )
        return data, observation, errors
    try:
        document = observation["data"]
        kind, series, complete = metric_series(document, limit_series)
        warnings = document.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ValueError("Invalid query warnings")
        if warnings:
            complete = False
            errors.append(
                {
                    "code": "query_warning",
                    "message": "Prometheus returned warnings",
                    "context": {"warnings": warnings},
                }
            )
        if not complete and not warnings:
            errors.append(
                {
                    "code": "result_truncated",
                    "message": "Metric result limit reached",
                    "context": {},
                }
            )
        if not series or not any(row["samples"] for row in series):
            complete = False
            errors.append(
                {"code": "source_missing", "message": "Query returned no samples", "context": {}}
            )
        if (
            datetime.now(UTC) - timestamp(observation["observed_at"])
        ).total_seconds() > freshness_s:
            complete = False
            observation.update(status="stale", error_code="source_stale")
            errors.append(
                {"code": "source_stale", "message": "Metric observation is stale", "context": {}}
            )
        if query.kind == "instant" and any(
            not 0 <= (datetime.now(UTC) - timestamp(sample["at"])).total_seconds() <= freshness_s
            for row in series
            for sample in row["samples"]
        ):
            complete = False
            errors.append(
                {
                    "code": "source_stale",
                    "message": "Instant query samples are stale",
                    "context": {},
                }
            )
        data.update(result_type=kind, series=series, complete=complete)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
        observation.update(status="error", error_code="invalid_source")
        errors.append(
            {
                "code": "invalid_source",
                "message": "Prometheus query response is invalid",
                "context": {},
            }
        )
    return data, observation, errors
