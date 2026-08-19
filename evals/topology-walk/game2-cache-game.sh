#!/usr/bin/env bash
# Cache-game eval: reintroduce the caching game on purpose and price it.
#
# The paper's Game 2 answer is subtraction: stateless engines turn cache
# placement into a routing cost, and the game stops existing. This eval checks
# the price from the other side - put the game back in (engines caching,
# prefixes recurring) and measure what the router's two prefix postures
# recover:
#
#   off       warmth ignored; shared heads recomputed where the request lands
#   affinity  prefill_affinity: shared-prefix legs return to the warm engine
#   coop      prefix_coop: warmth priced as a fading discount inside Algorithm 1
#
# Each arm gets its own trace: N long shared prefixes fanned across many
# requests, with prefix-id ranges disjoint between arms. That separation is
# what makes the cross-arm comparison a measurement rather than a handoff from
# the previous arm's engine caches. Every request's tail is unique: the
# generator never repeats a (prefix_id, input_len) pair, and narwhal-bench
# salts every prompt it synthesizes, so no two requests ever carry the same
# full text. On an engine image that asserts on full-cache hits, that
# uniqueness is load-bearing - a repeated prompt is a dead engine, not a cache
# hit.
#
# PRECONDITION: the engines must be running with prefix caching on, or every
# arm measures noise. This script refuses to run against a fleet whose engines
# export no prefix-cache counters. The operator owns the wave in and the wave
# back out; this script only drives the arms. (The reference fleet's standing
# wave is caching-off precisely because of the assert above.)
#
# Usage:
#   bash evals/topology-walk/game2-cache-game.sh
#   PREFIXES=48 DURATION=1200 RATES=1.0,2.0 bash evals/topology-walk/game2-cache-game.sh
#
# Environment:
#   FLEET           base fleet config              (default config/fleet.local.json)
#   SEED            trace generator seed           (default 7)
#   PREFIXES        distinct shared prefixes       (default 24)
#   PREFIX_ID_OFFSET first arm's prefix-id base    (default 0; arms add 0/100/200)
#   PREFIX_TOKENS   tokens per shared prefix       (default 8000)
#   TAIL_TOKENS     unique tail band, lo-hi        (default 200-2600)
#   OUT_TOKENS      decode band, lo-hi             (default 60-100)
#   DURATION        trace length, seconds          (default 900)
#   TRACE_RATE      Poisson rate baked into trace  (default 1.0)
#   RATES           timestamp constants to replay  (default 1.0,2.0)
#   PORT            router port                    (default 8011)
#   OUT             artifact directory             (default runs/local/eval-cache-game)
#
# Cost: 3 arms x |RATES| x DURATION of load (45 minutes at the defaults) plus
# router turnarounds. Score with:
#   narwhal-report --dir $OUT --ttft-slo <ttft> --tpot-slo <tpot> --phases 1
# The trailing table reads the game directly: first-open TTFT against
# repeat-open TTFT per arm (hashed by the trace's own prefix ids), attainment
# per arm, and each engine's prefix-cache counter deltas while the arm ran.
set -u
cd "$(dirname "$0")/../.."

BIN="${NARWHAL_BIN:-}"
if [ -z "$BIN" ] && [ -x .venv/bin/narwhal-serve ]; then BIN=".venv/bin/"; fi

FLEET="${FLEET:-config/fleet.local.json}"
SEED="${SEED:-7}"
PREFIXES="${PREFIXES:-24}"
PREFIX_ID_OFFSET="${PREFIX_ID_OFFSET:-0}"
PREFIX_TOKENS="${PREFIX_TOKENS:-8000}"
TAIL_TOKENS="${TAIL_TOKENS:-200-2600}"
OUT_TOKENS="${OUT_TOKENS:-60-100}"
DURATION="${DURATION:-900}"
TRACE_RATE="${TRACE_RATE:-1.0}"
RATES="${RATES:-1.0,2.0}"
PORT="${PORT:-8011}"
OUT="${OUT:-runs/local/eval-cache-game}"

