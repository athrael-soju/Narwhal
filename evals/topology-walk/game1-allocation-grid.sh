#!/usr/bin/env bash
# Allocation-grid eval: price the allocation game at a fixed workload.
#
# The topology walk scores controllers on a moving optimum, but it never
# measures the game itself: at every moment the controller is chasing, so the
# fleet never sits still long enough to read the payoff surface. This eval
# holds the workload still and moves the split instead. Five pinned static
# fleets, one per split, all serving the same phase:
#
#     1P5D -> 2P4D -> 3P3D -> 4P2D -> 5P1D, each at the GRID workload
#
# What it measures. The allocation paper claims a unique variational
# equilibrium that equalizes marginal violation rates across the pools. On a
# real fleet that claim reads as: goodput peaks at one split, attainment
# degrades monotonically away from it, and the marginal step between adjacent
# splits changes sign at the peak. The walk says the optimum for the default
# grid workload (60-100 output tokens at 2.0 req/s) is 3P3D; this eval checks
# that the measured surface agrees, split by split, at one seed.
#
# Each cell is the walk's `static` discipline pointed at one split: every
# engine pinned, thresholds unreachable, controller reactive, so no flip path
# can fire and the split the phase opens with is the split it dies with. The
# workload, the seed, and the budgets are identical across cells; the split
# is the only variable.
#
# Usage:
#   bash evals/topology-walk/game1-allocation-grid.sh
#   SPLITS="3 4" PHASE_SECONDS=600 bash evals/topology-walk/game1-allocation-grid.sh
#
# Environment:
#   FLEET           base fleet config              (default config/fleet.local.json)
#   SEED            client seed; one per run       (default 7)
#   SPLITS          prefill counts to measure      (default: 1 2 3 4 5)
#   PHASE_SECONDS   seconds per split              (default 1200)
#   GRID_ISL        input band                     (default 12000-16000)
#   GRID_OSL        output band                    (default 60-100)
#   GRID_RATE       arrival rate multiplier        (default 2.0)
#   PORT            router port                    (default 8011)
#   OUT             artifact directory             (default runs/local/eval-allocation-grid)
#
# One seed per invocation, same as the walk. Cost: |SPLITS| x PHASE_SECONDS of
# load - about twenty minutes per split at the default, plus router turnarounds.
# Preflight refuses a fleet whose config pins or floors anything: a pinned
# engine silently narrows the reachable grid, and a floor above 1 makes the
# 1P5D cell a lie (it would score as the floor permits - check
# min_prefill in the cell configs before quoting the lean end).
#
# Score with:
#   narwhal-report --dir $OUT --ttft-slo <ttft> --tpot-slo <tpot> --phases 1
# The trailing table this script prints is the surface at a glance: attainment
# per split under both conventions, with refusals named beside the number.
set -u
cd "$(dirname "$0")/../.."

BIN="${NARWHAL_BIN:-}"
if [ -z "$BIN" ] && [ -x .venv/bin/narwhal-serve ]; then BIN=".venv/bin/"; fi

FLEET="${FLEET:-config/fleet.local.json}"
SEED="${SEED:-7}"
SPLITS="${SPLITS:-1 2 3 4 5}"
S="${PHASE_SECONDS:-1200}"
ISL="${GRID_ISL:-12000-16000}"
OSL="${GRID_OSL:-60-100}"
RATE="${GRID_RATE:-2.0}"
PORT="${PORT:-8011}"
OUT="${OUT:-runs/local/eval-allocation-grid}"

[ -f "$FLEET" ] || { echo "no such fleet config: $FLEET"; exit 2; }
TTFT=$(python3 -c "import json;print(json.load(open('$FLEET'))['slo']['ttft_s'])")
TPOT=$(python3 -c "import json;print(json.load(open('$FLEET'))['slo']['tpot_s'])")
MODEL=$(python3 -c "import json;print(json.load(open('$FLEET'))['model'])")
mkdir -p "$OUT"

# One phase, the walk's mid-ladder shape: the input band constant, the output
# band and rate where the walk claims 3P3D is optimal.
PHASE="$S:$ISL:$OSL:$RATE"

# Splits are pinned static fleets. The frozen-thresholds half is the walk's
# own trick (see run.sh): Algorithm 2 never fires at a billion sustained
# intervals, and no flip path moves a pinned engine, so the opening split is
# the whole measurement. First-k engines take prefill, in fleet order; both
# pools are non-empty by construction (k in 1..5 over six engines).
FLEET="$FLEET" OUT="$OUT" SPLITS="$SPLITS" python3 - <<'PYEOF'
import json, os
base = json.load(open(os.environ["FLEET"])); out = os.environ["OUT"]
base.pop("_", None)
n = len(base["engines"])
if base.get("min_prefill", 1) > 1:
    raise SystemExit(f"FLEET carries min_prefill={base['min_prefill']}: the lean end would score as the floor permits")
if any(e.get("pin") for e in base["engines"]):
    raise SystemExit("FLEET carries pinned engines: the grid must derive pins itself")
FROZEN = {"expand": 1e9, "shrink": 0.5, "cooldown_s": 1e9,
          "sustained_intervals": 1000000000}
