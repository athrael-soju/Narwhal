"""Score a comparison run the way the study's methodology §C asks for it.

Goodput is the headline, but it says nothing about whether the controller
actuated or thrashed. §C names the rest: re-role count and rate, thrash as
reversals within a short window (counted windowless across the run here),
and attainment split by which SLO bound.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .oracle import fraction_wrong
from .oracle import read as read_oracle
from .profiler import ProfileStore


@dataclass
class ArmRate:
    """One scored cell, arm at rate: everything §C asks a report to carry."""

    arm: str
    tag: str
    rate: float | None
    met: int
    total: int
    reroles: int
    reversals: int
    damaged: int
    wall_s: float
    bound: Counter
    adapt_s: list[float]
    wrong: float | None
    # §E: seconds between o1 on the prefill instance and the first byte the
    # decode leg produced, split by whether the KV crossed instances.
    handoff_crossed: list[float]
    handoff_local: list[float]

    @property
    def crossed_share(self) -> float:
        """Fraction of handoffs that crossed instances."""
        n = len(self.handoff_crossed) + len(self.handoff_local)
        return len(self.handoff_crossed) / n if n else 0.0

    @property
    def attainment(self) -> float:
        """Met over total: §6.1's goodput."""
        return self.met / self.total if self.total else 0.0

    @property
    def thrash_per_hour(self) -> float:
        """Reversals per wall-clock hour, §C's thrash."""
        return self.reversals * 3600.0 / self.wall_s if self.wall_s else 0.0

    @property
    def median_adapt_s(self) -> float | None:
        """§C's time-to-adapt: the lag from a phase boundary to the next flip."""
        if not self.adapt_s:
            return None
        ordered = sorted(self.adapt_s)
        return ordered[len(ordered) // 2]


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _handoffs(rows: list[dict]) -> tuple[list[float], list[float]]:
    """§E per-request handoff cost, from the journal alone."""
    crossed: list[float] = []
    local: list[float] = []
    for r in rows:
        if r.get("error") or r.get("first_byte_s") is None or r.get("ttft_s") is None:
            continue
        (crossed if r.get("crossed") else local).append(r["first_byte_s"] - r["ttft_s"])
    return crossed, local


def _bound(row: dict, ttft_slo: float, tpot_slo: float) -> str:
    """Which constraint a request missed, or "met" - or "refused" at the door."""
    if row.get("refused"):
        return "refused"
    if row.get("error"):
        return "error"
    if row["output_len"] < row.get("wanted_len", 0):
        return "short"
    over = []
    if row.get("ttft_s") and row["ttft_s"] > ttft_slo:
        over.append("ttft")
    if row.get("tpot_s") and row["tpot_s"] > tpot_slo:
        over.append("tpot")
    return "+".join(over) if over else "met"


def _time_to_adapt(
    flips: list[dict], first_arrival: float, phase_s: float, phases: int = 3
) -> list[float]:
    """Lag from each load shift to the first role change after it (§C).

    The phase boundaries are deterministic, so no extra instrumentation is
    needed. A boundary with no flip after it contributes nothing rather than a
    zero, because "never adapted" is not "adapted instantly".
    """
    stamped = [f["at"] for f in flips if "at" in f]
    if not stamped or not phase_s:
        return []
    out = []
    for k in range(1, phases):
        boundary = first_arrival + k * phase_s
        after = [at for at in stamped if at >= boundary]
        if after:
            out.append(min(after) - boundary)
    return out


def _reversals(flips: list[dict]) -> int:
    """§C: an engine flipped one way then back. Counted per instance."""
    last: dict[str, str] = {}
    n = 0
    for f in flips:
        iid, to = f["iid"], f["to"]
        if last.get(iid) not in (None, to):
            n += 1
        last[iid] = to
    return n


def read_run(
    d: Path,
    ttft_slo: float,
    tpot_slo: float,
    profiles: Path | None = None,
    phases: int = 3,
) -> list[ArmRate]:
    """Score every (arm, rate) cell under `d`.

    With a profile store the §D oracle also runs, giving the fraction of windows
    the controller spent on a split the oracle would not have chosen.
    """
    store = ProfileStore(profiles) if profiles and profiles.exists() else None
    out: list[ArmRate] = []
    for journal in sorted(d.glob("*.journal.jsonl")):
        arm, rest = journal.name.split(".", 1)[0], journal.name.split(".")[1:-2]
        tag = ".".join(rest)
        rows = [
            r
            for x in journal.read_text().splitlines()
            if x.strip() and "meta" not in (r := json.loads(x))
        ]
        if not rows:
            continue
        try:
            rate = float(tag)
        except ValueError:
            rate = None  # a named cell, e.g. `walk`
        bound = Counter(_bound(r, ttft_slo, tpot_slo) for r in rows)
        state = d / f"{arm}.{tag}.state.after.json"
        flips = json.loads(state.read_text())["flips"] if state.exists() else []
        wall = max(r["arrived"] for r in rows) - min(r["arrived"] for r in rows)
        phase_s = wall / phases if wall else 0.0
        crossed, local = _handoffs(rows)
        out.append(
            ArmRate(
                arm=arm,
                tag=tag,
                rate=rate,
                met=bound["met"],
                # The same denominator bench.score_journal uses: a request
                # refused at the door was never served late, and charging the
                # arm for the honesty would make the two scorers disagree on
                # every predictive cell. The refused count stays visible in
                # `bound`.
                total=len(rows) - bound["refused"],
                reroles=len(flips),
                reversals=_reversals(flips),
                damaged=sum(
                    f.get("prefill_inflight", 0) + f.get("decode_inflight", 0) for f in flips
                ),
                wall_s=wall,
                bound=bound,
                adapt_s=_time_to_adapt(flips, min(r["arrived"] for r in rows), phase_s, phases),
                wrong=_oracle_gap(journal, state, store, tpot_slo),
                handoff_crossed=crossed,
                handoff_local=local,
            )
        )
    return out


def _oracle_gap(
    journal: Path, state: Path, store: ProfileStore | None, tpot_slo: float
) -> float | None:
    """§D's fraction-of-time-wrong, when a profile is available to price it."""
    if store is None or not len(store):
        return None
    profile = store.any()
    if profile is None:
        return None
    try:
        return fraction_wrong(read_oracle(journal, state, profile, tpot_slo))
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def oracle_saturated(rows: list[ArmRate], band: float = 0.85) -> bool:
    """True when the §D oracle discriminates nothing on this run.

    On the walk workload the oracle reads wrong for every arm -
    high-attaining ones included - because TPOT binds nearly always at
    that ISL band and the min_prefill floor blocks the split it prefers,
    so the column says the same thing about winners and losers. A column
    that cannot separate arms must not be cited; the renderer says so
    instead of leaving the reader to notice.
    """
    scored = [r.wrong for r in rows if r.wrong is not None]
    return len(scored) >= 2 and all(w >= band for w in scored)


def render(rows: list[ArmRate]) -> str:
    """Both report tables as text."""
    header = (
        f"{'arm':<12}{'cell':>6}{'attain':>9}{'met/total':>12}"
        f"{'re-roles':>10}{'thrash/h':>10}{'adapt s':>9}{'damaged':>9}{'wrong':>8}  binding"
    )
    lines = [header, "-" * 82]
    ordered = sorted(rows, key=lambda r: (r.arm, r.rate is None, r.rate or 0.0, r.tag))
    for r in ordered:
        misses = ", ".join(f"{k} {v}" for k, v in r.bound.most_common() if k != "met") or "none"
        cell = f"{r.rate:.2f}" if r.rate is not None else r.tag[:6]
        lines.append(
            f"{r.arm:<12}{cell:>6}{r.attainment * 100:>8.1f}%"
            f"{f'{r.met}/{r.total}':>12}{r.reroles:>10}{r.thrash_per_hour:>10.1f}"
            f"{('-' if r.median_adapt_s is None else f'{r.median_adapt_s:.1f}'):>9}"
            f"{r.damaged:>9}"
            f"{('-' if r.wrong is None else f'{r.wrong * 100:.0f}%'):>8}  {misses}"
        )
    if oracle_saturated(rows):
        lines.append(
            "NOTE: the offline oracle is saturated on this run (every scored arm >= 85% "
            "wrong-split) - the column discriminates nothing here and is not citable."
        )
    if any(r.handoff_crossed or r.handoff_local for r in rows):
        lines += [
            "",
            f"{'arm':<12}{'cell':>6}{'crossed':>9}"
            f"{'p50':>7}{'p90':>7}{'p99':>7}{'local p50':>11}   KV handoff, s",
        ]
        for r in ordered:
            if not (r.handoff_crossed or r.handoff_local):
                continue
            cell = f"{r.rate:.2f}" if r.rate is not None else r.tag[:6]

            def f3(v: float | None) -> str:
                return "-" if v is None else f"{v:.2f}"

            lines.append(
                f"{r.arm:<12}{cell:>6}{r.crossed_share * 100:>8.0f}%"
                f"{f3(_pct(r.handoff_crossed, 0.5)):>7}{f3(_pct(r.handoff_crossed, 0.9)):>7}"
                f"{f3(_pct(r.handoff_crossed, 0.99)):>7}{f3(_pct(r.handoff_local, 0.5)):>11}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: score every cell found under --dir."""
    ap = argparse.ArgumentParser(description="Score a comparison run (the study's methodology §C)")
    ap.add_argument("--dir", default="runs/local/comparison")
    ap.add_argument("--ttft-slo", type=float, required=True)
    ap.add_argument("--tpot-slo", type=float, required=True)
    ap.add_argument(
        "--profiles", default="runs/profiles.json", help="profile store, for the §D oracle"
    )
    ap.add_argument(
        "--phases", type=int, default=3, help="phases in each cell's trace, for time-to-adapt"
    )
    args = ap.parse_args(argv)

    rows = read_run(Path(args.dir), args.ttft_slo, args.tpot_slo, Path(args.profiles), args.phases)
    if not rows:
        print(f"no journals under {args.dir}")
        return 2
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
