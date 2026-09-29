# Cache-aware placement trial

This trial measures whether pricing prefill with prefix-cache evidence serves more requests within the SLOs on the same fleet. It compares a baseline arm, which prices every engine cold, with a cache-aware arm. Both arms keep the backend's prefix caching on. Run it separately from the [cold synthetic trial](03-Load-Trial.md), which requires prefix caching off.

## Freeze the comparison

Before measuring, record these in the private execution record:

- the benefit and regression thresholds that decide the result;
- the fleet, model, engine image, launch policy and SLOs, shared by both arms, which differ only in the cache-event entry of `runtime.extra_args`;
- the repeated-prefix and cold-control workload files and their shape;
- the two offered rates and the `--run-seed` for each workload and rate;
- the order of runs, identical in both arms.

Judge each arm by completed SLO-qualified requests per second and attainment over all offers, counting refusals and failures as misses.

Use the smallest qualified topology with two eligible prefill engines and a decode destination.

## Choose the workload shape

Every request in a workload has `L` input tokens. The engine caches blocks of `B` tokens, reported as `block_size` in each engine's `residency` record in `/narwhal/state`.

An engine with boundary-state groups, such as Mamba state, keeps that state at the last full block before the prompt's end. A prompt of `L = k*B + s` tokens with `0 < s < B` keeps it at `k*B`. A prompt with `s = 0` keeps zero boundary blocks. For a repeated-prefix workload on such an engine:

- set `--input-tokens` to `k*B + s` with `0 < s < B`;
- set `--prefix-tokens` to at least `k*B`;
- expect a cached prefix of `k*B` tokens and an uncached suffix of `s` tokens on the engine holding the family.

On an engine with full-attention groups only, the cached prefix is the largest multiple of `B` within both the shared prefix and `L - 1` tokens.