[ -f "$FLEET" ] || { echo "no such fleet config: $FLEET"; exit 2; }
TTFT=$(python3 -c "import json;print(json.load(open('$FLEET'))['slo']['ttft_s'])")
TPOT=$(python3 -c "import json;print(json.load(open('$FLEET'))['slo']['tpot_s'])")
MODEL=$(python3 -c "import json;print(json.load(open('$FLEET'))['model'])")
mkdir -p "$OUT"

# The game needs a cache to contest. Refuse early when there is none: without
# counters the arms differ only in routing noise, and the result would read as
# a pricing while measuring weather.
echo "== precondition: engines must export prefix-cache counters"
FLEET="$FLEET" OUT="$OUT" python3 - <<'PYEOF' || exit 2
import json, os, sys, urllib.request
engines = json.load(open(os.environ["FLEET"]))["engines"]
found = {}
for e in engines:
    url = e["url"].rstrip("/") + "/metrics"
    try:
        text = urllib.request.urlopen(url, timeout=10).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - any failure is a precondition failure
        raise SystemExit(f"{e['iid']} unreachable at {url}: {exc}")
    series = [l for l in text.splitlines() if "prefix_cache" in l and not l.startswith("#")]
    open(f"{os.environ['OUT']}/precondition.{e['iid']}.metrics.txt", "w").write("\n".join(series) + "\n")
    if series:
        found[e["iid"]] = len(series)
if not found:
    raise SystemExit(
        "no engine exports prefix-cache counters: the fleet is not caching; "
        "wave the engines in with PREFIX_CACHING=on first"
    )
print("  caching counters on:", ", ".join(f"{i}({n})" for i, n in sorted(found.items())))
have = set(found)
missing = [e["iid"] for e in engines if e["iid"] not in have]
if missing:
    print(f"  note: no prefix-cache series on {', '.join(missing)} (partial cache is still a measurement)")
PYEOF

make_trace () {
  # One trace per arm, with disjoint ctx tags, so an arm cannot warm the next
  # arm's prefixes through persistent engine caches. The shared RNG keeps the
  # offer sequence identical apart from the range the prefix ids live in.
  local arm="$1"
  local offset="$2"
  local trace="$OUT/trace.$arm.jsonl"
  SEED="$SEED" PREFIXES="$PREFIXES" PREFIX_ID_OFFSET="$offset" \
  PREFIX_TOKENS="$PREFIX_TOKENS" TAIL_TOKENS="$TAIL_TOKENS" \
  OUT_TOKENS="$OUT_TOKENS" DURATION="$DURATION" TRACE_RATE="$TRACE_RATE" \
  TRACE="$trace" python3 - <<'PYEOF'
import json, os, random
rng = random.Random(int(os.environ["SEED"]))
prefixes = int(os.environ["PREFIXES"])
offset = int(os.environ["PREFIX_ID_OFFSET"])
plen = int(os.environ["PREFIX_TOKENS"])
tlo, thi = (int(x) for x in os.environ["TAIL_TOKENS"].split("-"))
olo, ohi = (int(x) for x in os.environ["OUT_TOKENS"].split("-"))
duration = float(os.environ["DURATION"])
rate = float(os.environ["TRACE_RATE"])
used, rows, t = set(), [], 0.0
while True:
    t += rng.expovariate(rate)
    if t >= duration:
        break
    pid = offset + rng.randrange(prefixes)
    for _ in range(1000):
        il = plen + rng.randint(tlo, thi)
        if (pid, il) not in used:
            break
    else:
        raise SystemExit(f"tail band {tlo}-{thi} too narrow for {prefixes} prefixes")
    used.add((pid, il))
    rows.append({"at": t, "input_len": il, "output_len": rng.randint(olo, ohi),
                 "prefix_id": pid, "prefix_len": plen})
open(os.environ["TRACE"], "w").write("".join(json.dumps(r) + "\n" for r in rows))
from collections import Counter
counts = Counter(r["prefix_id"] for r in rows)
print(f"== trace: {len(rows)} requests over {duration:g}s at {rate:g} req/s; "
      f"{prefixes} prefixes of {plen} tokens at ids {offset}..{offset + prefixes - 1}, "
      f"least-dealt prefix opens {min(counts.values())} requests")
PYEOF
}

