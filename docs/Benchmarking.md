# Benchmarking

Measure your own fleet the way the study measured this one. The bench drives load at the router and journals every request, the report tool scores the record for attainment and adaptation cost, and the preflight gates check a run's preconditions. Every number comes from your hardware under your trace, and the measured comparison behind *The Price of Order in Disaggregated Inference* used these same tools, with its methodology published beside it.

[Serving KPIs](KPIs.md) defines every term used here.

## The metric

Attainment is the fraction of *offered* requests that meet both SLOs, TTFT and TPOT, at once. A request the fleet never finished misses its SLO, because counting only completions would let a fleet score better by dropping work. The headline number is the sustained rate: the highest offered rate that still meets the attainment target, 90% by default, with `--target` to change it.

## Calibrate SLOs first

Attainment prices every request against your two SLO targets, so set the targets before any sweep. Run light load, read the p99 TTFT and TPOT from the Prometheus histograms (the standard board plots both quantiles), and set each SLO at about twice its light-load p99. `narwhal-check` confirms the choice: its final `slo` gate prices the targets against the fitted profiles. A TPOT target under the profiled floor is unreachable at any fleet size, and the gate reports that before the run starts. [Deploy](Deploy.md) covers profiling and the gates.

## Drive a rate sweep

```bash
.venv/bin/narwhal-bench --base http://localhost:8000 --model <served-name> \
  --ttft-slo <yours> --tpot-slo <yours> \
  --rates 0.6,1.0,1.6,2.4,3.2 --out runs/local/adaptive.jsonl
```

The built-in generator runs phased load whose rate multipliers are priced from your own fitted curves when `--profiles` finds the store (the default is `runs/local/profiles.json`), so the sweep loads the fleet you have. `--phase-seconds` sets phase length, and `--seed` fixes the arrival process. The run prints attainment per rate and the sustained rate:

```
  rate   attainment     met/total
   0.6       100.0%    180/180
     1        98.3%    295/300
   1.6        71.2%    342/480

sustained at 90% attainment: 1 req/s
```

## Replay a trace

`--trace-file` replays timestamped JSONL (`{at, input_len, output_len}`) and bypasses the generator. `--rates` becomes the timestamp constant: each value scales the recorded timestamps, so the same shape offers a different rate. `--segments dur:isl_lo-isl_hi:osl_lo-osl_hi:mult,...` builds a custom phase ladder for a workload whose optimal split has to move during the run.

## Compare architectures on your fleet

The case for adaptivity is a controlled comparison on your own hardware: arms that share one trace, one rate list and one pair of SLOs, and differ only in whether a role can move. Three configs cover the range the study measured:

| Arm | Config | Roles |
| --- | --- | --- |
| Adaptive | the fleet config, unchanged | the controller moves them |
| Static | the fleet config with `"pin": true` on every engine, `controller` set to `"reactive"`, `thresholds.expand` above any reachable load (`1e9`), `cooldown_s` past the run length | pinned at the launched split |
| Aggregated | the static config, with every engine's `role` set to `decode` | pinned, both phases sharing each instance |

On the static arm the pins do the fixing. Algorithm 1's step-3 flip ignores thresholds only toward decode (toward prefill it consults `thresholds.shrink`), and no flip path moves a pinned engine, so roles stay where the config put them. The unreachable thresholds and the long cooldown cover the case the pin list misses. In the aggregated arm both phases execute on one instance and KV crosses the fabric only on a failover retry. Confirm it from the journal, where `crossed` reads `false` on every row that never retried.

Restart the router on each arm's config and drive it identically. Every arm appends to the same journal, `journal.jsonl` beside `profiles_path` (`runs/journal.jsonl` under the example config), so move the file aside between arms: a later arm scores whatever rows it finds there, the earlier arms' rows included. Score the adaptive arm against every static split within budget, because a single pinned baseline shows the cost of a moving mix for one mix only. `tools/compare.sh` automates the sweep: each arm gets its own router, journal and bench output under `runs/local/comparison`, and `RATES` overrides the rate list.

[evals/topology-walk](https://github.com/athrael-soju/Narwhal/tree/main/evals/topology-walk) packages the comparison as a reproducible eval: five cells - two hot-swap controller variants and three architecture baselines - derived from your fleet config, driven over a moving optimum with one seed per run. `tools/score_walk.py` breaks a cell's client attainment down per phase, and `tools/plot_walk.py` plots the split the fleet kept over time from its router log.

Two sizing rules come from the study's measurement campaign. A discriminating phase has to start above the pinned split's measured knee: pin the fleet in the shape under test, raise the rate in short steps until windowed attainment collapses, and set the phase's rate past that point. Size a phase under the knee and it cannot separate the arms, because the static side meets its SLOs through the whole phase. Paired arms run treatment-first with a rest interval between them, because a freshly worked fleet skews whichever arm runs first. [Evals](Evals.md) covers the rest of the run discipline, resting the fleet included.

## Score the record

```bash
.venv/bin/narwhal-report --dir runs/local/comparison \
  --ttft-slo <yours> --tpot-slo <yours> --profiles runs/profiles.json
```

The directory takes journals named `<arm>.<tag>.journal.jsonl`, the layout `tools/compare.sh` writes. `--profiles` defaults to `runs/local/profiles.json`, so point it at the store the profiler wrote.

Per arm and rate the report prints attainment, the re-role count, thrash per hour (reversals of the same engine), median time-to-adapt, the KV handoff percentiles split crossed against local, and which SLO bound each miss. Two runs can post the same attainment with very different re-role behavior behind it, so read attainment beside the thrash and time-to-adapt columns.

The two tools score different populations, and the difference decides cross-arm rankings. `narwhal-bench` scores from the client side: met over offered, with admission refusals counted as misses, the deployer's number. `narwhal-report` scores the router's journal and excludes refusals from its denominator, reporting them apart. Refusal rates can differ by more than an order of magnitude between arms, because predictive admission turns load away at the door where a pinned split queues and misses, so journal-scored comparisons across arms are not like-for-like. Rank arms by the client score, and quote any journal score with its refusal count beside it. The journal keeps the per-request evidence behind either score: arrival, TTFT, TPOT, both instance ids, whether the KV crossed, and the error if any ([Api](Api.md) lists the fields). Score a journal directly with `narwhal-bench --score-journal <path> --ttft-slo ... --tpot-slo ...`.

## The interactive console

```bash
.venv/bin/narwhal-live-bench --base http://localhost:8000 \
  --model <served-model> --ttft-slo <s> --tpot-slo <s> --heartbeat 5
```

`narwhal-live-bench` drives freestyle load at a running router. Commands are read from stdin as well as the prompt, so a saved file of commands with `wait` lines is a scripted session:

| Command | Effect |
| --- | --- |
| `rate X` | base arrivals per second |
| `shape LO-HI LO-HI` | input and output token bands |
| `mult X` | standing rate multiplier |
| `preset NAME` | a predefined traffic shape |
| `spike Xx Ns` | temporary extra multiplier with automatic revert |
| `prefix on LEN [POOL]` | shared prompt heads over a prefix pool |
| `prefix off` | back to unrelated prompts |
| `status` | rolling window and the router's own view |
| `wait N` | pause, for scripted sessions |
| `quit` | stop and print the session score |

The session ends with a score and a recorded trace whose path it prints. `narwhal-bench --trace-file <path>` replays the recording request for request.

## The 90-second check

`make demo` needs no fleet. The simulator replays a 90-second moving trace across the topology spectrum on CPU and prints the comparison table, adaptive against every static split, in under a minute of wall clock. Run it before spending GPU time. The test suite pins the table at two of its rates.
