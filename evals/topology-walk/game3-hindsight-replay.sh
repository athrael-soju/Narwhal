#!/usr/bin/env bash
# Hindsight-replay eval: the routing game's offline denominator.
#
# The paper prices Game 3 live (priced admission converts deep-saturation
# anarchy into a refusal ledger) and defers the index: PoA-hat wants the value
# the routing decisions would have had under a scheduler that knew each
# window in advance. This eval is that replay. It replays run journals, not
# live traffic: nothing touches the fleet, and it can run while the fleet
# serves someone else.
#
# Method (V1 scope, stated so the citation is honest):
#   - Prefill-leg game only - the routing game's direct analog here.
#   - Windows of up to N consecutive arrivals, N the profiled engines.
#   - cost(r, e) = prefill_time_e(input_len_r) * (1 + resident_price_e), the
#     engine's fitted prefill curve loaded by the work the journal shows
#     resident on that engine at the window's start (arrived <= t <
#     arrived + ttft). Both sides pay the same state; OPT is the min-cost
#     perfect matching over the window, exact by permutation.
#   - PoA-hat(window) = modeled cost of the journal's actual assignment /
#     OPT. Reported per station (phase) and overall, overall both as
#     median-of-windows and as ratio-of-sums - the first shows spread, the
#     second is the game-theoretic quantity the paper's tables carry.
#
# One constraint to read the numbers by: OPT is a perfect matching within the
# window, so every profiled engine serves exactly one request of each window.
# On a symmetric fleet that is the congestion game's OPT analog. On a strongly
# asymmetric one - one engine several times slower than the rest - the
# matching can price above the actual policy and PoA-hat drops below 1.0;
# read that as the window's constraint binding, not as negative anarchy.
#
# Ports the estimator structure of research/studies/poa-replay/replay.py
# (Price-of-Order, papers@c896b6c) onto shipped artifacts.
#
# Usage:
#   bash evals/topology-walk/game3-hindsight-replay.sh
#   IN=runs/canon/topology-walk PROFILES=runs/local/profiles.json \
#     bash evals/topology-walk/game3-hindsight-replay.sh
#
# Environment:
#   IN        directory holding per-run artifact sets  (default runs/canon/topology-walk)
#   PROFILES  the router's fitted profile store        (default runs/local/profiles.json)
#   PHASE_S   station length in seconds                (default 1200 - the walk's phase)
#   OUT       artifact directory                       (default runs/local/eval-hindsight-replay)
#
# Inputs it refuses to guess: with no profiles the engine curves are
# fabrications, so a missing store stops the run with exit 2 rather than
# degrade to assumption. Journals whose prefill_iid names an unprofiled
# engine contribute nothing (their rows drop out), which keeps a partial
# deploy from pricing ghosts.
set -u
cd "$(dirname "$0")/../.."

IN="${IN:-runs/canon/topology-walk}"
PROFILES="${PROFILES:-runs/local/profiles.json}"
PHASE_S="${PHASE_S:-1200}"
OUT="${OUT:-runs/local/eval-hindsight-replay}"

[ -d "$IN" ] || { echo "no such input directory: $IN"; exit 2; }
[ -f "$PROFILES" ] || { echo "no profile store at $PROFILES - the curves would be guessed; stopping"; exit 2; }
mkdir -p "$OUT"

IN="$IN" PROFILES="$PROFILES" PHASE_S="$PHASE_S" OUT="$OUT" python3 - <<'PYEOF'
import bisect, glob, json, os
from itertools import permutations

root, profiles_path = os.environ["IN"], os.environ["PROFILES"]
phase_s, out_dir = float(os.environ["PHASE_S"]), os.environ["OUT"]

with open(profiles_path) as f:
    profiles = {p["iid"]: p for p in json.load(f)}
iids = sorted(profiles)
if not iids:
    raise SystemExit(f"profile store at {profiles_path} holds no engines")

def prefill_time(p: dict, n: int) -> float:
    return max(1e-9, p["ttft_a"] * n * n + p["ttft_b"] * n + p["ttft_c"])

journals = sorted(
    glob.glob(f"{root}/**/*.journal.jsonl", recursive=True)
    + glob.glob(f"{root}/*.journal.jsonl")
)
journals = sorted(set(journals))
if not journals:
    raise SystemExit(f"no journals found under {root}")

