# Topology-walk eval

**Question.** What does each fleet architecture score when the optimum keeps moving?

**Verdict.** Attainment per cell, scored two ways.

**Cost.** ~13 h 20 m per seed, five cells.

```bash
bash evals/topology-walk/run.sh
SEED=11 bash evals/topology-walk/run.sh
CELLS="planner static" PHASE_SECONDS=60 bash evals/topology-walk/run.sh
```

`FLEET` defaults to `config/fleet.local.json`, which is operator-local and does not ship; the run exits 2 without one. Copy `config/fleet.example.json` - or the preset under `presets/` that names your hardware - fill in the fabric addresses and the model, and pass `FLEET=config/fleet.mine.json`. The rest default to all five cells, 1,200 s a phase, seed 7, port 8011, artifacts under `runs/local/eval-topology-walk`.

This eval drives the fleet to a single prefill engine at its lean end, where one engine carries the fleet's whole KV egress. A fabric that cannot hold that corner produces a result about the fabric rather than the controller, so verify `narwhal-check --ring` is green first and read phase 5 with that in mind.

## The game-specific companions

The walk prices architectures while the optimum moves. The three companion scripts isolate the individual games that make that moving optimum expensive. They share the walk's configs, budgets, seed discipline, and artifact layout.

### Game 1: allocation grid

**Question.** Holding one workload fixed, where does fleet attainment peak across the five split choices?

**Why it exists.** The walk never sits still long enough to read the allocation game's payoff surface. [`game1-allocation-grid.sh`](game1-allocation-grid.sh) pins one static fleet per split - 1P5D through 5P1D at the walk's mid-ladder load by default - so split is the only moving variable. A result supports the game model when attainment peaks at the split the live walk selected and adjacent marginals degrade away from it; a flat or displaced surface means the topology walk's allocation story did not survive the fixed-workload test.

```bash
bash evals/topology-walk/game1-allocation-grid.sh
SPLITS="3 4" PHASE_SECONDS=600 bash evals/topology-walk/game1-allocation-grid.sh
```

The default costs five 1,200 s cells plus router turnarounds and writes `runs/local/eval-allocation-grid`. The script prints client attainment, journal attainment, and refusals per split; the scored artifacts sit beside the exact pinned `fleet.grid-*.json` each split ran.

### Game 2: cache game

**Question.** If engines cache prefixes again, how much of the routing cost do Narwhal's two prefix postures recover?

**Why it exists.** Narwhal's engines are stateless in the standing deployment, which deletes the cache-placement game. [`game2-cache-game.sh`](game2-cache-game.sh) reintroduces it under controlled cache state. It runs three router arms against long shared-prefix traffic with unique tails: `off` ignores warmth, `affinity` returns shared prefixes to the engine that warmed them, and `coop` prices fading warmth inside the routing cost.

Each arm generates its own trace and its own prefix-id range, so cache contents cannot carry from one arm into the next. The trailing table compares first-open and repeat-open TTFT inside every arm, reports attainment, and dumps engine prefix-cache counter deltas. Treat an arm with little warm gain but unchanged attainment as router overhead that did not pay; treat a warm-gain lead with poor attainment as cache loyalty bought at admission cost.

```bash
bash evals/topology-walk/game2-cache-game.sh
PREFIXES=48 DURATION=1200 RATES=1.0,2.0 \
  bash evals/topology-walk/game2-cache-game.sh
```

The default costs 45 minutes of load plus router turnarounds and writes `runs/local/eval-cache-game`. The engines must already run with prefix caching on; the script verifies that from engine metrics and exits 2 otherwise. Engines restart only in whole waves, so the operator owns the wave into caching mode and the wave back to the fleet's standing mode.

### Game 3: hindsight replay

**Question.** On routing decisions already recorded by a walk, how far was the actual assignment from a windowed hindsight optimum?

**Why it exists.** The routing game's denominator is not a live policy; it is the value a scheduler with the full window in advance could have reached. [`game3-hindsight-replay.sh`](game3-hindsight-replay.sh) replays walk journals against the fitted engine profile store, compares the journal's actual prefill assignment with an exact min-cost matching per arrival window, and writes both Markdown and JSON reports.

```bash
IN=runs/canon/topology-walk PROFILES=runs/local/profiles.json \
  bash evals/topology-walk/game3-hindsight-replay.sh
```

The replay is CPU-only and does not touch the fleet. It writes `runs/local/eval-hindsight-replay/game3-poa-replay.{md,json}` by default. Its ratio-of-sums is the game-theoretic quantity; per-window medians and p90s show spread. The estimator prices the prefill leg only, and its per-window OPT is a perfect matching, so ratios below 1.0 on an asymmetric fleet mean the matching constraint bound the estimate rather than that anarchy went negative.

## The ladder

Eight phases, one flip apart, out and back:

