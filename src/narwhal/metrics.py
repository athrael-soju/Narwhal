"""Prometheus text exposition for the router.

The fleet already runs Prometheus and already scrapes `:8011/metrics`;
this module is what answers. What it wants are the numbers
the study's methodology §C asks for alongside goodput: whether the controller actuated,
how often it reversed itself, and which SLO bound each miss.

Text format is emitted directly rather than through a client library, so the
router keeps its dependency list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Most resolution just under the target - that is where attainment is decided -
# with enough above it to shape the violation tail.
_SLO_FRACTIONS = (0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0, 1.5, 3.0, 10.0)


def buckets_for(slo_s: float) -> tuple[float, ...]:
    """Histogram edges scaled to one SLO, in seconds."""
    return tuple(round(f * slo_s, 6) for f in _SLO_FRACTIONS)


@dataclass
class Histogram:
    """Fixed buckets in the Prometheus text shape; observe-only."""

    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    n: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        """Count one value into its bucket, the total and the sum."""
        self.total += value
        self.n += 1
        for i, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[i] += 1

    def render(self, name: str, help_text: str) -> list[str]:
        """The /metrics payload: counters, pools, loads and both histograms."""
        out = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
        cumulative = 0
        for edge, count in zip(self.buckets, self.counts, strict=True):
            cumulative = max(cumulative, count)
            out.append(f'{name}_bucket{{le="{edge}"}} {count}')
        out.append(f'{name}_bucket{{le="+Inf"}} {self.n}')
        out.append(f"{name}_sum {self.total}")
        out.append(f"{name}_count {self.n}")
        return out


def _lines(
    name: str, help_text: str, kind: str, samples: list[tuple[dict[str, str], float]]
) -> list[str]:
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        label_s = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
        out.append(f"{name}{label_s} {value}")
    return out


def render(state: dict, ttft: Histogram, tpot: Histogram) -> str:
    """Render a `/arrow/state` snapshot plus the latency histograms."""
    flips = state.get("flips", [])
    by_target: dict[tuple[str, str], int] = {}
    for f in flips:
        key = (f.get("to", "?"), f.get("by", "?"))
        by_target[key] = by_target.get(key, 0) + 1

    last: dict[str, str] = {}
    reversals = 0
    for f in flips:
        iid, to = f.get("iid"), f.get("to")
        if last.get(iid) not in (None, to):
            reversals += 1
        last[iid] = to

    out: list[str] = []
    out += _lines(
        "arrow_served_total",
        "Requests completed without error",
        "counter",
        [({}, state.get("served", 0))],
    )
    out += _lines(
        "arrow_instance_role",
        "Which pool each engine carries right now (1 = holds this role)",
        "gauge",
        # An aggregated fleet (no prefill pool) runs both phases on every
        # engine - the label is a routing convention, the function is P+D -
        # so the metric reports the function and the board can say so.
        [
            ({"iid": iid, "role": role}, 1)
            for role in ("prefill", "decode")
            for iid in state.get("pools", {}).get(role, [])
        ]
        + (
            [
                ({"iid": iid, "role": "prefill"}, 1)
                for iid in state.get("pools", {}).get("decode", [])
            ]
            if not state.get("pools", {}).get("prefill")
            else []
        ),
    )
    out += _lines(
        "arrow_refused_total",
        "Requests the predictive door turned away: priced over the TTFT budget",
        "counter",
        [({}, state.get("admission", {}).get("refused", 0))],
    )
    out += _lines(
        "arrow_rejected_total",
        "Requests refused at the pool limit",
        "counter",
        [({}, state.get("admission", {}).get("rejected", 0))],
    )
    out += _lines(
        "arrow_probation_instances",
        "Engines the drift instrument has deprioritized",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("probation", [])],
    )
    out += _lines(
        "arrow_failed_total",
        "Requests that ended in an error",
        "counter",
        [({}, state.get("failed", 0))],
    )
    out += _lines(
        "arrow_unserved_total",
        "Requests that reached Algorithm 1 step 3 with nothing meeting the SLO",
        "counter",
        [({}, state.get("unserved", 0))],
    )
    poa = state.get("poa") or {}
    if poa.get("regret") is not None:
        out += _lines(
            "arrow_placement_regret",
            "Median per-placement regret vs the placement's own floor (observation only)",
            "gauge",
            [({}, poa["regret"])],
        )
    if poa.get("regime"):
        out += _lines(
            "arrow_regime",
            "1 for the current load regime (subcritical / transitional / saturated)",
            "gauge",
            [
                ({"regime": r}, 1 if r == poa["regime"] else 0)
                for r in ("subcritical", "transitional", "saturated")
            ],
        )
    # The alarm-worthy availability signal: non-zero means the breaker is
    # holding an engine out of scheduling right now.
    out += _lines(
        "arrow_ejected_instances",
        "Instances the breaker currently holds out of scheduling and both loads",
        "gauge",
        [({}, len(state.get("ejected", [])))],
    )
    out += _lines(
        "arrow_ejected",
        "1 while this instance is ejected",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("ejected", [])],
    )
    out += _lines(
        "arrow_flips_total",
        "Role changes, by target pool and which algorithm asked",
        "counter",
        [({"to": to, "by": by}, n) for (to, by), n in sorted(by_target.items())],
    )
    out += _lines(
        "arrow_flip_reversals_total",
        "Role changes that put an instance back where it came from",
        "counter",
        [({}, reversals)],
    )
    out += _lines(
        "arrow_flips_refused_total",
        "Flips declined by the load condition, the cooldown, or the pool-size guard",
        "counter",
        [({}, len(state.get("flips_refused", [])))],
    )
    out += _lines(
        "arrow_flip_inflight_total",
        "Requests resident on an instance at the moment it was flipped",
        "counter",
        [
            ({"phase": "prefill"}, sum(f.get("prefill_inflight", 0) for f in flips)),
            ({"phase": "decode"}, sum(f.get("decode_inflight", 0) for f in flips)),
        ],
    )
    out += _lines(
        "arrow_pool_instances",
        "Instances in each pool",
        "gauge",
        [({"role": r}, len(ids)) for r, ids in sorted(state.get("pools", {}).items())],
    )
    out += _lines(
        "arrow_pool_load",
        "Pool load as a ratio against its own SLO target",
        "gauge",
        [({"role": r}, v) for r, v in sorted(state.get("load", {}).items())],
    )
    out += _lines(
        "arrow_resident_requests",
        "Requests resident on each instance",
        "gauge",
        [
            ({"iid": iid, "phase": phase}, v[phase])
            for iid, v in sorted(state.get("resident", {}).items())
            for phase in ("prefill", "decode")
        ],
    )
    out += ttft.render("arrow_ttft_seconds", "Time to first token, q1 + p1 (Arrow §4.2)")
    out += tpot.render("arrow_tpot_seconds", "Time per output token (Arrow §4.3)")
    return "\n".join(out) + "\n"