def replay(path: str):
    """Per-station window ratios for one journal; the estimator as scoped above."""
    with open(path) as f:
        rows = [json.loads(x) for x in f if x.strip() and not x.lstrip().startswith('{"meta"')]
    rows = [r for r in rows if "arrived" in r and r.get("prefill_iid") in profiles]
    rows.sort(key=lambda r: r["arrived"])
    if len(rows) < len(iids):
        return None
    t0 = rows[0]["arrived"]
    per_engine = {i: [] for i in iids}
    for r in rows:
        per_engine[r["prefill_iid"]].append(
            (r["arrived"], r["arrived"] + (r.get("ttft_s") or 0.0), r["input_len"])
        )
    starts = {i: [e[0] for e in per_engine[i]] for i in iids}

    def resident_price(iid: str, t: float) -> float:
        hi = bisect.bisect_right(starts[iid], t)
        return sum(
            prefill_time(profiles[iid], n)
            for (a, end, n) in per_engine[iid][:hi]
            if end > t
        )

    n_e = len(iids)
    idx_of = {i: k for k, i in enumerate(iids)}
    stations: dict[int, list[float]] = {}
    sum_actual = sum_opt = 0.0
    for w0 in range(0, len(rows) - n_e + 1, n_e):
        win = rows[w0 : w0 + n_e]
        t = win[0]["arrived"]
        state = {i: resident_price(i, t) for i in iids}
        cost = [
            [prefill_time(profiles[e], r["input_len"]) * (1.0 + state[e]) for e in iids]
            for r in win
        ]
        opt = min(
            sum(cost[k][perm[k]] for k in range(n_e))
            for perm in permutations(range(n_e))
        )
        observed = sum(cost[k][idx_of[win[k]["prefill_iid"]]] for k in range(n_e))
        if opt <= 0:
            continue
        stations.setdefault(int((t - t0) // phase_s), []).append(observed / opt)
        sum_actual += observed
        sum_opt += opt
    return stations, sum_actual, sum_opt

report_md, report_json = [], {}
report_md.append(
    f"# PoA-hat replay over `{root}`\n\n"
    f"profiles: `{profiles_path}` ({len(iids)} engines: {', '.join(iids)})\n"
    f"station width {phase_s:g} s; PoA-hat = modeled actual / windowed matching OPT,"
    " prefill leg only, OPT frictionless within the window.\n\n"
    "OPT is a perfect matching within each window (every profiled engine serves"
    " exactly one request): on a strongly asymmetric fleet it can price above"
    " the actual policy, and PoA-hat below 1.0 reads as that constraint"
    " binding, not as negative anarchy.\n"
)
for path in journals:
    rel = os.path.relpath(path, root)
    cell = rel[:-len(".journal.jsonl")].replace(os.sep, "/")
    got = replay(path)
    if got is None:
        report_md.append(f"\n## {cell}\n\nnot enough profiled rows to price.\n")
        continue
    stations, sum_actual, sum_opt = got
    allv = sorted(x for v in stations.values() for x in v)
    if not allv:
        report_md.append(f"\n## {cell}\n\nno windows survived.\n")
        continue
    p50 = allv[len(allv) // 2]
    p90 = allv[min(int(0.9 * len(allv)), len(allv) - 1)]
    overall = sum_actual / sum_opt if sum_opt > 0 else float("nan")
    report_json[cell] = {
        "windows": len(allv),
        "p50": p50,
        "p90": p90,
        "max": allv[-1],
        "ratio_of_sums": overall,
        "stations": {
            st: {
                "windows": len(v),
                "p50": sorted(v)[len(v) // 2],
                "p90": sorted(v)[min(int(0.9 * len(v)), len(v) - 1)],
                "max": max(v),
            }
            for st, v in sorted(stations.items())
        },
    }
    lines = [
        f"\n## {cell}\n",
        f"windows {len(allv)}; PoA-hat p50 {p50:.3f}  p90 {p90:.3f}  max {allv[-1]:.3f}  ratio-of-sums {overall:.3f}\n",
        "",
        "| station | windows | p50 | p90 | max |",
        "| --- | --- | --- | --- | --- |",
    ]
    for st, v in sorted(stations.items()):
        sv = sorted(v)
        lines.append(
            f"| {st} | {len(v)} | {sv[len(sv) // 2]:.3f} | "
            f"{sv[min(int(0.9 * len(sv)), len(sv) - 1)]:.3f} | {max(sv):.3f} |"
        )
    report_md.append("\n".join(lines) + "\n")

md_path = f"{out_dir}/game3-poa-replay.md"
with open(md_path, "w") as f:
    f.write("\n".join(report_md))
json_path = f"{out_dir}/game3-poa-replay.json"
with open(json_path, "w") as f:
    json.dump(
        {
            "scope": "prefill-leg game; windowed exact matching OPT; OPT "
                     "frictionless within the window; resident work "
                     "reconstructed from the journal; the matching serves "
                     "every profiled engine once per window, so PoA-hat < 1 "
                     "on an asymmetric fleet reads as the constraint binding",
            "profiles": profiles_path,
            "station_s": phase_s,
            "runs": report_json,
        },
        f,
        indent=2,
    )
    f.write("\n")
print(f"wrote {md_path} and {json_path}")
for cell, r in report_json.items():
    print(f"  {cell:<36} p50 {r['p50']:.3f}  p90 {r['p90']:.3f}  ratio-of-sums {r['ratio_of_sums']:.3f}")
PYEOF
