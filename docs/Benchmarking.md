# Benchmarking

This page shows how to measure your own fleet: drive load at the
router and journal every request, then score the record with the
shipped tools. Nothing here depends on anyone else's numbers. The measured
comparison ships with *The Price of Order in Disaggregated
Inference*, with the methodology behind it.

[Serving KPIs](KPIs.md) defines every term this page uses.

## The metric

Attainment is the fraction of *offered* requests that meet both SLOs,
TTFT and TPOT, at once. A request the fleet never finished has missed
its SLO, because counting only completions rewards dropping load. The
headline number is the sustained rate: the highest offered rate that
holds attainment at the target (90% by default, `--target` to change
it).

## Calibrate SLOs first

Every number below is priced against your two SLO targets, so set
them before sweeping. Run light load, read the p99 TTFT and TPOT from
the Prometheus histograms (the standard board plots both quantiles),
and set each SLO at about twice its light-load p99. Then run
`narwhal-check`; its final `slo` gate confirms the targets are
reachable against the fitted profiles. A TPOT target under the
profiled floor is unreachable at any fleet size, and the gate reports
this before the run starts. [Deploy](Deploy.md) covers profiling and
the gates.

## Drive a rate sweep

```bash
.venv/bin/narwhal-bench --base http://localhost:8000 --model <served-name> \
  --ttft-slo <yours> --tpot-slo <yours> \
  --rates 0.6,1.0,1.6,2.4,3.2 --out runs/local/adaptive.jsonl
```

The built-in generator runs phased load whose rate multipliers are
priced from your own fitted curves when `--profiles` finds the store
(the default is `runs/local/profiles.json`), so the sweep stresses
the fleet you have. `--phase-seconds` sets phase length, and `--seed`
fixes the arrival process. It prints attainment per rate and the
sustained rate:

```
  rate   attainment     met/total
   0.6       100.0%    180/180
     1        98.3%    295/300
   1.6        71.2%    342/480

sustained at 90% attainment: 1 req/s
```

## Replay a trace

`--trace-file` replays timestamped JSONL (`{at, input_len,
output_len}`) and bypasses the generator. `--rates` then acts as the
timestamp constant, and each value scales the recorded timestamps to
simulate a different offered rate on the same shape. `--segments
dur:isl_lo-isl_hi:osl_lo-osl_hi:mult,...` builds a custom phase
ladder instead, for a workload whose optimal split moves on purpose.

## Compare architectures on your fleet

Prove adaptivity against baselines on your own hardware. Run three
arms with the same trace, rates, and SLOs, so the only difference is
whether roles move:

- **Adaptive** - the router as configured; drive it as above.
- **Static** - copy the config, set `"pin": true` on every engine,
  switch `controller` to `"reactive"`, and set `thresholds.expand` to
  a number no load reaches (such as `1e9`) with `cooldown_s` past the
  run length. The pins matter, because Algorithm 1's inline rescue
  flip ignores thresholds, and a pin stops every flip path. Roles
  stay where the config put them. Restart the router on that config
  and drive it identically.
- **Aggregated** - copy the static arm's config and start every
  engine in the decode pool. Both phases land on one instance, and KV
  crosses only on a failover retry. Confirm from the journal:
  `"crossed"` should read `false` on every row that never retried.

Every arm appends to the same journal (`journal.jsonl` beside
`profiles_path`, so `runs/journal.jsonl` under the example config).
Move the file aside between arms, or the second arm scores the first
one's requests too. Compare adaptive against every static split you can afford to
run rather than one hand-picked baseline. `tools/compare.sh` automates
the whole sweep: each arm gets its own router, journal and bench
output under `runs/local/comparison`, and `RATES` overrides the
sweep.

Two sizing rules, both learned by measurement. Size each
discriminating phase above the pinned split's measured knee: pin the
fleet in the shape under test, raise the rate in short steps until
windowed attainment collapses, and set the phase's rate past that
point, because a wall sized under the knee rewards standing still.
And run paired arms treatment-first with a rest interval between
them, because a freshly worked fleet biases whichever arm runs
first.

## Score the record

```bash
.venv/bin/narwhal-report --dir runs/local/comparison \
  --ttft-slo <yours> --tpot-slo <yours> --profiles runs/profiles.json
```

The directory holds journals named `<arm>.<tag>.journal.jsonl`, the
layout `tools/compare.sh` writes; `--profiles` defaults to
`runs/local/profiles.json`, so point it at the store the profiler
wrote.

Per arm and rate it prints attainment, the re-role count, thrash per
hour (reversals of the same engine), median time-to-adapt, the KV
handoff percentiles split crossed against local, and which SLO bound
each miss. A run that hits its rate by thrashing reads differently
from one that holds still.

The two tools score differently, and the difference matters when
comparing arms. `narwhal-bench` scores from the client side: met over
offered, with admission refusals counted as misses. `narwhal-report`
scores the router's journal and excludes refusals from its
denominator, reporting them apart. Refusal rates can differ by two
orders of magnitude between arms, because predictive admission
refuses at the door where a pinned split queues and misses, so
journal-scored comparisons across arms are not like-for-like. Rank
arms by the client score, and quote a journal score with its refusal
count beside it. The journal carries the
per-request evidence: arrival, TTFT, TPOT, both instance ids, whether
the KV crossed, and the error if any ([Api](Api.md) lists the
fields). Score one directly with `narwhal-bench --score-journal
<path> --ttft-slo ... --tpot-slo ...`.

## The interactive console

```bash
.venv/bin/narwhal-drive --base http://localhost:8000 \
  --model <served-model> --ttft-slo <s> --tpot-slo <s> --heartbeat 5
```

`narwhal-drive` (also installed as `narwhal-live-bench`) drives
freestyle load at a running router. Commands at the prompt are also
accepted on stdin, so a file of commands with `wait` lines is a
scripted session:

| Command | Effect |
| --- | --- |
| `rate X` | base arrivals per second |
| `shape LO-HI LO-HI` | input and output token bands |
| `mult X` | standing rate multiplier |
| `preset NAME` | a predefined traffic shape |
| `spike Xx Ns` | temporary extra multiplier, then auto-revert |
| `prefix on LEN [POOL]` | shared prompt heads over a prefix pool |
| `prefix off` | back to unrelated prompts |
| `status` | rolling window and the router's own view |
| `wait N` | pause (for scripted sessions) |
| `quit` | stop and print the session score |

The session ends with a score and a recorded trace whose path it
prints. `narwhal-bench --trace-file <path>` replays it exactly.

## The 90-second check

`make demo` needs no fleet. The simulator replays a 90-second moving
trace across the topology spectrum on CPU and prints the comparison
table, adaptive against every static split, in under a minute of wall
clock. Run it before spending GPU time. The test suite pins the table
at two of its rates.