```
4P2D -> 5P1D -> 4P2D -> 3P3D -> 2P4D -> 1P5D -> 2P4D -> 3P3D
```

Each phase names an intent, not a setting. Nothing tells the controller where the optimum went. The load shape moves - output length and arrival rate together decide which split is optimal, with the input band held constant - and the controller has to find it:

| phase | intent | input | output | rate |
| --- | --- | --- | --- | --- |
| 0 | 4P2D | 12–16k | 20–40 | 3.0 |
| 1 | 5P1D | 12–16k | 1–4 | 3.8 |
| 2 | 4P2D | 12–16k | 20–40 | 3.0 |
| 3 | 3P3D | 12–16k | 60–100 | 2.0 |
| 4 | 2P4D | 12–16k | 120–180 | 1.2 |
| 5 | 1P5D | 12–16k | 350–450 | 0.6 |
| 6 | 2P4D | 12–16k | 120–180 | 1.2 |
| 7 | 3P3D | 12–16k | 60–100 | 2.0 |

Out and back matters. A controller that adapts well going one direction can hunt or lag coming back, and the second half is where that shows.

## The cells

One variable moves per cell - the architecture or the controller,
never both. Same fleet, same ladder, same seed. `planner` and
`reactive` are the same router and the same hot swap; they differ only
in the policy that decides when a label moves, so the pair measures a
controller trade inside Narwhal's adaptive architecture. The
architecture claim rests on adaptive - either controller - against
`coldswap`, `static`, and `aggregated`.

| cell | what it is |
| --- | --- |
| `planner` | adaptive hot-swap, windowed target-state planner (the default) |
| `reactive` | adaptive hot-swap, Algorithm 2 thresholds |
| `coldswap` | the planner charged 300 s out of service per flip |
| `static` | one fixed split, every engine pinned, no flip can fire |
| `aggregated` | no disaggregation; every engine both-phases its own requests |

Three of them need a word.

**`coldswap`** emulates drain-and-reprovision by charging `flip_offline_s: 300` per flip. Engines never actually restart, and 300 s is under half a real reprovision on most hardware, so its penalty is a floor rather than an estimate.

**`static`** pins every engine *and* sets unreachable thresholds, and the pins are the load-bearing half: no flip path moves a pinned engine. Thresholds alone would leak. Algorithm 2 obeys them, and the 31-year cooldown bars Algorithm 1's step-3 flip toward decode, but that flip fires inside placement when nothing meets the SLO, and toward prefill it reads only the shrink guard, which a quiet decode pool passes whatever `expand` says. The controller must be reactive for the mirror-image reason: the planner prices demand and never reads thresholds at all, so with anything unpinned it would re-split on its own schedule.

**`aggregated`** is `static` with every engine set to `decode`, so each serves both phases locally and no KV crosses the fabric.

## Check the floor before reading the result

A fleet with `min_prefill` above 1 **cannot reach phase 5**. The phase still runs, still scores, and still reports - as 2P4D. The intent is in the ladder; what the fleet held is in the artifacts.

Phases 4, 5 and 6 ask for 2P → 1P → 2P. Under a floor of 2 they collapse into one flat stretch, and three of the eight stations stop being independent measurements. Check before you quote anything:

```bash
python3 - <<'EOF'
import json, collections
INTENT = [4, 5, 4, 3, 2, 1, 2, 3]
cell = "runs/local/eval-topology-walk/planner.seed7"
a = json.load(open(f"{cell}.state.after.json"))
b = json.load(open(f"{cell}.state.before.json"))
t0 = next(json.loads(l)["arrived"] for l in open(f"{cell}.journal.jsonl") if '"arrived"' in l)
roles = {i: "prefill" for i in b["pools"]["prefill"]}
roles.update({i: "decode" for i in b["pools"]["decode"]})
ev = [(t0, sum(r == "prefill" for r in roles.values()))]
for f in a["flips"]:
    roles[f["iid"]] = f["to"]
    ev.append((f["at"], sum(r == "prefill" for r in roles.values())))
ev.append((t0 + 1200 * len(INTENT), ev[-1][1]))
print(f"min_prefill={a['min_prefill']}  pinned={a['pinned']}")
for k, want in enumerate(INTENT):
    s, e = t0 + 1200 * k, t0 + 1200 * (k + 1)
    seg = collections.Counter()
    for (ta, va), (tb, _) in zip(ev, ev[1:]):
        lo, hi = max(ta, s), min(tb, e)
        if hi > lo:
            seg[va] += hi - lo
    tot = sum(seg.values()) or 1
    held = " ".join(f"{v}P:{w / tot * 100:.0f}%" for v, w in sorted(seg.items()))
    print(f"  phase {k}  asked {want}P   held {held}")
EOF
```

