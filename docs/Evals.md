# Evals

An eval is a reproducible run that answers a stated question about a fleet and ends in a scored verdict. The README beside its runner in [evals/](https://github.com/athrael-soju/Narwhal/tree/main/evals) states the question, the cells, the cost, and how to read the result.

## Before a scored run

A freshly worked fleet biases what comes next. Caches stay warm, queues take time to drain, and crossed KV transfers can stall on residue from the earlier load. After any load (a plumbing smoke, a benchmark, production traffic), leave the fleet idle for 15 minutes before preflighting a run whose numbers you intend to keep. [Benchmarking](Benchmarking.md) applies the same rule to paired arms, and a multi-hour eval needs it more.

Prove the plumbing at short length before paying for a full one. Every eval defaults to full length, and most take hours. One short pass exercises the whole wiring. With `PHASE_SECONDS=60` the topology walk covers all five cells in about 40 minutes. Scores from that pass mean nothing. Rest the fleet again, then run for real.

An eval stops any router already on its host and starts one per cell. Do not aim an eval at a fleet serving traffic you care about.

## When preflight fails

Every live eval but the cache game preflights with `narwhal-check`. A red gate stops the run before scoring, and the eval prints the path of the log it wrote. The log names the gate, the leg, and the engine.

Minutes after a fleet has worked, a crossed transfer can still be stalled on the previous run's residue, and it clears on its own. Treat a single-leg failure in that window as transient, and re-check once with the fleet idle:

```bash
.venv/bin/narwhal-check --fleet <your-config> --ring --repeats 3
```

When the failure repeats on an idle fleet, treat it as a fault. [Deploy](Deploy.md) documents the gates and what each one isolates. A consume-leg failure at the ring stage isolates the KV path between two specific engines, and Deploy's interface checks are the next stop.

## After a run

A run writes one artifact set per cell under `runs/`, and [evals/README.md](https://github.com/athrael-soju/Narwhal/blob/main/evals/README.md) documents the layout. The config files contain the fleet's real addresses, so scrub them before sharing a result; the state files are `/arrow/state` captures - iids, pools, thresholds, SLOs, counters, flips - and hold none. The journals record lengths, timings, and engine ids, but a failure row can embed the full engine URL, so grep before sharing. `check.before.log` shares that property (its failure lines interpolate the engine URLs too).

Replicates change the seed and nothing else. Rest the fleet between replicates the same way you do before a scored run, and quote single-seed results as `n=1`.
