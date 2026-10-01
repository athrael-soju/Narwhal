---
description: Compare SLO-qualified throughput between cold and cache-aware prefill pricing on one Narwhal fleet.
---

# Cache-aware placement trial

The trial compares the throughput of requests that meet the service-level objectives (SLOs) between two arms on the same fleet:

| Arm | Prefill pricing |
| --- | --- |
| Baseline | Every engine cold |
| Cache-aware | The warm fit of each engine that holds the request's prefix |

Both arms run with the backend's prefix caching on.

## Freeze the comparison

Record these in the private execution record before the first measured run:

- the benefit and regression thresholds
- the fleet, model, engine image, launch policy, SLOs, and controller settings
- the workload shape, family count, and profile lengths
- the rate pilot rule, the seeds, and the run order

The arms differ only in the cache-event entry of `runtime.extra_args`.

Each run's score is its `qualified_rps_including_drain` and `attainment`.

Refusals and failures count as misses.

## Choose the workload shape

| Symbol | Meaning |
| --- | --- |
| `L` | Input tokens of every request in a workload |
| `B` | Engine cache block size, reported as `block_size` in each engine's `residency` record in `/narwhal/state` |

For a prompt of `L = k*B + s` tokens with `0 < s < B`, an engine with boundary-state groups, such as Mamba state, keeps its boundary state at `k*B`.

For a repeated-prefix workload on such an engine:

- set `--input-tokens` to `k*B + s` with `0 < s < B`
- set `--prefix-tokens` to at least `k*B`
- expect a cached prefix of `k*B` tokens and an uncached suffix of `s` tokens on the engine holding the family

On an engine with full-attention groups only, the cached prefix is the largest multiple of `B` within both the shared prefix and `L - 1` tokens.

