# Cache-aware placement trial

This trial measures whether pricing prefill with prefix-cache evidence serves more requests within the SLOs on the same fleet. It compares a baseline arm, which prices every engine cold, with a cache-aware arm. Both arms keep the backend's prefix caching on. Run it separately from the [cold synthetic trial](03-Load-Trial.md), which requires prefix caching off.

## Freeze the comparison

Before measuring, record these in the private execution record:

- the benefit and regression thresholds that decide the result;
- the fleet, model, engine image, launch policy and SLOs, identical for both arms;
- the repeated-prefix and cold-control workload files;
- the two offered rates;
- the order of runs.

A higher prefix-cache hit rate alone does not pass the trial. Judge each arm by completed SLO-qualified requests per second and attainment over all offers, counting refusals and failures as misses.

Use the smallest qualified topology with two eligible prefill engines and a decode destination.

## Prepare each arm

The two arms differ only in the backend's cache-event setting:

| Arm | `runtime.extra_args` | Router pricing |
| --- | --- | --- |
| Baseline | `["--kv-events-config", "{\"enable_kv_cache_events\": false}"]` added | No sidecar serves residency, so every engine is priced cold |
| Cache-aware | Launcher default | Sidecars serve residency; engines holding a prefix are priced with their warm fit |

For each arm, launch and check every engine as in [Gate C](../deploy/03-Validate-Engines.md). Then attest the engines, profile them and calibrate the first-token deadline as in [Gate F](../deploy/06-Profile-and-Preflight.md). In the cache-aware arm, profiling records a [warm prefill fit](01-Profile.md#warm-prefill-with-a-cached-prefix) for each engine that reuses a cached prefix. Run a passing preflight before trial traffic.

In the cache-aware arm, confirm from `/narwhal/state` that every engine's `residency` record reports `"known": true` before each run.

## Create the workloads

From the management workstation, prepare one repeated-prefix workload and one cold control with the same input and output shape:

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

Replace each `<...>` value with the frozen choice. Each repeated-prefix request starts with one of the family prefixes and ends with a suffix unique to its run and sequence. The control gives every request its own prefix.

## Run each point

Run each workload at both offered rates in each arm. Give each run a distinct `--run-seed`, so later runs cannot reuse the control's cached prompts:

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --out "$TRIAL_DIR/<arm>-<workload>-<rate>" \
  --workload "$TRIAL_DIR/<workload>/workload.json" \
  --rate <rate> --requests <requests> --run-seed <run-seed>
```

Drain the router between runs and reconcile each run with the journal as in [Reconcile and accept](04-Reconcile-and-Accept.md).

## Report the result

For each arm, workload and rate, report:

- completed SLO-qualified requests per second;
- attainment over all offers;
- TTFT and TPOT distributions;
- controller decisions and role changes.

In the cache-aware arm, also report:

- the estimation error, comparing each journal row's `cache_placement.predicted_prefill_s` with its `upstream_seconds.prefill`;
- the share of placements where `cache_placement.cold_choice_iid` differs from `placed_iid`;
- sidecar and router overhead, including residency resynchronisations from `/narwhal/state`.

During one cache-aware run, change one engine's role through the guarded controller path. Confirm that resident requests finish on their original engine and that the residency record keeps following the engine.

The trial passes only when the cache-aware arm meets the predeclared benefit threshold on the repeated-prefix workload at both rates. It must also stay within the regression threshold on the cold control.

## Capability limits

A sidecar can start after vLLM's replay buffer has dropped the engine's early event batches. It then reports residency unknown until the engine restarts; see [residency limits](../cli/Attest.md#residency-limits). With a hybrid attention and Mamba model, only the prefill engine that computed a prompt can reuse its prefix.