Each engine prices a cached prefix with its warm fit inside the measured warm domain, `cached_min_prefix_tokens` to `cached_max_prefix_tokens` and `cached_min_suffix_tokens` to `cached_max_suffix_tokens` in the [profile fields](../telemetry/02-Profiles.md#profile-fields). Outside that domain the engine gets cold pricing. Profile with [`--cached-prefix-lens` and `--cached-suffix-lens`](01-Profile.md#warm-prefill-with-a-cached-prefix) values that bracket the trial's cached prefix and uncached suffix.

## Prepare each arm

The two arms differ only in the backend's cache-event setting:

| Arm | `runtime.extra_args` | Router pricing |
| --- | --- | --- |
| Baseline | `["--kv-events-config", "{\"enable_kv_cache_events\": false}"]` added | Sidecars answer the residency routes with HTTP 404; the router prices every engine cold |
| Cache-aware | Launcher default | Sidecars serve residency; the router prices each engine holding a prefix with its warm fit |

For each arm:

1. Launch and check every engine as in [Gate C](../deploy/03-Validate-Engines.md).
2. Attest the engines, profile them with the warm prefill lengths from [Choose the workload shape](#choose-the-workload-shape), and calibrate the first-token deadline as in [Gate F](../deploy/06-Profile-and-Preflight.md).
3. Run a passing preflight before trial traffic.

In the cache-aware arm, confirm before each run that every engine's `residency` record reports `"known": true` and every profile carries its `cached_` fields.

## Create the workloads

Set `NARWHAL_TRIAL_URL` and `TRIAL_DIR` as in [Create the trial directory and workload](03-Load-Trial.md#create-the-trial-directory-and-workload). From the management workstation, prepare one repeated-prefix workload and one cold control with the same shape:

```bash
.venv/bin/python tools/measurement/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/repeated" \
  --input-tokens <input-tokens> --output-tokens <output-tokens> \
  --prefix-tokens <prefix-tokens> --families <families>

.venv/bin/python tools/measurement/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/control" \
  --input-tokens <input-tokens> --output-tokens <output-tokens> \
  --prefix-tokens <prefix-tokens> --families 0
```

Replace each `<...>` value with the frozen choice. Each repeated-prefix request starts with one of the family prefixes and ends with a suffix unique to its run and sequence. The control gives every request its own prefix. A shared-prefix workload draws its tokens from a pool of at least 16 distinct token IDs in the seed response.

## Check cache-aware pricing

In the cache-aware arm, run one short, unscored point with the repeated-prefix workload. Join its `requests.jsonl` to the router journal on `client_rid`, as in [Join client offers to the router journal](04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal).

Start the measured runs when the rows after each family's first request carry a [`cache_placement`](../telemetry/01-Journal.md#terminal-request-records) record with `predicted_prefill_s` below `cold_prefill_s`. Otherwise revisit the workload shape and the warm prefill lengths.

## Run each point

Run each workload at both offered rates in each arm, with the fleet's `slo.ttft_s` and `slo.tpot_s` and the frozen attainment:

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/<arm>-<workload>-<rate>" \
  --workload "$TRIAL_DIR/<workload>/workload.json" \
  --rate <rate> --requests <requests> --run-seed <run-seed> \
  --ttft <ttft-s> --tpot <tpot-s> --attainment <attainment>
```

Give each workload and rate its own `--run-seed`, and use the same seed for that workload and rate in both arms. Family prefixes stay cached between runs within an arm. Run the points in the frozen order in both arms.

Each run writes `summary.json` with:

| Field | Meaning |
| --- | --- |
| `attainment` | Completed requests within the TTFT and TPOT limits, over all offers |
| `qualified_rps_including_drain` | Completed requests within the limits per second of the measurement window |
| `ttft_s`, `tpot_s` | p50, p95 and p99 over completed requests |

`run` exits with status 2 when attainment falls below `--attainment` or the client misses its schedule (`client_schedule_valid: false`). `summary.json` holds the point's results in both cases.

Drain the router between runs and reconcile each run with the journal as in [Reconcile and accept](04-Reconcile-and-Accept.md).

## Exercise a role change

After the measured runs, run one separate, unscored cache-aware point for the role change.

With two prefill engines and one decode engine, the reactive controller moves a prefill engine to decode when projected prefill load stays at or below `controller.thresholds.shrink` and the move reduces the worst projected SLO ratio by at least `controller.reactive.movement_margin`. See [adjacent-split decisions](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions).

1. Prepare a decode-heavy workload with the repeated-prefix workload's `--seed`, `--input-tokens`, `--prefix-tokens` and `--families` and a larger `--output-tokens`. Its family prefixes match the repeated-prefix workload.
2. Run it at a rate that raises decode load until `flips` in `/narwhal/state` records the move with `"by": "reactive"`.
3. Confirm that `pools.prefill` lists the remaining prefill engine and new prefill placements go to it.
4. Confirm that journal rows placed on the moved engine before the change complete with that engine as `prefill_iid`, and that its `flips` record gains `drained_s`.
5. Confirm that the moved engine's `residency` record keeps `"known": true` and the same `epoch` across the change.

## Report the result

For each arm, workload and rate, report:

- `qualified_rps_including_drain` and `attainment`;
- the `ttft_s` and `tpot_s` distributions;
- controller decisions and `flips`.

In the cache-aware arm, also report these per rate, over journal rows with a `cache_placement` record and `attempts` equal to 1:

- the estimation error, comparing `cache_placement.predicted_prefill_s` with `upstream_seconds.prefill`, the prefill HTTP leg's duration including engine queueing;
- the share of rows where `cache_placement.cold_choice_iid` differs from `cache_placement.placed_iid`.

Also report residency resynchronisations, the change in each engine's `residency` `resyncs` between the run's `state-before.json` and `state-after.json`, and sidecar and router CPU and memory from host process accounting.

The trial passes when the cache-aware arm meets the predeclared benefit threshold on the repeated-prefix workload at both rates and stays within the regression threshold on the cold control.

## Capability limits

A sidecar that starts after vLLM's replay buffer drops the engine's early event batches reports residency unknown until the engine restarts. See [residency limits](../cli/Attest.md#residency-limits). With a hybrid attention and Mamba model, only the prefill engine that computed a prompt can reuse its prefix.
