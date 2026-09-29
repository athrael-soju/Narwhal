# Cache-aware placement trial

The trial compares SLO-qualified throughput between two arms on the same fleet. The baseline arm prices every engine cold. The cache-aware arm prices an engine that holds a request's prefix with that engine's warm fit. Both arms run with the backend's prefix caching on. For measurements with prefix caching off, use the [cold synthetic trial](03-Load-Trial.md).

## Freeze the comparison

Record these in the private execution record before the first measured run:

- the benefit and regression thresholds;
- the fleet, model, engine image, launch policy, SLOs and controller settings;
- the workload shape, family count and profile lengths;
- the rate pilot rule, the seeds and the run order.

Both arms share every recorded setting. The arms differ in the cache-event entry of `runtime.extra_args`.

Each run's score is its `qualified_rps_including_drain` and `attainment`. Refusals and failures count as misses.

## Choose the workload shape

Every request in a workload has `L` input tokens. The engine caches blocks of `B` tokens, reported as `block_size` in each engine's `residency` record in `/narwhal/state`.

An engine with boundary-state groups, such as Mamba state, keeps that state at the last full block before the prompt's end. A prompt of `L = k*B + s` tokens with `0 < s < B` keeps it at `k*B`. A prompt with `s = 0` keeps zero boundary blocks. For a repeated-prefix workload on such an engine:

- set `--input-tokens` to `k*B + s` with `0 < s < B`;
- set `--prefix-tokens` to at least `k*B`;
- expect a cached prefix of `k*B` tokens and an uncached suffix of `s` tokens on the engine holding the family.

On an engine with full-attention groups only, the cached prefix is the largest multiple of `B` within both the shared prefix and `L - 1` tokens.

