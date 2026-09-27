"""Define bounded observation and site-provider interfaces without MCP dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from narwhal.deployment.management_registry import ManagementTarget

from .management_targets import TargetContract

PROMETHEUS_VERSION = "3.14.0"
GRAFANA_VERSION = "13.2.1"


@dataclass(frozen=True)
class MonitoringBinding:
    """Bind observed services to the monitoring host's expected deployment."""

    targets: TargetContract
    datasource_url: str
    host_id: str
    prometheus_version: str = PROMETHEUS_VERSION
    grafana_version: str = GRAFANA_VERSION


@dataclass(frozen=True)
class SourceCapture:
    """Carry bounded bytes to the caller that redacts and exports them."""

    source: str
    content: bytes
    observed_at: str
    complete: bool = True
    status: str = "ok"
    error_code: str | None = None
    error: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


class SiteProvider(Protocol):
    """Resolve only registered subjects within an absolute monotonic deadline."""

    async def monitoring_binding(
        self, target: ManagementTarget, *, deadline: float
    ) -> MonitoringBinding:
        """Return expected scrape identities and the host-local datasource URL."""
        ...

    async def collect(
        self,
        target: ManagementTarget,
        kind: Literal["inventory", "log"],
        subject_id: str,
        *,
        deadline: float,
        max_bytes: int,
    ) -> tuple[SourceCapture, ...]:
        """Inspect one registered host or log without deployment mutations."""
        ...
