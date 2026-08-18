#!/usr/bin/env bash
# One comparison run: adaptive, static, aggregated on the same trace and rates.
# TRACE_FILE=<timestamped jsonl> replays a recorded trace instead of the
# built-in phases; --rates then scales its timestamps (1.0 = as recorded).
#
# Run it on the node that hosts the router. Each arm gets its own router, its
# own journal and its own bench output under runs/local/comparison. The arms
# differ only in the config: adaptive is the shipped planner default, static
# pins roles with a reactive controller whose thresholds no load reaches and
# whose trigger must persist a million passes, and aggregated additionally
# starts every engine in the decode pool so placement keeps both phases
# together (KV crosses only on the failover retry). Every derived config is
# validated through FleetConfig.load before an arm runs, because a config
# that fails at startup reads like a 0% result.
set -u
cd "$(dirname "$0")/.."
V=.venv/bin
RATES="${RATES:-0.05,0.1,0.15}"
PHASE_SECONDS="${PHASE_SECONDS:-180}"
OUT=runs/local/comparison
TTFT=$(python3 -c 'import json;print(json.load(open("config/fleet.local.json"))["slo"]["ttft_s"])')
TPOT=$(python3 -c 'import json;print(json.load(open("config/fleet.local.json"))["slo"]["tpot_s"])')
MODEL=$(python3 -c 'import json;print(json.load(open("config/fleet.local.json"))["model"])')
mkdir -p "$OUT"

# Derived here rather than kept on the node, because `narwhal-fleet deploy`
# replaces the tree and an arm whose config went missing scores 0% at every
# rate, which reads like a result.
BASE=config/fleet.local.json
$V/python - "$BASE" <<'PYEOF'
import json, pathlib, sys
from narwhal.config import FleetConfig

base = json.loads(pathlib.Path(sys.argv[1]).read_text())
pinned = dict(base.get("thresholds", {}))
pinned.update(
    {"expand": 1e9, "shrink": 0.0, "cooldown_s": 1e9,
     "panic_ratio": 0.0, "sustained_intervals": 1_000_000}
)

static = json.loads(json.dumps(base))
static["thresholds"] = pinned
static["controller"] = "reactive"
# Truly static pins every engine: Algorithm 1's step-3 flip toward
# decode consults no threshold.
for e in static["engines"]:
    e["pin"] = True
pathlib.Path("config/fleet.static.json").write_text(json.dumps(static, indent=2) + "\n")

agg = json.loads(json.dumps(static))
for e in agg["engines"]:
    e["role"] = "decode"
pathlib.Path("config/fleet.aggregated.json").write_text(json.dumps(agg, indent=2) + "\n")

for name in ("static", "aggregated"):
    FleetConfig.load(f"config/fleet.{name}.json")
print("derived arm configs validate")
PYEOF

start_router () {
  local name="$1" cfg="$2" tag="$3"
  pkill -f narwhal-serve
  for _ in $(seq 1 30); do
    curl -sf http://localhost:8011/health >/dev/null 2>&1 || break
    sleep 1
  done
  rm -f "$OUT/$name.$tag.journal.jsonl"
  setsid $V/narwhal-serve --fleet "$cfg" --host :: --port 8011 \
    --journal "$OUT/$name.$tag.journal.jsonl" </dev/null \
    > "$OUT/$name.$tag.router.log" 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf http://localhost:8011/health >/dev/null 2>&1 && break; sleep 1
  done
  curl -sf http://localhost:8011/health >/dev/null 2>&1
}

# One router per rate, not one per arm. The adaptive router carries its pool
# split forward, so a rate swept after another starts wherever the last one
# left it while the pinned arms start clean. A paired comparison needs arms
# aligned on identical windows, so every (arm, rate) begins from the same
# configured split.
run_arm () {
  local name="$1" cfg="$2"
  : > "$OUT/$name.bench.log"
  for rate in ${RATES//,/ }; do
    if ! start_router "$name" "$cfg" "$rate"; then
      echo "== $name FAILED TO START at $rate"; tail -5 "$OUT/$name.$rate.router.log"; return 1
    fi
    cp "$cfg" "$OUT/$name.$rate.config.json"
    curl -s http://localhost:8011/arrow/state > "$OUT/$name.$rate.state.before.json"
    TRACE_ARGS=()
    [ -n "${TRACE_FILE:-}" ] && TRACE_ARGS=(--trace-file "$TRACE_FILE")
    $V/narwhal-bench --base http://localhost:8011 --model "$MODEL" --force \
      --ttft-slo "$TTFT" --tpot-slo "$TPOT" --rates "$rate" \
      --phase-seconds "$PHASE_SECONDS" "${TRACE_ARGS[@]}" \
      --out "$OUT/$name.$rate.samples.jsonl" >> "$OUT/$name.bench.log" 2>&1
    curl -s http://localhost:8011/arrow/state > "$OUT/$name.$rate.state.after.json"
  done
  echo "== $name"; grep -vE "^ +rate|^$" "$OUT/$name.bench.log" | grep -v "sustained at"
}

run_arm adaptive   config/fleet.local.json
run_arm static     config/fleet.static.json
run_arm aggregated config/fleet.aggregated.json
pkill -f narwhal-serve
echo "ALL ARMS DONE"
