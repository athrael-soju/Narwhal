# Measure a fleet

Attribute every production claim to one model, engine build, hardware shape, topology, workload and latency-target pair measured together.

## Read the measurement boundaries

The router journal and deployment client use different timing boundaries.

| Metric | Router journal                                                              | Deployment client                                                        |
| ------ | --------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| TTFT   | Router arrival through prefill completion.                                  | HTTP request start through the first identified output token.            |
| TPOT   | Prefill completion through final decode token, divided by `output_len - 1`. | First through last identified output token, divided by `output_len - 1`. |

Router TTFT includes token counting, placement wait, prefill queueing, and prefill execution. `first_byte_s - ttft_s` covers KV transfer and decode queueing before the first visible token.

Configure the deployment client to record scheduled requests, response status, TTFT, TPOT, requested output length, completed output length, and terminal errors. Count refused and failed scored requests as SLO misses. Preserve its stream-accounting rule with the results so another run can reproduce the denominator.

The router journal counts identified output tokens, including tokens with empty text and reasoning-only output. Its TPOT requires at least two tokens. The [journal contract](API-and-Data-Reference.md#request-journal) defines the router denominator and terminal outcomes.

## 1. Calibrate SLOs

### Idle fleet

Reserve the engines, warm the model and disable prefix caching before profiling.

### Profile sweep

Profile the intended serving shape over its input and decode ranges.

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

Use at least three distinct prefill lengths and include the workload's longest inputs.

The decode sweep must cover both context lengths and active sequence counts. The profiler requires at least two input lengths and two concurrency values. For production calibration, use at least three concurrency values, including one stream and the intended operating range. The default decode input lengths end at 8,192 tokens; explicitly extend them for longer-context workloads.

Decode probes request exact output token IDs and one token per SSE event; the profiler checks stream completion, per-event identity, token cardinality and interval count, aborting at the first failed check.

### Profile artifacts

Alongside `profiles.json`, the command writes `profiles.samples.json` with software identity, sweep settings, per-engine prefill observations, decode intervals, cell medians, and fitted profiles. A partial run retains completed engines. `--overwrite` starts a fresh pair containing the selected engines. Keep both files with the deployment record.

The profiler reads `kv_cache_size_tokens` from vLLM's `cache_config_info` metric. Engine builds that supply the field add a physical KV constraint; other builds use the TPOT-derived capacity limit.

### Profile acceptance

Validate prefill coverage and fit error manually. Measured prefill latency includes the HTTP round trip and one generated token.

Equal resident-token totals can run at different token intervals when their sequence counts differ. The profiler samples intervals inside a complete decoding cohort, after every stream has produced its first token and before a stream completes. It infers active requests and resident KV tokens from client observations; compare those estimates with engine request and inter-token metrics near scheduler saturation.

The profiler fits nonnegative coefficients and uses the same constrained estimator for leave-one-cell-out cross-validation. It saves the measured minimum and maximum on each axis, forming the rectangle inside which the controller can price decode work.

Repeat the sweep and measure intermediate workload-shaped cells. Leave-one-cell-out error measures interpolation inside one run; the separate repeat establishes run-to-run stability and tail latency.

`profiles.max_decode_fit_mape` defaults to `0.05`, and `profiles.max_decode_cv_mape` defaults to `0.13`. Set the fit limit at or below `controller.reactive.movement_margin`, then choose both limits from measurements covering the deployment's expected decode domain.

`narwhal-check` names the engine, value and limit when profile error exceeds either bound or the measured domain contains one point on an axis; gate reactive control on the current schema and measured decode evidence from the accepted sweep.

Run light traffic and set `slo.ttft_s` and `slo.tpot_s` from the service requirement and observed distribution. A TPOT target below the measured per-token floor gives the engine shape zero feasible decode capacity. Run preflight after changing either target.

```bash
narwhal-check --fleet config/fleet.production.json
```

The pace gate compares engines with the fleet median when at least three probes succeed. Smaller fleets need a saved prefill profile for every engine and an exact `usage.prompt_tokens` count. The same 1.5-times slowdown limit applies to both comparisons.

## 2. Validate the deployment under load

Run `narwhal-check` against the final fleet, then send the deployment workload through the private SSH route with the recorded model, cache policy and request limits.

The [first-deployment trial](Deploy.md#10-validate-private-route-capacity) uses the management workstation as its client and the existing router SSH destination as its private route. Record both endpoint hosts and the tunnel mapping with the workload, and attribute its throughput and latency to that route.

Before the run, bind one deployment identifier to the exact Narwhal release, source revision, distribution digest, fleet config, profile and sample stores, engine image digest and launcher, attestation documents, router, engine and SSH route configuration, preflight output, endpoint captures, deployment-client output, router journal, state snapshots and metrics.

Hold the source revision, model, runtime, profiles, workload shape, cache policy, and latency targets fixed across the run. Start with two offered rates to establish the scaling direction, then extend the sweep until the client attainment target fails or the intended operating ceiling passes. Let resident requests and transfer leases drain between runs.

For each rate, retain the offered count, terminal count, response classes, TTFT and TPOT distribution, output-length validity, refusal causes and client errors under that deployment identifier.

### Run the initial synthetic workload

Use the management workstation and private tunnel from [Deploy step 10](Deploy.md#open-the-private-trial-route-from-the-workstation). The initial trial uses 8,192 input tokens, exactly 128 output tokens, and 200 requests at each of 0.5 and 1 request/s. Score a request against both candidate limits, TTFT at most 2 seconds and TPOT at most 0.0333 seconds, and require 190 of the 200 offers to pass for 95% observed attainment. These two rates establish the initial scaling direction; higher capacity requires further measured rates. This synthetic token-length workload measures serving performance for that shape through the private route.

Confirm that 8,192-token inputs and 128-token outputs fit the accepted profile range and engine context limit, and retain the existing launch records that establish prefix caching is disabled for this trial. Keep the fleet, profiles, router targets and cache policy fixed across both rates. Reserve the router for trial traffic so its journal and state can be reconciled with the client records.

In the management checkout, install the Python client dependencies and create a fresh private trial directory. `make setup` prepares the workstation's Python environment for the load helper. The serving processes continue on their assigned remote hosts.

```bash
make setup
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
umask 077
mkdir -p runs
TRIAL_DIR=$(mktemp -d "$PWD/runs/load-trial-XXXXXXXX")
.venv/bin/python tools/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" \
  --input-tokens 8192 --output-tokens 128 --seed 1729 \
  --out "$TRIAL_DIR/workload"
```

`prepare` reads the router's served model and requests one unscored 32-token completion from a fixed public seed prompt. It saves the returned token IDs as a model-valid token pool in `workload/workload.json`. For each scored request, a deterministic sampler draws 8,192 IDs from that pool using seed plus sequence number. Both rates replay the same 200 input sequences with temperature zero, `add_special_tokens=false`, `min_tokens=max_tokens=128` and `ignore_eos=true`. Preserve this workload file across the two runs. Its digest, the helper digest, workstation hostname, source revision, command, Python/httpx versions and selected limits enter each run manifest.

Run the first offered rate:

```bash
.venv/bin/python tools/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 0.5 --requests 200 --ttft 2 --tpot 0.0333 --attainment 0.95 \
  --out "$TRIAL_DIR/rate-0.5"
```

Inspect `rate-0.5/summary.json` and `requests.jsonl` before continuing. Exit code `0` means the candidate threshold passed with a valid client schedule; `2` retains a completed trial that missed the threshold or client schedule; `1` identifies a setup or drain failure; `130` retains an interrupted run's partial artifacts. Diagnose setup, stream-accounting or client-scheduling failures from the retained record before another rate. A valid run with latency misses or HTTP refusals still supplies the first rate measurement.

After the first trial drains, run the second offered rate:

```bash
.venv/bin/python tools/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 1 --requests 200 --ttft 2 --tpot 0.0333 --attainment 0.95 \
  --out "$TRIAL_DIR/rate-1"
```

Each run waits for empty router admission queues and resident work, checks one unscored warmup at the full workload shape, drains again, then schedules the 200 offers independently of response completion. The default client ceiling is 64 concurrent requests and the scheduling-lag limit is 50 ms. Missed scheduling slots receive terminal client-miss records and invalidate the offered-rate comparison. The client issues one attempt per sent offer and records HTTP refusals, stream errors and timeouts in the denominator. Increasing a client limit requires CPU, memory, network and scheduling evidence identifying the client bottleneck.

`requests.jsonl` retains one terminal row per scheduled offer, including `client_rid`, sequence, scheduled and actual start times, HTTP status, error details, input/output counts, TTFT and TPOT. SSE token IDs count empty-text and reasoning output; one identified token per event, a length finish, `[DONE]`, and matching final usage counts qualify a complete response. TTFT runs from HTTP dispatch to the first identified token; TPOT divides first-to-last token time by completed output tokens minus one. Latency percentiles describe complete responses; attainment divides responses meeting both limits by all 200 scheduled offers. Match `client_rid` to the router journal's `client_rid` and reconcile each sent offer to its terminal class.

`summary.json` reports completion throughput, output-token throughput and SLO-qualified throughput over the offer window plus final response/drain time. It also records client CPU time, peak resident memory and scheduling lag; `network-before.json` and `network-after.json` retain workstation interface counters covering concurrent host traffic. The state snapshots retain admission and resident-work checks before warmup, after warmup and after load. Use those artifacts with router/engine metrics to distinguish client, SSH-route and serving limits. Retain each rate as a separate result and label the 95% fraction as observed attainment for this trial.

Run artifacts use a fresh mode-0700 directory and mode-0600 files. The helper runs directly from the management checkout, so its recorded digest identifies a local helper update independently of the installed router revision. Continue with journal reconciliation, dashboard queries and the post-load KV ring in [Deploy step 10](Deploy.md#measure-and-retain-the-trial).

The deployment acceptance record should name the highest offered rate meeting the candidate client target, the tested request shape and SSH route, the preflight revision, and the retained artifact paths. The deployment system owns that policy and decides when a configuration change requires another run.

The [capacity gate](Deploy.md#10-validate-private-route-capacity) completes setup with a measured workload, reconciled journal, working dashboard and passing post-load KV ring. The [API and data reference](API-and-Data-Reference.md) defines the retained journal, state and metrics.