If `min_prefill` is above 1, say so beside any number you quote from this eval. The lean stations are the ones that separate the architectures, and a floored fleet did not measure them.

## Seeds

`SEED` is a parameter, and one run uses one seed. It sets the arrival realization and the per-request length draws; the fleet config, the ladder, and the build are identical across seeds.

Two full seeds of the reference run have completed, 7 and 11, and the example result below quotes them as one basis: attainment pooled over both runs' requests, everything else averaged. Run more with `SEED=13` and so on to widen the basis; a single-seed result should say `n=1` rather than carry a spread it does not have.

Expect the controller's decisions to reproduce more tightly than its timing: flip counts and directions should match across seeds; which engine moved, and when, need not.

## Scoring

The budgets a walk runs under come from the fleet config's `slo` block: `run.sh` carries them into every derived cell and hands them to the bench client, so the admission door and the scorer see the same numbers. The result below was run and scored against the reference budgets, 15 s TTFT and 60 ms TPOT:

```bash
narwhal-report --dir runs/local/eval-topology-walk --ttft-slo 15.0 --tpot-slo 0.06 --phases 8
```

The shipped cell configs carry 3 s TTFT with the same 60 ms TPOT as a tighter demonstration budget; a run against them scores with `--ttft-slo 3.0`. The scorer's budgets must equal the run's either way - the door priced every refusal against them.

Report both conventions. Client-scored attainment is met-over-offered with refusals counted as misses, and it is the deployer's number. The journal scorer takes router-side verdicts and holds refusals apart from the denominator.

The two can rank architectures differently, and that is not a bug. An architecture that refuses cleanly under pressure looks better to the journal scorer than to the client. Refusal rates differ enough across cells that a cross-cell quote has to name its convention.

Two tools break the result down further. `tools/score_walk.py` scores the client convention per phase - offered, attainment, miss kinds, TTFT percentiles - by regenerating the trace from the seed and segment spec. `tools/plot_walk.py` draws the split a cell actually held over time from its router log. Both read this eval's artifacts as written.

## An example result

Two full runs on the reference fleet (six nodes, Kimi-K3), seeds 7
and 11, scored client-side against the reference budgets of 15 s TTFT
and 60 ms TPOT. Attainment pools both runs' requests and offered sums
them. The two controllers miss in different phases:

| phase | intent | offered | planner | reactive |
| --- | --- | --- | --- | --- |
| 0 | 4P2D | 7,206 | 99.5% | 99.9% |
| 1 | 5P1D (flood) | 9,082 | 85.8% | 100.0% |
| 2 | 4P2D | 7,337 | 95.8% | 98.7% |
| 3 | 3P3D | 4,851 | 95.1% | 96.9% |
| 4 | 2P4D | 2,918 | 99.0% | 95.9% |
| 5 | 1P5D (lean) | 1,493 | 100.0% | 95.4% |
| 6 | 2P4D | 2,839 | 100.0% | 98.2% |
| 7 | 3P3D | 4,935 | 100.0% | 97.8% |
| **overall** | | **40,661** | **95.3%** | **98.5%** |

The planner hunted through the flood, roughly 40 moves per run in
phase 1, and never settled at 5P1D (one run held it for a single 82 s
cadence and reverted); the flood offers the most requests of any
phase, so it dominates the weighted total. Reactive held still through
the same phase and served all of it, then lagged at the lean end:
614 s on average to reach 1P5D against the planner's 202 s, and
95.4% there against 100%. Overall attainment weights phases by offered
volume, so the headline under-weights the lean stations, and those are
the stations that separate the architectures.

All five cells from the same two runs, overall only, scored both ways
(refused is the pooled count over both runs):

| cell | client | journal | refused |
| --- | --- | --- | --- |
| planner | 95.3% | 75.7% | 356 |
| reactive | 98.5% | 79.0% | 0 |
| coldswap | 54.7% | 67.6% | 12,760 |
| static | 93.4% | 82.9% | 2,080 |
| aggregated | 32.5% | 16.8% | 1,425 |

The actuation ablation is the architecture evidence: the same planner
charged 300 s out of service per flip kept 54.7% of the load, and no
disaggregation kept 32.5%. The conventions rank the cells
differently, reactive first on the client's count and static first on
the journal's on both runs, and the refused column is most of the
explanation. Static held one split whose knee the ladder's rates
approached, so predictive admission refused 2,080 requests at the door
across the two runs that the adaptive cells served.

These two runs are the evidence *The Price of Order in Disaggregated
Inference* reports; where they diverge the paper prints the mean with
the per-run spread beside it.

## The configs

`fleet.*.json` here are the five shapes rendered against `presets/mi355x-kimi-k3/`, for reading and diffing. `run.sh` derives the same shapes from your own `FLEET` at run time and writes them into the artifact directory, so what each cell actually ran is recorded beside its journal.
