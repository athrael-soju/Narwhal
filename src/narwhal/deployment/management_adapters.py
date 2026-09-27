"""Compose trusted management adapters without client-selected executable code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .management_executor import ReconcileOutcome, StageContext, StageOutcome
    from .management_registry import ManagementTarget


@dataclass(frozen=True)
class AdapterManifest:
    """Fix the implementation, source and required operations for each action."""

    id: str
    version: str
    assets_sha256: str
    source: dict[str, Any]
    actions: dict[str, frozenset[str]]


@dataclass(frozen=True)
class PreparedPlan:
    """Return discovered identities and immutable inputs from preparation."""

    identity: dict[str, Any]
    observations: dict[str, Any]
    inputs: dict[str, bytes]
    stages: list[dict[str, Any]]
    parameters: dict[str, Any]


class ManagementAdapter(Protocol):
    """Keep resource discovery and effects inside a trusted package adapter."""

    manifest: AdapterManifest

    def prepare(
        self,
        context: StageContext,
        target: ManagementTarget,
        action: str,
        parameters: dict[str, Any],
    ) -> PreparedPlan:
        """Inspect identities, including canonical resources, without deployment changes."""
        ...

    def check(self, context: StageContext, target: ManagementTarget, plan: dict[str, Any]) -> None:
        """Recheck live identities, retained inputs and action prerequisites within the deadline."""
        ...

    def execute_stage(
        self,
        context: StageContext,
        stage: dict[str, Any],
        plan: dict[str, Any],
    ) -> StageOutcome:
        """Execute one declared operation and retain its receipts."""
        ...

    def reconcile(self, context: StageContext, operation: dict[str, Any]) -> ReconcileOutcome:
        """Inspect intentions and receipts without starting or stopping resources."""
        ...


def installed_adapters() -> dict[str, ManagementAdapter]:
    """Enable adapters only after their action and recovery implementations ship."""
    return {}
