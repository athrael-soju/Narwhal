"""The offline upper bound the study's methodology §D asks for.

A reactive controller waits for a queue to form. The oracle knows the window's
future and picks the split that serves it, so the gap between them is the value
of predicting rather than reacting, measured rather than asserted.

This is a model over the journal, not a measurement of a run: it prices each
window's work with the profiled curves and asks which split could have carried
it. It is an upper bound on the policy, and the frictionless variant charges
nothing for the role changes it makes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .profiler import Profile


@dataclass(frozen=True)
class Window:
    """One §D window: offered work, the oracle's split, and the controller's."""

    start: float
    prefill_s: float  # instance-seconds of prefill work arriving
    decode_s: float  # instance-seconds of decode work in flight
    best_prefill: int  # instances the oracle would put on prefill
    actual_prefill: int


def _best_split(prefill_s: float, decode_s: float, n: int) -> int:
    """Instances on prefill that split the fleet in proportion to the demand.

    Both pools keep at least one instance, which is Algorithm 3's own `|S| > 1`
    guard read as a floor rather than a guard.
    """
    total = prefill_s + decode_s
    if total <= 0:
        return n // 2
    share = round(n * prefill_s / total)
    return max(1, min(n - 1, share))


def _split_at(t: float, opening_prefill: int, flips: list[dict]) -> int:
    """The controller's prefill pool size at time `t`, replayed from the flips."""
    size = opening_prefill
    for f in flips:
        if f["at"] > t:
            break
        size += 1 if f["to"] == "prefill" else -1
    return size


def windows(
    rows: list[dict],
    flips: list[dict],
    profile: Profile,
    n_instances: int,
    opening_prefill: int,
    tpot_s: float,
    window_s: float = 10.0,
) -> list[Window]:
    """Cut the run into windows and judge each against the oracle's split."""
    if not rows:
        return []
    t0 = min(r["arrived"] for r in rows)
    t1 = max(r["arrived"] for r in rows)
    out: list[Window] = []
    edge = t0
    while edge < t1:
        end = edge + window_s
        active = [r for r in rows if edge <= r["arrived"] < end]
        prefill_s = sum(profile.prefill_time(r["input_len"]) for r in active)
        decode_s = sum((r.get("output_len") or 0) * (r.get("tpot_s") or tpot_s) for r in active)
        out.append(
            Window(
                start=edge,
                prefill_s=prefill_s,
                decode_s=decode_s,
                best_prefill=_best_split(prefill_s, decode_s, n_instances),
                actual_prefill=_split_at(edge, opening_prefill, flips),
            )
        )
        edge = end
    return out


def fraction_wrong(ws: list[Window]) -> float:
    """§D: windows where the controller's split differs from the oracle's."""
    if not ws:
        return 0.0
    return sum(1 for w in ws if w.best_prefill != w.actual_prefill) / len(ws)


def read(
    journal: Path, state: Path, profile: Profile, tpot_s: float, window_s: float = 10.0
) -> list[Window]:
    """Windows for one run, read off its journal and state files."""
    rows = [
        r
        for x in journal.read_text().splitlines()
        if x.strip() and "meta" not in (r := json.loads(x))
    ]
    snap = json.loads(state.read_text()) if state.exists() else {}
    flips = sorted(snap.get("flips", []), key=lambda f: f["at"])
    pools = snap.get("pools", {})
    n = sum(len(v) for v in pools.values()) or 6
    # The snapshot is taken after the run, so wind the flips back to the opening.
    opening = len(pools.get("prefill", []))
    for f in flips:
        opening -= 1 if f["to"] == "prefill" else -1
    return windows(rows, flips, profile, n, max(1, opening), tpot_s, window_s)
