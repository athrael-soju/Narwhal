# Evals

Fleet evals: reproducible runs that answer a question about a fleet and end in a scored verdict. Each one ships the configs it runs, so a result can be reproduced by anyone with the same hardware.

A preflight check asks whether a fleet is wired correctly. [`narwhal-check`](../docs/Deploy.md) does that, and it is the gate before any of this. An eval asks a harder question: whether the fleet holds up under a load shape chosen to find a specific failure, and what it scores when it does.

| eval | question | cost |
| --- | --- | --- |
| [topology-walk](topology-walk/) | What does each architecture score when the optimum keeps moving? | ~13 h 20 m per seed |

Run discipline - resting the fleet before a scored run, and what a failed preflight means - is in [Evals](../docs/Evals.md).

## Running one

Every eval reads a fleet config and writes its artifacts under `runs/`:

```bash
bash evals/topology-walk/run.sh
FLEET=config/fleet.mine.json SEED=11 bash evals/topology-walk/run.sh
```

Common environment for both:

| variable | meaning | default |
| --- | --- | --- |
| `FLEET` | base fleet config to derive cells from | `config/fleet.local.json` |
| `PORT` | router port | `8011` |
| `OUT` | artifact directory | `runs/local/eval-<name>` |
| `SEED` | client seed | `7` |
| `NARWHAL_BIN` | prefix for the CLIs, if not on `PATH` | `.venv/bin/` when present |

The default `FLEET` is operator-local and does not ship, and the run exits 2 when it names no file. On a fresh checkout, copy [`config/fleet.example.json`](../config/fleet.example.json) - or the preset under `presets/` that names your hardware - to a config of your own, fill in the fabric addresses and the model, and point `FLEET` at it.

Each eval **stops any router running on the host** and starts one of its own per cell, so nothing carries between cells. Do not run one against a fleet serving production traffic.

No GPUs handy? `tools/stub_fleet.py` stands up a fake fleet that speaks the same routes, which is enough to exercise the plumbing and read the artifact shapes. Scores from it mean nothing.

## The configs

Each eval directory ships the cell configs it runs, rendered against `presets/mi355x-kimi-k3/`:

```
evals/topology-walk/fleet.planner.json        adaptive, windowed planner
evals/topology-walk/fleet.reactive.json       adaptive, Algorithm 2
evals/topology-walk/fleet.coldswap.json       adaptive, 300 s charged per flip
evals/topology-walk/fleet.static.json         one fixed split, no flip possible
evals/topology-walk/fleet.aggregated.json     no disaggregation
```

These are for reading and for diffing. `run.sh` derives the same shapes from *your* `FLEET` at run time, so an eval works on any fleet without editing a config by hand. Every field in them is documented in [Configuration](../docs/Configuration.md).

Addresses in these files are placeholders (`<node-1-fabric-address>`). They name no host, no path, and no site. Anything added here publishes, so keep it that way.

## Artifacts

Every eval writes the same set per cell, and they are the evidence:

```
<cell>.<tag>.journal.jsonl      router-side, one row per request
<cell>.<tag>.samples.jsonl      client-side, one row per request
<cell>.<tag>.config.json        the exact config that cell ran
<cell>.<tag>.state.before.json  pools, pins, thresholds at launch
<cell>.<tag>.state.after.json   final pools, and the full flip list
<cell>.<tag>.router.log         router stdout and stderr
<cell>.<tag>.bench.log          client attainment summary
check.before.log                preflight evidence, once for the whole run
```

`state.after.json` is the one people forget and later need. Its `flips` list is what lets you reconstruct which split the fleet actually held in each phase, which is a different question from what the phase asked for. A fleet held above the `min_prefill` floor reports intents it never reached, and only the flip list shows it.

Score a run's artifact directory with the budgets the run drove under. `run.sh` reads them out of the fleet config's `slo` block and prints the exact line, artifact directory included, when it finishes:

```bash
narwhal-report --dir "$OUT" --ttft-slo 15.0 --tpot-slo 0.06 --phases 8
```

The 15 s TTFT and 60 ms TPOT are the reference budgets the quoted result was run and scored against. The shipped presets carry 3 s TTFT with the same 60 ms TPOT as a tighter demonstration budget, and a run against them scores with `--ttft-slo 3.0`. The scorer's budgets must equal the run's either way: the admission door priced every refusal against them.

Client-scored attainment counts refusals as misses and is the deployer's number. The journal scorer takes router-side verdicts and holds refusals apart. Refusal rates differ across architectures, so a cross-cell comparison has to name which convention it used. [Benchmarking](../docs/Benchmarking.md) covers the scoring in full.

## Adding one

An eval earns its place when it has a question a preflight cannot answer, a load shape chosen to answer it, and a pass/fail rule fixed before the run. Give it a directory, a `run.sh` that exits non-zero on failure, its cell configs with placeholder addresses, and a `README.md` that states the question, the mechanism it is looking for, and what a failure means for the operator.