# Arms: the fleet as configured, then one knob at a time. Both knobs at once is
# a config error by design (the validator refuses it), so the arms are these
# three and nothing else.
FLEET="$FLEET" OUT="$OUT" python3 - <<'PYEOF'
import json, os
base = json.load(open(os.environ["FLEET"])); out = os.environ["OUT"]
base.pop("_", None)
aff = json.loads(json.dumps(base)); aff["prefill_affinity"] = True
open(f"{out}/fleet.affinity.json", "w").write(json.dumps(aff, indent=2) + "\n")
coop = json.loads(json.dumps(base)); coop["prefix_coop"] = True
open(f"{out}/fleet.coop.json", "w").write(json.dumps(coop, indent=2) + "\n")
open(f"{out}/fleet.off.json", "w").write(json.dumps(base, indent=2) + "\n")
PYEOF

scrape_engines () {
  # one prefix-cache series dump per engine, named for the arm and moment
  OUT="$OUT" FLEET="$FLEET" STEM="$1" python3 - <<'PYEOF'
import json, os, urllib.request
engines = json.load(open(os.environ["FLEET"]))["engines"]
stem = os.environ["STEM"]
for e in engines:
    url = e["url"].rstrip("/") + "/metrics"
    try:
        text = urllib.request.urlopen(url, timeout=10).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a dropped scrape is a gap, not a failed arm
        continue
    series = [l for l in text.splitlines() if "prefix_cache" in l and not l.startswith("#")]
    open(f"{os.environ['OUT']}/{stem}.{e['iid']}.metrics.txt", "w").write("\n".join(series) + "\n")
PYEOF
}

prefix_offset () {
  case "$1" in
    off) echo "$PREFIX_ID_OFFSET" ;;
    affinity) echo $((PREFIX_ID_OFFSET + 100)) ;;
    coop) echo $((PREFIX_ID_OFFSET + 200)) ;;
    *) echo "unknown arm $1"; exit 2 ;;
  esac
}

run_arm () {
  local name="$1" cfg="$2" rate="$3" tag="$4"
  local offset trace
  offset=$(prefix_offset "$name")
  trace="$OUT/trace.$name.jsonl"
  if [ ! -s "$trace" ]; then
    make_trace "$name" "$offset" || return 1
  fi
  # Every arm gets a router of its own, so no warmth estimate carries over.
  pkill -f narwhal-serve
  for _ in $(seq 1 30); do curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 || break; sleep 1; done
  rm -f runs/local/journal.jsonl
  setsid ${BIN}narwhal-serve --fleet "$cfg" --host :: --port "$PORT" </dev/null \
    > "$OUT/$name.$tag.router.log" 2>&1 &
  for _ in $(seq 1 30); do curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
  if ! curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then
    echo "== $name FAILED TO START" ; tail -5 "$OUT/$name.$tag.router.log"; return 1
  fi
  cp "$cfg" "$OUT/$name.$tag.config.json"
  curl -s "http://localhost:$PORT/arrow/state" > "$OUT/$name.$tag.state.before.json"
  scrape_engines "$name.$tag.before"
  echo "== $name at rate $rate"
  ${BIN}narwhal-bench --base "http://localhost:$PORT" --model "$MODEL" \
    --ttft-slo "$TTFT" --tpot-slo "$TPOT" --force \
    --rates "$rate" --trace-file "$trace" \
    --out "$OUT/$name.$tag.samples.jsonl" >> "$OUT/$name.bench.log" 2>&1
  curl -s "http://localhost:$PORT/arrow/state" > "$OUT/$name.$tag.state.after.json"
  cp runs/local/journal.jsonl "$OUT/$name.$tag.journal.jsonl" 2>/dev/null
  scrape_engines "$name.$tag.after"
  grep -vE "^ +rate|^$|sustained at" "$OUT/$name.bench.log" | tail -1
}

for name in off affinity coop; do
  : > "$OUT/$name.bench.log"
  for rate in $(echo "$RATES" | tr , " "); do
    tag=$(echo "$rate" | tr . p)
    run_arm "$name" "$OUT/fleet.$name.json" "$rate" "$tag" || echo "== $name at $rate did not complete"
  done
done

pkill -f narwhal-serve