An engine prices a cached prefix with its warm fit when both lengths lie inside its measured warm domain in the [profile fields](../telemetry/02-Profiles.md#profile-fields):

| Length | Range |
| --- | --- |
| Cached prefix | `cached_min_prefix_tokens` to `cached_max_prefix_tokens` |
| Uncached suffix | `cached_min_suffix_tokens` to `cached_max_suffix_tokens` |

Profile both arms with these [profiler options](../cli/Profile.md#prefill-and-decode-sweeps):

| Flag | Value |
| --- | --- |
| `--cached-prefix-lens` | Lengths that bracket the trial's cached prefix |
| `--cached-suffix-lens` | Lengths that bracket the trial's uncached suffix |
| `--decode-input-lens` | Lengths up to the trial's input length |

## Prepare each arm

| Arm | `runtime.extra_args` | Residency routes | Router pricing |
| --- | --- | --- | --- |
| Baseline | `["--kv-events-config", "{\"enable_kv_cache_events\": false}"]` added | HTTP 404 from every sidecar | Every engine cold |
| Cache-aware | Launcher default | Served by every sidecar | Each engine holding a prefix at its warm fit |

For each arm:

1. Launch and check every engine as in [Gate C](../deploy/03-Validate-Engines.md#prepare-check-and-start-each-engine).
2. Attest the engines as in [Gate E](../deploy/05-Attest.md).
3. Profile the engines with the lengths from [Choose the workload shape](#choose-the-workload-shape).
4. Calibrate the first-token deadline over every directed engine pair as in [Gate F](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).
5. Run a passing preflight.
6. Start the router with `pin: false` on every engine and the recorded controller settings.

Before each cache-aware run, confirm:

- every engine's `residency` record reports [`"known": true`](../cli/Attest.md#when-residency-is-known)
- every profile carries its `cached_` fields

## Create the workloads

1. Set `NARWHAL_TRIAL_URL` and `TRIAL_DIR` as in [Create the trial directory and workload](03-Load-Trial.md#create-the-trial-directory-and-workload).
2. From the management workstation, prepare a repeated-prefix workload for each pass and one cold control with the same shape.

Repeated-prefix workload:

```bash
.venv/bin/python tools/measurement/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/repeated-1" \
  --input-tokens <input-tokens> --output-tokens <output-tokens> \
  --prefix-tokens <prefix-tokens> --families <families> --seed <seed>
```

Cold control:

```bash
.venv/bin/python tools/measurement/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/control" \
  --input-tokens <input-tokens> --output-tokens <output-tokens> \
  --prefix-tokens <prefix-tokens> --families 0 --seed <seed>
```

Replace each `<...>` value with the recorded choice.

Give the second pass's repeated-prefix workload, `repeated-2`, a distinct `--seed`.

Prompts by workload:

| Workload | Request prompt |
| --- | --- |
| Repeated-prefix | One of the family prefixes followed by a suffix unique to its run and sequence |
| Cold control | A unique prefix for every request |

## Set the offered rates

1. In the baseline arm, run a 60-second point at each pilot rate, each with a fresh repeated-prefix workload.
2. Take `R_b` as the highest pilot rate with attainment at or above the recorded attainment.
3. Set the low rate to `1.1 * R_b` and the high rate to `1.3 * R_b`, each rounded to 0.5 request/s.

## Check cache-aware pricing

1. In the cache-aware arm, run one short, unscored point with a repeated-prefix workload.
2. Join its `requests.jsonl` to the router journal on `client_rid`, as in [Join client offers to the router journal](04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal).
3. Compare `predicted_prefill_s` with `cold_prefill_s` in the [`cache_placement`](../telemetry/01-Journal.md#cache-placement) record of each row with `placed_cached_tokens` above 0.

Next step by comparison result:

| `predicted_prefill_s` against `cold_prefill_s` | Next step |
| --- | --- |
| Below on every row | Start the measured runs |
| At or above on any row | Revisit the workload shape and the warm prefill lengths |

## Run each point

Run these in each arm, in this order:

| Stage | Points in order |
| --- | --- |
| Warm-up | One unscored 120-second run at the low rate |
| Pass 1 | `repeated-1` low, control low, `repeated-1` high, control high |
| Pass 2 | Control high, `repeated-2` high, control low, `repeated-2` low |

Run one point:

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/<arm>-<point>" \
  --workload "$TRIAL_DIR/<workload>/workload.json" \
  --rate <rate> --requests <requests> --run-seed <run-seed> \
  --ttft <ttft-s> --tpot <tpot-s> --attainment <attainment>
```

Point flags:

| Flag | Value |
| --- | --- |
| `--requests` | 60 seconds of offers at the point's rate |
| `--run-seed` | A distinct seed for each point, shared by both arms |

A run that exits with status `0` or `2` writes `summary.json` with:

| Field | Meaning |
| --- | --- |
| `attainment` | Completed requests within the time to first token (TTFT) and time per output token (TPOT) limits, over all offers |
| `qualified_rps_including_drain` | Completed requests within the limits per second of the measurement window |
| `ttft_s`, `tpot_s` | p50, p95, and p99 over completed requests |
| `outcomes` | Offers by client outcome, with router refusals in `http_error` |

`run` exits with status `2` when either holds:

- attainment falls below `--attainment`
- the client misses its schedule (`client_schedule_valid: false`)

Between runs:

1. Drain the router.
2. Reconcile the run with the journal as in [Reconcile and accept](04-Reconcile-and-Accept.md).

## Exercise a role change

Run one separate, unscored cache-aware point after the measured runs.

A reactive prefill-to-decode move requires every [adjacent-split condition](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions).

Run the role-change point:

1. Prepare a decode-heavy workload with a larger `--output-tokens` and the repeated-prefix workload's `--seed`, `--input-tokens`, `--prefix-tokens`, and `--families`.
2. Run it at a rate that raises decode load until `flips` in `/narwhal/state` records the move with `"by": "reactive"`.
3. Confirm that `pools.prefill` lists the remaining prefill engines.
4. Confirm that prefill placements after the change go to the remaining prefill engines.
5. Confirm that journal rows placed on the moved engine before the change complete with that engine as `prefill_iid`.
6. Confirm that the move's `flips` record reports a numeric `drained_s`.
7. Confirm that the moved engine's `residency` record keeps `"known": true` and the same `epoch` across the change.

## Report the result

For each arm and point, report:

- `qualified_rps_including_drain`, `attainment`, and refusals by cause
- the `ttft_s` and `tpot_s` distributions
- controller decisions and `flips`
- the prefill engines' prefix-cache hit rate from engine metrics

In the cache-aware arm, report these per rate over journal rows with a `cache_placement` record and `attempts` equal to 1:

- the estimation error of `cache_placement.predicted_prefill_s` against `upstream_seconds.prefill`
- the share of rows where `cache_placement.cold_choice_iid` differs from `cache_placement.placed_iid`

Overhead measures to report:

| Measure | Source |
| --- | --- |
| Residency resynchronisations | The change in each engine's `residency` `resyncs` between the run's `state-before.json` and `state-after.json` |
| Sidecar and router CPU and memory | Host process accounting |

Compare the arms on the mean of the two passes at each point.

The trial passes when the cache-aware arm meets both thresholds at both rates:

- the benefit threshold on the repeated-prefix workload
- the regression threshold on the control
