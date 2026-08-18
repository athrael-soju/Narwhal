#!/usr/bin/env bash
# Topology-walk eval: score fleet architectures against a moving optimum.
#
# Traffic shape decides how a disaggregated fleet should be split. Short
# outputs at a high arrival rate want most engines prefilling; long outputs at
# a low rate want most of them decoding. A fleet that holds one split is right
# only while the traffic holds still.
#
# This eval walks the optimum across the fleet and back, and scores each
# architecture on the same ladder with the same seed. Eight phases, one flip
# apart, out and back:
#
#     4P2D -> 5P1D -> 4P2D -> 3P3D -> 2P4D -> 1P5D -> 2P4D -> 3P3D
#
# Each phase names an intent, not a setting. Nothing tells the controller where
# the optimum went; the load shape moves and the controller has to find it. A
# fleet with a prefill floor cannot reach the lean end, and the 1P5D phase
# silently scores as whatever the floor permits - so
# check the floor before reading the result. At the lean end one engine carries
# the fleet's whole KV egress, so a green `narwhal-check --ring` is a
# prerequisite: a fabric fault there reads as a controller result.
#
# The cells, all on one fleet, one seed, architecture as the only variable:
#
#   planner     adaptive hot-swap, windowed target-state planner (the default)
#   reactive    adaptive hot-swap, Algorithm 2 thresholds
#   coldswap    the planner charged 300 s out-of-service per flip
#   static      one fixed split, every engine pinned, no flip can fire
#   aggregated  no disaggregation; every engine both-phases locally
#
# Usage:
#   bash evals/topology-walk/run.sh
#   SEED=11 bash evals/topology-walk/run.sh
#   CELLS="planner static" PHASE_SECONDS=60 bash evals/topology-walk/run.sh
#
# Environment:
#   FLEET           base fleet config              (default config/fleet.local.json)
#   SEED            client seed; one per run       (default 7)
#   CELLS           which cells to run             (default: all five)
#   PHASE_SECONDS   seconds per phase              (default 1200)
#   PORT            router port                    (default 8011)
#   OUT             artifact directory             (default runs/local/eval-topology-walk)
#
# One seed per invocation. The seed is the only thing that varies between
# replicates, so run this again with SEED=<n> for each additional seed rather
# than editing anything. Error bars need at least two.
#
# Cost: five cells x 8 phases x PHASE_SECONDS. At the 1200 s default that is
# 9,600 s of load per cell and about 13 h 20 m for the set. Shake the plumbing
# out first with PHASE_SECONDS=60, which covers all five cells in 40 minutes
# and whose scores mean nothing.
set -u
cd "$(dirname "$0")/../.."

BIN="${NARWHAL_BIN:-}"
if [ -z "$BIN" ] && [ -x .venv/bin/narwhal-serve ]; then BIN=".venv/bin/"; fi

FLEET="${FLEET:-config/fleet.local.json}"
SEED="${SEED:-7}"
CELLS="${CELLS:-planner reactive coldswap static aggregated}"
S="${PHASE_SECONDS:-1200}"
PORT="${PORT:-8011}"
OUT="${OUT:-runs/local/eval-topology-walk}"
HERE="evals/topology-walk"

[ -f "$FLEET" ] || { echo "no such fleet config: $FLEET"; exit 2; }
TTFT=$(python3 -c "import json;print(json.load(open('$FLEET'))['slo']['ttft_s'])")
TPOT=$(python3 -c "import json;print(json.load(open('$FLEET'))['slo']['tpot_s'])")
MODEL=$(python3 -c "import json;print(json.load(open('$FLEET'))['model'])")
mkdir -p "$OUT"

# Each phase is dur:isl_lo-isl_hi:osl_lo-osl_hi:rate. The output length and the
# rate together set which split is optimal; the input band is constant so the
# ladder moves one variable.
P5="12000-16000:1-4:3.8"      ; P4="12000-16000:20-40:3.0"
P3="12000-16000:60-100:2.0"   ; P2="12000-16000:120-180:1.2"
P1="12000-16000:350-450:0.6"
WALK="$S:$P4,$S:$P5,$S:$P4,$S:$P3,$S:$P2,$S:$P1,$S:$P2,$S:$P3"

