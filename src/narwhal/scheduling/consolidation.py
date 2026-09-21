"""Bounded arrival evidence and decode-consolidation safety."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .demand import OutputEstimates, rounded

if TYPE_CHECKING:
    from .controller import ReactiveController


def empty_demand_evidence() -> dict[str, object]:
    """The zero consolidation-evidence state, for routers without a controller."""
    return {
        "span_s": 0.0,
        "arrivals": 0,
        "required_span_s": 0.0,
        "required_arrivals": 0,
        "max_span_s": 0.0,
        "closed": False,
        "risk_kind": None,
        "risk_age_s": None,
        "risk_events": {},
        "short_decode_engines": None,
        "long_decode_engines": None,
        "trend_ratio": None,
        "envelope_decode_engines": None,
        "blocked_gate": "none",
    }


@dataclass(frozen=True)
class ConsolidationEvidence:
    """Full-precision evidence used to permit or refuse decode consolidation."""

    span_s: float
    arrivals: int
    required_span_s: float
    required_arrivals: int
    max_span_s: float
    closed: bool
    risk_kind: str | None
    risk_age_s: float | None
    short_decode_engines: float
    long_decode_engines: float
    blocked_gate: str
    reason: str

    @property
    def envelope_decode_engines(self) -> float:
        """Conservative demand used for the candidate's decode capacity."""
        return max(self.short_decode_engines, self.long_decode_engines)

    @property
    def allowed(self) -> bool:
        """Whether the candidate may consolidate decode capacity."""
        return self.blocked_gate == "none"

    def snapshot(self) -> dict[str, object]:
        """Serialize evidence for state and metrics without changing policy inputs."""
        return {
            "span_s": rounded(self.span_s),
            "arrivals": self.arrivals,
            "required_span_s": rounded(self.required_span_s),
            "required_arrivals": self.required_arrivals,
            "max_span_s": rounded(self.max_span_s),
            "closed": self.closed,
            "risk_kind": self.risk_kind,
            "risk_age_s": rounded(self.risk_age_s) if self.risk_age_s is not None else None,
            "short_decode_engines": rounded(self.short_decode_engines),
            "long_decode_engines": rounded(self.long_decode_engines),
            "trend_ratio": (
                rounded(self.short_decode_engines / self.long_decode_engines)
                if self.long_decode_engines > 0
                else None
            ),
            "envelope_decode_engines": rounded(self.envelope_decode_engines),
            "blocked_gate": self.blocked_gate,
        }

    def details(self) -> dict[str, object]:
        """Flatten evidence into the existing controller-decision fields."""
        return {
            key if key in ("risk_kind", "risk_age_s") else f"evidence_{key}": value
            for key, value in self.snapshot().items()
        }


class ConsolidationSafety:
    """Re-arm bounded consolidation evidence after decode risk events."""

    def __init__(
        self,
        controller: ReactiveController,
        *,
        evidence_span_s: float,
        evidence_max_span_s: float,
        evidence_min_arrivals: int,
        demand_rise_tolerance: float,
    ) -> None:
        self.controller = controller
        self._clock = controller._clock
        self.demand = controller.demand
        self.evidence_span_s = evidence_span_s
        self.evidence_max_span_s = evidence_max_span_s
        self.evidence_min_arrivals = evidence_min_arrivals
        self.demand_rise_tolerance = demand_rise_tolerance
        self._risk_since: float | None = None
        self._risk_kind: str | None = None
        self._risk_events: dict[str, int] = {"first_token_timeout": 0, "p_to_d_recovery": 0}

    def note_risk_event(self, kind: str, at: float | None = None) -> None:
        """Restart the decode-consolidation evidence window after a risk event.

        The window closes on sufficient post-event arrivals or when the bounded
        lookback expires.
        """
        seen = self._clock() if at is None else at
        if self._risk_since is None or seen > self._risk_since:
            self._risk_since = seen
            self._risk_kind = kind
        self._risk_events[kind] = self._risk_events.get(kind, 0) + 1

    def capture(
        self,
        now: float,
        *,
        estimates: OutputEstimates | None = None,
        correction: float | None = None,
    ) -> ConsolidationEvidence:
        """Capture the evidence window, risk hold and short-horizon trend once."""
        cutoff = now - self.evidence_max_span_s
        if self._risk_since is not None:
            cutoff = max(cutoff, self._risk_since)
        count, oldest = self.demand.arrivals.evidence(cutoff)
        span = now - oldest if oldest is not None and oldest <= now else 0.0
        risk_age = now - self._risk_since if self._risk_since is not None else None
        closed = bool(
            (span >= self.evidence_span_s and count >= self.evidence_min_arrivals)
            or (oldest is not None and span >= self.evidence_max_span_s)
            or (risk_age is not None and risk_age >= self.evidence_max_span_s)
        )
        samples, _ = self.demand.arrivals.evidence(now - self.evidence_span_s)
        _, short = self.controller._demand(
            now,
            horizon_s=self.evidence_span_s,
            estimates=estimates,
            correction=correction,
        )
        long_estimate = self.controller.last_demand.decode_engines
        gate, reason = "none", ""
        if not closed:
            if self._risk_since is not None and self._risk_kind is not None:
                gate = "risk"
                reason = (
                    f"{self._risk_kind.replace('_', ' ')} "
                    f"{rounded(now - self._risk_since, 1)} s ago: re-collecting "
                    "one evidence window"
                )
            else:
                gate = "evidence"
                reason = "demand evidence is still collecting"
        elif samples >= self.evidence_min_arrivals and (
            short > long_estimate * (1.0 + self.demand_rise_tolerance)
            if long_estimate > 0
            else short > 0
        ):
            gate, reason = "trend", "rising decode demand blocks consolidation"
        return ConsolidationEvidence(
            span_s=span,
            arrivals=count,
            required_span_s=self.evidence_span_s,
            required_arrivals=self.evidence_min_arrivals,
            max_span_s=self.evidence_max_span_s,
            closed=closed,
            risk_kind=self._risk_kind,
            risk_age_s=risk_age,
            short_decode_engines=short,
            long_decode_engines=long_estimate,
            blocked_gate=gate,
            reason=reason,
        )

    def consolidation_evidence_snapshot(self) -> dict[str, object]:
        """Consolidation evidence and risk state for `/narwhal/state` and `/metrics`."""
        now = self._clock()
        evidence = self.capture(now)
        out = empty_demand_evidence()
        out.update(evidence.snapshot())
        out["risk_events"] = dict(self._risk_events)
        return out

    def risk_handoff(self) -> dict[str, object] | None:
        """Return the newest risk event's age and per-kind counts for handoff.

        The receiver collects fresh arrivals to close its evidence window.
        """
        if self._risk_since is None:
            return None
        return {
            "kind": self._risk_kind,
            "age_s": max(0.0, self._clock() - self._risk_since),
            "events": {kind: count for kind, count in self._risk_events.items() if count > 0},
        }

    def restore_risk(self, *, kind: str | None, age_s: float, events: dict[str, int]) -> bool:
        """Rebuild armed risk state from a handoff's elapsed age."""
        if kind is None:
            self._risk_since = None
            self._risk_kind = None
            return False
        # Anchor the elapsed age to this process's clock.
        self._risk_since = self._clock() - max(0.0, age_s)
        self._risk_kind = kind
        for name, count in events.items():
            self._risk_events[name] = self._risk_events.get(name, 0) + count
        return True