Each engine prices a cached prefix with its warm fit inside the measured warm domain: `cached_min_prefix_tokens` to `cached_max_prefix_tokens` and `cached_min_suffix_tokens` to `cached_max_suffix_tokens` in the [profile fields](../telemetry/02-Profiles.md#profile-fields). Outside that domain the engine prices cold.

Profile both arms with these [profiler options](../cli/Profile.md):

| Option | Values |
| --- | --- |
| `--cached-prefix-lens` | Lengths that bracket the trial's cached prefix |
| `--cached-suffix-lens` | Lengths that bracket the trial's uncached suffix |
| `--decode-input-lens` | Lengths up to the trial's input length |

## Prepare each arm

| Arm | `runtime.extra_args` | Router pricing |
| --- | --- | --- |
| Baseline | `["--kv-events-config", "{\"enable_kv_cache_events\": false}"]` added | Sidecars answer the residency routes with HTTP 404; the router prices every engine cold |
| Cache-aware | Launcher default | Sidecars serve residency; the router prices each engine holding a prefix with its warm fit |

For each arm:

1. Launch and check every engine as in [Gate C](../deploy/03-Validate-Engines.md).
2. Attest the engines and profile them with the lengths from [Choose the workload shape](#choose-the-workload-shape).
3. Calibrate the first-token deadline over every directed engine pair as in [Gate F](../deploy/06-Profile-and-Preflight.md).
4. Run a passing preflight.
5. Start the router with `pin: false` on every engine and the recorded controller settings.

In the cache-aware arm, confirm before each run that every engine's `residency` record reports `"known": true` and every profile carries its `cached_` fields.

## Create the workloads

Set `NARWHAL_TRIAL_URL` and `TRIAL_DIR` as in [Create the trial directory and workload](03-Load-Trial.md#create-the-trial-directory-and-workload). From the management workstation, prepare a repeated-prefix workload for each pass and one cold control with the same shape.

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

Replace each `<...>` value with the recorded choice. Give the second pass's repeated-prefix workload, `repeated-2`, a distinct `--seed`. Each repeated-prefix request starts with one of the family prefixes and ends with a suffix unique to its run and sequence. The control gives every request a unique prefix.

## Set the offered rates

1. In the baseline arm, run a 60-second point at each pilot rate, each with a fresh repeated-prefix workload.
2. Take `R_b` as the highest pilot rate with attainment at or above the recorded attainment.
3. Set the low rate to `1.1 * R_b` and the high rate to `1.3 * R_b`, each rounded to 0.5 req/s.

## Check cache-aware pricing

In the cache-aware arm, run one short, unscored point with a repeated-prefix workload. Join its `requests.jsonl` to the router journal on `client_rid`, as in [Join client offers to the router journal](04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal).

Start the measured runs when the rows after each family's first request carry a [`cache_placement`](../telemetry/01-Journal.md#terminal-request-records) record with `predicted_prefill_s` below `cold_prefill_s`. Otherwise revisit the workload shape and the warm prefill lengths.

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

Set `--requests` to 60 seconds of offers at the point's rate. Give each point a distinct `--run-seed`, and use the same seed for that point in both arms.

Each run writes `summary.json` at either exit status, with:

| Field | Meaning |
| --- | --- |
| `attainment` | Completed requests within the TTFT and TPOT limits, over all offers |
| `qualified_rps_including_drain` | Completed requests within the limits per second of the measurement window |
| `ttft_s`, `tpot_s` | p50, p95 and p99 over completed requests |
| `outcomes` | Offers by outcome, including refusals |

`run` exits with status 2 when attainment falls below `--attainment` or the client misses its schedule (`client_schedule_valid: false`).

Drain the router between runs and reconcile each run with the journal as in [Reconcile and accept](04-Reconcile-and-Accept.md).

## Exercise a role change

Run one separate, unscored cache-aware point after the measured runs.

The reactive controller moves a prefill engine to decode when both [adjacent-split conditions](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions) hold:

- projected prefill load stays at or below `controller.thresholds.shrink`;
- the move reduces the worst projected SLO ratio by at least `controller.reactive.movement_margin`.

1. Prepare a decode-heavy workload with the repeated-prefix workload's `--seed`, `--input-tokens`, `--prefix-tokens` and `--families` and a larger `--output-tokens`.
2. Run it at a rate that raises decode load until `flips` in `/narwhal/state` records the move with `"by": "reactive"`.
3. Confirm that `pools.prefill` lists the remaining prefill engines and new prefill placements go to them.
4. Confirm that journal rows placed on the moved engine before the change complete with that engine as `prefill_iid`, and that its `flips` record gains `drained_s`.
5. Confirm that the moved engine's `residency` record keeps `"known": true` and the same `epoch` across the change.

## Report the result

For each arm and point, report:

- `qualified_rps_including_drain`, `attainment` and refusals by cause;
- the `ttft_s` and `tpot_s` distributions;
- controller decisions and `flips`;
- the prefill engines' prefix-cache hit rate from engine metrics.

In the cache-aware arm, also report these per rate, over journal rows with a `cache_placement` record and `attempts` equal to 1:

- the estimation error, comparing `cache_placement.predicted_prefill_s` with `upstream_seconds.prefill`, the prefill HTTP leg's duration including engine queueing;
- the share of rows where `cache_placement.cold_choice_iid` differs from `cache_placement.placed_iid`.

Report residency resynchronisations as the change in each engine's `residency` `resyncs` between the run's `state-before.json` and `state-after.json`. Report sidecar and router CPU and memory from host process accounting.

Compare the arms on the mean of the two passes at each point. The trial passes when the cache-aware arm meets both thresholds at both rates:

- the benefit threshold on the repeated-prefix workload;
- the regression threshold on the control.

## Capability limits

A sidecar that starts after vLLM's replay buffer drops the engine's early event batches reports residency unknown until the engine restarts ([residency limits](../cli/Attest.md#residency-limits)). With a hybrid attention and Mamba model, the prefill engine that computed a prompt is the one engine that can reuse its prefix.