# Each cell's config is derived from your fleet so that the architecture is the
# only thing that differs between them. evals/topology-walk/fleet.*.json are the
# same five shapes rendered against the shipped preset, for reading.
FLEET="$FLEET" OUT="$OUT" python3 - <<'PYEOF'
import json, os
base = json.load(open(os.environ["FLEET"])); out = os.environ["OUT"]
base.pop("_", None)
# Unreachable thresholds: a trigger must sustain a billion monitoring passes and
# P->D pays a 31-year cooldown, silencing Algorithm 2 and the step-3 flip toward
# decode. Step 3 fires inside placement when nothing meets the SLO, and toward
# prefill it reads only the shrink guard, which a quiet decode pool passes - the
# pins close that door, since no flip path moves a pinned engine. The controller
# must be reactive because the planner prices demand and never reads thresholds:
# with anything unpinned it would re-split anyway.
FROZEN = {"expand": 1e9, "shrink": 0.5, "cooldown_s": 1e9,
          "sustained_intervals": 1000000000}
cells = {}
cells["planner"] = json.loads(json.dumps(base))
cells["reactive"] = {**json.loads(json.dumps(base)), "controller": "reactive"}
cells["coldswap"] = {**json.loads(json.dumps(base)), "flip_offline_s": 300.0}
static = json.loads(json.dumps(base))
static["thresholds"] = dict(FROZEN); static["controller"] = "reactive"
for e in static["engines"]:
    e["pin"] = True
cells["static"] = static
agg = json.loads(json.dumps(static))
for e in agg["engines"]:
    e["role"] = "decode"
cells["aggregated"] = agg
for name, cfg in cells.items():
    open(f"{out}/fleet.{name}.json", "w").write(json.dumps(cfg, indent=2) + "\n")
PYEOF

echo "== preflight"
${BIN}narwhal-check --fleet "$FLEET" --ring --repeats 3 > "$OUT/check.before.log" 2>&1 \
  || { echo "PREFLIGHT FAILED - see $OUT/check.before.log"; exit 1; }

run_cell () {
  local name="$1" cfg="$OUT/fleet.$1.json" tag="seed$SEED"
  [ -f "$cfg" ] || { echo "== $name: no such cell"; return 1; }
  # Every cell gets a router of its own, so nothing carries over between them.
  pkill -f narwhal-serve
  for _ in $(seq 1 30); do curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 || break; sleep 1; done
  rm -f runs/local/journal.jsonl
  setsid ${BIN}narwhal-serve --fleet "$cfg" --host :: --port "$PORT" </dev/null \
    > "$OUT/$name.$tag.router.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
  if ! curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
    echo "== $name FAILED TO START"; tail -5 "$OUT/$name.$tag.router.log"; return 1
  fi
  cp "$cfg" "$OUT/$name.$tag.config.json"
  curl -s "http://localhost:$PORT/arrow/state" > "$OUT/$name.$tag.state.before.json"
  echo "== $name (seed $SEED)"
  ${BIN}narwhal-bench --base "http://localhost:$PORT" --model "$MODEL" \
    --ttft-slo "$TTFT" --tpot-slo "$TPOT" --force --seed "$SEED" --rates 1.0 \
    --segments "$WALK" --out "$OUT/$name.$tag.samples.jsonl" \
    >> "$OUT/$name.$tag.bench.log" 2>&1
  curl -s "http://localhost:$PORT/arrow/state" > "$OUT/$name.$tag.state.after.json"
  cp runs/local/journal.jsonl "$OUT/$name.$tag.journal.jsonl" 2>/dev/null
  grep -vE "^ +rate|^$|sustained at" "$OUT/$name.$tag.bench.log" | tail -2
}

for cell in $CELLS; do
  : > "$OUT/$cell.seed$SEED.bench.log"
  run_cell "$cell" || echo "== $cell did not complete"
done

pkill -f narwhal-serve
echo
echo "== all cells done; router stopped"
echo "   score:  ${BIN}narwhal-report --dir $OUT --ttft-slo $TTFT --tpot-slo $TPOT --phases 8"
echo "   reached splits per cell are in <cell>.seed$SEED.state.after.json (pools + flips)"
