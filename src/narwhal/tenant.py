"""The tenant layer: an authenticated door, shares of the door, honest books.

One implicit tenant is what the Arrow paper assumes; a shared fleet needs three
things this module isolates. Identity: which tenant a request belongs to,
from an API key the config names by environment variable - secrets stay out
of the run's record (the repo's convention), never in the JSON the archive
keeps. Fairness and priority: weighted shares of the admission limit, so one
tenant's flood occupies its own share and cannot starve another's trickle,
and a heavier class survives overload longer, which is "admitted first, shed
last" on a router that refuses rather than queues. Accounting: per-tenant
served/failed/rejected/inflight, journaled and surfaced, because a shared
fleet's customers are billed on what they actually got.

Composition note: predictive admission answers whether the fleet can
serve on time; this layer answers whose turn it is when it cannot. Refusals
should land on the lowest class first either way, and both rules live behind
`TenantLedger.can_admit` so whichever admission pass asks the question gets
the same answer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .config import TenantSpec


@dataclass
class _Books:
    """Per-tenant accounting, all of it the fleet's honest record per customer."""

    served: int = 0
    failed: int = 0
    rejected: int = 0
    inflight: int = 0


class TenantLedger:
    """Resolution, admission policy and accounting for the tenants a config names.

    With no tenants configured the ledger holds one anonymous book over the
    whole pool and admits exactly as before: the feature is inert by default.
    """

    def __init__(
        self,
        max_connections: int,
        specs: list[TenantSpec] | None = None,
        *,
        auth_required: bool = False,
        anonymous_weight: float = 1.0,
    ) -> None:
        self.max_connections = max_connections
        self.specs = specs or []
        self.auth_required = auth_required
        self.anonymous_weight = anonymous_weight
        self._by_key: dict[str, TenantSpec] = {}
        for spec in self.specs:
            key = os.environ.get(spec.api_key_env, "")
            # The serving door is where an unset key must refuse: a named
            # tenant nobody can authenticate as is a misconfiguration, and
            # discovering it on the first 401 is too late. Loading the same
            # config for checks and reports needs no keys and never builds
            # a ledger.
            if not key:
                raise ValueError(
                    f"tenants[{spec.name}].api_key_env {spec.api_key_env} is not set "
                    "in the environment; the door cannot authenticate this tenant"
                )
            self._by_key[key] = spec
        self._books: dict[str, _Books] = {s.name: _Books() for s in self.specs}
        self._books.setdefault("anonymous", _Books())

    # -- identity --------------------------------------------------------

    def resolve(self, headers: Mapping[str, str]) -> TenantSpec | None:
        """The tenant a request belongs to, from its bearer credential.

        `None` means the door stays shut when auth is required, or the
        anonymous bucket when it is not.
        """
        key = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        key = key or headers.get("x-api-key", "").strip()
        if not key:
            return None
        return self._by_key.get(key)

    def name_of(self, tenant: TenantSpec | None) -> str:
        """The ledger name: the tenant's own, or the anonymous door."""
        return tenant.name if tenant is not None else "anonymous"

    def weight_of(self, name: str) -> float:
        """Placement priority is the fair share itself."""
        if name == "anonymous":
            return self.anonymous_weight
        spec = next((s for s in self.specs if s.name == name), None)
        return spec.weight if spec is not None else self.anonymous_weight

    # -- admission ---------------------------------------------------------

    def _cap(self, name: str) -> int:
        """This tenant's seat count: its share of the pool, floored at one,
        and never past its own explicit cap. Shares keep the outer bound
        honest: every tenant capped at its share sums to the pool exactly.
        """
        if not self.specs:
            return self.max_connections
        # One denominator for every bucket: the named weights plus, when the
        # door is open, the anonymous bucket's. Splitting the denominators
        # (named shares over named-only, anonymous over the full sum) let
        # the caps oversubscribe the pool, and the pool-total backstop then
        # refused a trickle that was inside its own promised share.
        total_weight = sum(s.weight for s in self.specs)
        if not self.auth_required:
            total_weight += self.anonymous_weight
        if name == "anonymous" and not self.auth_required:
            weight = self.anonymous_weight
        else:
            spec = next((s for s in self.specs if s.name == name), None)
            weight = spec.weight if spec is not None else 0.0
        share = max(1, round(self.max_connections * weight / total_weight))
        spec = next((s for s in self.specs if s.name == name), None)
        if spec is not None and spec.max_concurrent > 0:
            share = min(share, spec.max_concurrent)
        return share

    def can_admit(self, tenant: TenantSpec | None) -> bool:
        """Whose turn it is: own share first, the pool second.

        The flood pays for its share alone: past `cap`, a refusal lands on
        the flooding tenant even while the pool has room the trickle's share
        is owed. It is conservation of seats, not conservation of throughput
        - that is what fairness with a reservation costs, and the issue says
        pay it.
        """
        name = self.name_of(tenant)
        books = self._books[name]
        if books.inflight >= self._cap(name):
            books.rejected += 1
            return False
        total = sum(b.inflight for b in self._books.values())
        if total >= self.max_connections:
            books.rejected += 1
            return False
        return True

    def door_refused(self, name: str) -> None:
        """A 401 at the door counts on the bucket the bearer claimed."""
        self._books[name].rejected += 1

    def admitted(self, name: str) -> None:
        """One seat taken by this tenant."""
        self._books[name].inflight += 1

    def completed(self, name: str, *, served: bool, refused: bool = False) -> None:
        """The seat is back, and the books say how the request ended.

        `refused` is the predictive door turning the request away after the
        seat was taken: the tenant's book reads it beside the share
        refusals, never as the fleet failing work it accepted.
        """
        books = self._books[name]
        books.inflight -= 1
        if refused:
            books.rejected += 1
        elif served:
            books.served += 1
        else:
            books.failed += 1

    def snapshot(self) -> dict[str, dict]:
        """The /arrow/state slice: every named book, anonymous included when used."""
        out: dict[str, dict] = {}
        for name, b in self._books.items():
            if name == "anonymous" and not (b.served or b.failed or b.rejected or b.inflight):
                continue
            out[name] = {
                "served": b.served,
                "failed": b.failed,
                "rejected": b.rejected,
                "inflight": b.inflight,
                "weight": self.weight_of(name),
                "cap": self._cap(name),
            }
        return out