# The game's price, read directly: for each arm, first opens of a prefix pay
# the cold TTFT and repeats enjoy the warm one - if the cache works and the
# router's posture lets it. samples rows join the trace by rid index (b<idx>).
OUT="$OUT" RATES="$RATES" TTFT="$TTFT" TPOT="$TPOT" python3 - <<'PYEOF'
import glob, json, os, statistics
out = os.environ["OUT"]
ttft, tpot = float(os.environ["TTFT"]), float(os.environ["TPOT"])
traces = {}
for arm in ("off", "affinity", "coop"):
    path = f"{out}/trace.{arm}.jsonl"
    if os.path.exists(path):
        traces[arm] = [json.loads(x) for x in open(path) if x.strip()]
if not traces:
    raise SystemExit("no per-arm traces found; nothing to read")
print(f"{'arm':>9} {'rate':>5} {'offered':>8} {'client':>8} {'cold ttft':>10} {'warm ttft':>10} {'warm gain':>10}")
for arm in ("off", "affinity", "coop"):
    trace = traces.get(arm)
    if trace is None:
        continue
    for rate in os.environ["RATES"].split(","):
        tag = rate.replace(".", "p")
        hits = glob.glob(f"{out}/{arm}.{tag}.samples.jsonl")
        if not hits:
            continue
        rows = [json.loads(x) for x in open(hits[0]) if x.strip() and '"rid"' in x]
        offered = len(rows)
        met = sum(1 for r in rows if not r.get("error")
                  and (r.get("ttft_s") or 9e9) <= ttft and (r.get("tpot_s") or 9e9) <= tpot)
        seen, cold, warm = set(), [], []
        for r in rows:
            idx = int(r["rid"][1:])
            if idx >= len(trace) or r.get("ttft_s") is None or r.get("error"):
                continue
            pid = trace[idx].get("prefix_id")
            if pid is None:
                continue
            (cold if pid not in seen else warm).append(r["ttft_s"])
            seen.add(pid)
        if cold and warm:
            print(f"{arm:>9} {rate:>5} {offered:>8} {met / offered * 100 if offered else 0:7.1f}%"
                  f" {statistics.median(cold):>9.2f}s {statistics.median(warm):>9.2f}s"
                  f" {statistics.median(cold) - statistics.median(warm):>9.2f}s")
        else:
            print(f"{arm:>9} {rate:>5} {offered:>8} {met / offered * 100 if offered else 0:7.1f}%"
                  f" {'(no repeated prefix survived)':>22}")
print("cold/warm are medians over per-prefix first and repeat opens; the samples")
print("carry no prefix ids, so the join runs on bench's rid = trace row index.")
PYEOF

# Engine-side corroboration where the counters allow it: before/after deltas of
# any numeric prefix-cache series, summed over engines per arm.
OUT="$OUT" RATES="$RATES" python3 - <<'PYEOF'
import glob, os
out = os.environ["OUT"]
def totals(stem):
    vals = {}
    for path in glob.glob(f"{out}/{stem}.*.metrics.txt"):
        for line in open(path):
            parts = line.split()
            if len(parts) != 2:
                continue
            name, _, val = parts[0], None, None
            try:
                val = float(parts[1])
            except ValueError:
                continue
            vals[name] = vals.get(name, 0.0) + val
    return vals
print("prefix-cache counter deltas per arm (summed over engines):")
for arm in ("off", "affinity", "coop"):
    for rate in os.environ["RATES"].split(","):
        tag = rate.replace(".", "p")
        before, after = totals(f"{arm}.{tag}.before"), totals(f"{arm}.{tag}.after")
        deltas = {k: after.get(k, 0.0) - v for k, v in before.items() if after.get(k, 0.0) >= v}
        if deltas:
            summary = ", ".join(f"{k.split(':')[-1]}+{v:.0f}" for k, v in sorted(deltas.items()))
            print(f"  {arm:>9} {rate:>5}: {summary}")
        else:
            print(f"  {arm:>9} {rate:>5}: (no numeric prefix-cache series scraped)")
PYEOF

echo
echo "== cache game done; router stopped"
echo "   score:  ${BIN}narwhal-report --dir $OUT --ttft-slo $TTFT --tpot-slo $TPOT --phases 1"
echo "   if you waved the engines in with caching on, wave them back when the fleet returns to walk duty"