for k in [int(x) for x in os.environ["SPLITS"].split()]:
    if not 1 <= k <= n - 1:
        raise SystemExit(f"split {k}P is out of reach on {n} engines")
    cfg = json.loads(json.dumps(base))
    cfg["thresholds"] = dict(FROZEN); cfg["controller"] = "reactive"
    for i, e in enumerate(cfg["engines"]):
        e["role"] = "prefill" if i < k else "decode"
        e["pin"] = True
    name = f"{k}p{n - k}d"
    open(f"{out}/fleet.grid-{name}.json", "w").write(json.dumps(cfg, indent=2) + "\n")
PYEOF

echo "== preflight"
${BIN}narwhal-check --fleet "$FLEET" --ring --repeats 3 > "$OUT/check.before.log" 2>&1 \
  || { echo "PREFLIGHT FAILED - see $OUT/check.before.log"; exit 1; }

run_cell () {
  local name="$1" cfg="$OUT/fleet.grid-$1.json" tag="seed$SEED"
  [ -f "$cfg" ] || { echo "== $name: no such cell"; return 1; }
  # Every split gets a router of its own, so nothing carries over between them.
  pkill -f narwhal-serve
  for _ in $(seq 1 30); do curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 || break; sleep 1; done
  rm -f runs/local/journal.jsonl
  setsid ${BIN}narwhal-serve --fleet "$cfg" --host :: --port "$PORT" </dev/null \
    > "$OUT/grid-$name.$tag.router.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
  if ! curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
    echo "== $name FAILED TO START"; tail -5 "$OUT/grid-$name.$tag.router.log"; return 1
  fi
  cp "$cfg" "$OUT/grid-$name.$tag.config.json"
  curl -s "http://localhost:$PORT/arrow/state" > "$OUT/grid-$name.$tag.state.before.json"
  echo "== grid-$name (seed $SEED, $PHASE)"
  ${BIN}narwhal-bench --base "http://localhost:$PORT" --model "$MODEL" \
    --ttft-slo "$TTFT" --tpot-slo "$TPOT" --seed "$SEED" --rates 1.0 \
    --segments "$PHASE" --out "$OUT/grid-$name.$tag.samples.jsonl" \
    >> "$OUT/grid-$name.$tag.bench.log" 2>&1
  curl -s "http://localhost:$PORT/arrow/state" > "$OUT/grid-$name.$tag.state.after.json"
  cp runs/local/journal.jsonl "$OUT/grid-$name.$tag.journal.jsonl" 2>/dev/null
  grep -vE "^ +rate|^$|sustained at" "$OUT/grid-$name.$tag.bench.log" | tail -2
}

for k in $SPLITS; do
  N=$(python3 -c "import json;print(len(json.load(open('$FLEET'))['engines']))")
  name="${k}p$((N - k))d"
  : > "$OUT/grid-$name.seed$SEED.bench.log"
  run_cell "$name" || echo "== grid-$name did not complete"
done

pkill -f narwhal-serve

# The surface at a glance: offered, attainment under both conventions, and
# served output tokens per split. Client attainment counts every sample against
# both budgets; journal attainment holds refusals out of the denominator, as
# narwhal-report does; refused is the journal's own count. The equilibrium
# prediction is a peak at the walk's claimed optimum with adjacent marginals
# flipping sign there; read it off the table, score formally with
# narwhal-report.
OUT="$OUT" SEED="$SEED" TTFT="$TTFT" TPOT="$TPOT" python3 - <<'PYEOF'
import json, os
out, seed = os.environ["OUT"], os.environ["SEED"]
ttft, tpot = float(os.environ["TTFT"]), float(os.environ["TPOT"])
print(f"{'split':>7} {'offered':>8} {'client':>8} {'journal':>8} {'refused':>8}")
for k in range(1, 20):
    path = f"{out}/grid-{k}p*.seed{seed}.samples.jsonl"
    import glob
    hits = glob.glob(path)
    if not hits:
        continue
    name = hits[0].rsplit("/grid-", 1)[1].split(".seed")[0]
    rows = [json.loads(x) for x in open(hits[0]) if x.strip() and '"rid"' in x]
    offered = len(rows)
    met = sum(
        1 for r in rows
        if not r.get("error")
        and (r.get("ttft_s") or 9e9) <= ttft
        and (r.get("tpot_s") or 9e9) <= tpot
    )
    try:
        j = [json.loads(x) for x in open(f"{out}/grid-{name}.seed{seed}.journal.jsonl")
             if x.strip() and '"arrived"' in x]
        refused = sum(1 for r in j if r.get("refused"))
        served = [r for r in j if not r.get("refused")]
        jmet = sum(1 for r in served if not r.get("error"))
        jline = f"{jmet / len(served) * 100:6.1f}%" if served else "     -"
    except FileNotFoundError:
        refused, jline = 0, "     -"
    if offered:
        print(f"{name:>7} {offered:>8} {met / offered * 100:6.1f}% {jline:>8} {refused:>8}")
print("client counts refusals as misses through error rows; journal holds them apart.")
PYEOF

echo
echo "== grid done; router stopped"
echo "   score:  ${BIN}narwhal-report --dir $OUT --ttft-slo $TTFT --tpot-slo $TPOT --phases 1"
