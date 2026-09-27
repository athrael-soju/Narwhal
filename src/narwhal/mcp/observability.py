"""Expose bounded monitoring, metric and registered-host inspection tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx
from pydantic import Field

from narwhal.deployment.management_access import AccessError, now
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementRegistry, ManagementTarget
from narwhal.observability.management_http import endpoint
from narwhal.observability.management_metrics import query_metrics, query_parameters
from narwhal.observability.management_site import RegisteredSiteProvider
from narwhal.observability.management_status import observe_monitoring, timestamp
from narwhal.observability.management_types import SiteProvider, SourceCapture

from .adapters import ToolAdapter
from .inspection import InspectionTools, TargetInput, _encoded, _error
from .results import Alias, Timestamp, result


class MetricsInput(TargetInput):
    """Select a registered query and its bounded historical window."""

    query_id: Alias
    start: Timestamp | None = None
    end: Timestamp | None = None
    step_s: int | None = Field(default=None, ge=1, le=300)
    limit_series: int = Field(default=100, ge=1, le=100)


class HostInput(TargetInput):
    """Select an adapter-provided registered host alias."""

    host_id: Alias


class LogInput(TargetInput):
    """Select one registered log and its maximum trailing bytes."""

    log_id: Alias
    max_bytes: int = Field(default=65_536, ge=1, le=65_536)


class ObservabilityTools(InspectionTools):
    """Reuse inspection grants, audits, redaction and immutable artifact exports."""

    def __init__(
        self,
        registry: ManagementRegistry,
        site_provider: SiteProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(registry)
        self.site = site_provider or RegisteredSiteProvider(self.access)
        self.transport = transport

    def _observations(self, target: ManagementTarget, rows: list[dict]) -> list[dict]:
        references = []
        for row in rows:
            if body := row.pop("raw_body", None):
                reference = self._store(target).export(body, "monitoring-source", complete=False)
                row.update(data=None, artifact_id=reference["artifact_id"])
                references.append(reference)
        return references

    async def _monitoring(self, inputs: TargetInput, target: ManagementTarget | None) -> dict:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        deadline = monotonic() + max(0.01, inputs.timeout_s - 0.1)
        # Validate endpoints before a provider performs any remote binding inspection.
        urls = {
            name + "_url": endpoint(target, name) for name in ("prometheus", "grafana", "router")
        }
        try:
            async with asyncio.timeout(max(0, deadline - monotonic())):
                binding = await self.site.monitoring_binding(target, deadline=deadline)
        except OperationError as error:
            raise AccessError(error.code, error.message) from None
        document = await observe_monitoring(
            binding,
            **urls,
            deadline=deadline,
            freshness_s=target.freshness_s,
            redactor=self.access.redactor(target),
            transport=self.transport,
        )
        references = self._observations(target, document["sources"])
        errors = [
            _error(
                row.get("error_code", "source_unavailable"),
                "Monitoring source is incomplete",
                source=row["source"],
            )
            for row in document["sources"]
            if row["status"] != "ok"
        ]
        reference = self._store(target).export(
            _encoded(document), "monitoring-status", complete=not errors
        )
        references.append(reference)
        return result(
            "monitoring_status",
            target.id,
            data=document,
            artifacts=references,
            outcome="degraded" if errors else "success",
            errors=errors,
        )

    async def _metrics(self, inputs: MetricsInput, target: ManagementTarget | None) -> dict:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        selected = next((query for query in target.queries if query.id == inputs.query_id), None)
        if selected is None:
            raise AccessError("invalid_input", "Query is not registered for this target")
        query_parameters(
            selected,
            start=inputs.start,
            end=inputs.end,
            step_s=inputs.step_s,
            limit_series=inputs.limit_series,
        )
        data, observation, errors = await query_metrics(
            selected,
            prometheus_url=endpoint(target, "prometheus"),
            start=inputs.start,
            end=inputs.end,
            step_s=inputs.step_s,
            limit_series=inputs.limit_series,
            deadline=monotonic() + max(0.01, inputs.timeout_s - 0.1),
            freshness_s=target.freshness_s,
            redactor=self.access.redactor(target),
            transport=self.transport,
        )
        references = self._observations(target, [observation])
        if observation["data"] is not None:
            references.append(
                self._store(target).export(
                    _encoded(observation), "metric-source", complete=data["complete"]
                )
            )
        return result(
            "metrics_query",
            target.id,
            data=data,
            errors=errors,
            artifacts=references,
            outcome="success" if data["complete"] else "degraded",
        )

    async def _collect(self, inputs: HostInput | LogInput, target: ManagementTarget | None) -> dict:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        inventory = isinstance(inputs, HostInput)
        name = "host_inventory" if inventory else "host_logs"
        if isinstance(inputs, HostInput):
            maximum, subject = 1_048_576, inputs.host_id
        else:
            maximum, subject = inputs.max_bytes, inputs.log_id
        if not inventory and not any(row.id == subject for row in target.logs):
            raise AccessError("invalid_input", "Log is not registered for this target")
        deadline = monotonic() + max(0.01, inputs.timeout_s - 0.1)
        try:
            async with asyncio.timeout(max(0, deadline - monotonic())):
                captures = await self.site.collect(
                    target,
                    "inventory" if inventory else "log",
                    subject,
                    deadline=deadline,
                    max_bytes=maximum,
                )
        except OperationError as error:
            raise AccessError(error.code, error.message) from None
        except TimeoutError:
            captures = (SourceCapture(subject, b"", now(), False, "timeout", "source_unavailable"),)
        references, rows, errors = [], [], []
        redactor = self.access.redactor(target)
        remaining = maximum
        export_remaining = maximum
        for capture in captures[:128]:
            content = capture.content[:remaining]
            remaining -= len(content)
            complete = (
                capture.complete and capture.status == "ok" and len(content) == len(capture.content)
            )
            safe = redactor.body(content)
            if len(safe) > export_remaining:
                safe = safe[:export_remaining].decode("utf-8", errors="ignore").encode()
                complete = False
            export_remaining -= len(safe)
            status = capture.status
            error_code = capture.error_code
            try:
                age = (datetime.now(UTC) - timestamp(capture.observed_at)).total_seconds()
                if not 0 <= age <= target.freshness_s:
                    complete, status, error_code = False, "stale", "source_stale"
            except (ValueError, TypeError, OverflowError, OSError):
                complete, status, error_code = False, "error", "invalid_source"
            reference = self._store(target).export(
                safe, "host-inventory-source" if inventory else "host-log", complete=complete
            )
            references.append(reference)
            row = {
                "source": capture.source,
                "observed_at": capture.observed_at,
                "status": status if complete else "truncated" if status == "ok" else status,
                "complete": complete,
                "artifact_id": reference["artifact_id"],
                "bytes": len(safe),
                "provenance": redactor.value(capture.provenance),
            }
            if not complete or status != "ok":
                detail = _error(
                    error_code or "source_truncated",
                    redactor.text(capture.error or "Host evidence is incomplete"),
                    source=capture.source,
                )
                row["error"] = detail
                errors.append(detail)
            rows.append(row)
        if len(captures) > 128 or not captures:
            errors.append(_error("collection_limit", "Host collection did not retain every source"))
        complete = not errors
        observed_at = now()
        if inventory:
            document = {
                "host_id": subject,
                "observed_at": observed_at,
                "complete": complete,
                "sources": rows,
            }
            reference = self._store(target).export(
                _encoded(document), "host-inventory", complete=complete
            )
            references.append(reference)
            data = {
                "snapshot_artifact_id": reference["artifact_id"],
                "observed_at": observed_at,
                "complete": complete,
            }
        else:
            # A selected log is one frozen file tail. Providers must not concatenate subjects.
            if len(references) != 1:
                raise AccessError("invalid_input", "Log provider must return one registered source")
            data = {
                "artifact_id": references[0]["artifact_id"],
                "bytes": rows[0]["bytes"],
                "complete": complete,
                "observed_at": observed_at,
            }
        return result(
            name,
            target.id,
            data=data,
            artifacts=references,
            errors=errors,
            outcome="success" if complete else "degraded",
        )


def observability_adapters(
    registry: ManagementRegistry,
    *,
    site_provider: SiteProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[ToolAdapter[Any], ...]:
    """Register four inspection tools with an explicitly supplied trusted site provider."""
    owner = ObservabilityTools(registry, site_provider, transport)
    adapters = []
    for name, description, model, operation in (
        (
            "monitoring_status",
            "Inspect configured monitoring and serving readiness",
            TargetInput,
            owner._monitoring,
        ),
        (
            "metrics_query",
            "Run a bounded registered Prometheus query",
            MetricsInput,
            owner._metrics,
        ),
        ("host_inventory", "Collect bounded registered host inventory", HostInput, owner._collect),
        ("host_logs", "Capture a registered log tail", LogInput, owner._collect),
    ):

        async def invoke(inputs: Any, name: str = name, operation: Any = operation) -> dict:
            return await owner._invoke(name, inputs, operation)

        adapters.append(ToolAdapter(name, description, model, invoke, open_world=True))
    return tuple(adapters)
