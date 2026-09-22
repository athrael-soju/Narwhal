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

The router journal counts identified output tokens, including tokens with empty text and reasoning-only output. Its TPOT requires at least two tokens. The [journal contract](Telemetry-and-Artifacts.md#request-journal) defines the router denominator and terminal outcomes.

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

Run `narwhal-check` against the final fleet before sending deployment traffic.

Before the run, bind one deployment identifier to the exact Narwhal release, source revision, distribution digest, fleet config, profile and sample stores, engine image digest and launcher, attestation documents, router and engine configuration, preflight output, endpoint captures, deployment-client output, router journal, state snapshots and metrics. Record the workstation and router hosts and SSH tunnel mapping under that identifier.

Hold the source revision, model, runtime, profiles, router targets, workload shape, cache policy and latency targets fixed while varying the offered rate. Drain resident requests and transfer leases between rates; continue until the client attainment target fails or the intended operating ceiling passes.

### Run the initial synthetic workload

Through the [private tunnel](Deploy.md#open-the-private-trial-route-from-the-workstation), send 200 requests with 8,192 input tokens and exactly 128 output tokens from the management workstation at 0.5 request/s, then replay them at 1 request/s. A rate meets the candidate 95% attainment target when at least 190 requests complete with TTFT at most 2 seconds and TPOT at most 0.0333 seconds.

Confirm that the workload fits the accepted profile range and engine context limit, retain the launch records containing `--no-enable-prefix-caching`, and reserve the router for trial traffic so its journal can be reconciled with client records.

From the management checkout, install the load helper's dependencies and create a private trial directory:

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

`prepare` reads the served model, requests an unscored 32-token completion from a fixed public seed prompt, and saves its token IDs to `workload/workload.json`. The sampler draws each request's 8,192 input IDs from that pool using the seed plus sequence number; both rates use the same file with temperature zero, `add_special_tokens=false`, `min_tokens=max_tokens=128` and `ignore_eos=true`. Each run manifest records the workload and helper digests, workstation hostname, source revision, command, Python/httpx versions and selected limits.

Run the first offered rate:

```bash
.venv/bin/python tools/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 0.5 --requests 200 --ttft 2 --tpot 0.0333 --attainment 0.95 \
  --out "$TRIAL_DIR/rate-0.5"
```

After the 0.5 request/s run, inspect its saved records before starting the next rate:

| Exit | Recorded condition | Operator action |
| --- | --- | --- |
| `0` | The client met the schedule and candidate attainment target. | Let the router drain, then run the next rate. |
| `2` | The run completed with an attainment or client-schedule miss. | Check `client_schedule_valid` in `summary.json`: retain a scheduled run with latency misses or HTTP refusals as a rate measurement; inspect `requests.jsonl` and correct missed client start times before repeating the run. |
| `1` | The helper reported a blocking error. | Diagnose the printed error and retained artifacts before repeating the run. |
| `130` | An interrupted run retained partial artifacts. | Inspect the partial record before repeating the run. |

After the first trial drains, run the second offered rate:

```bash
.venv/bin/python tools/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 1 --requests 200 --ttft 2 --tpot 0.0333 --attainment 0.95 \
  --out "$TRIAL_DIR/rate-1"
```

Each run waits for empty router admission queues and resident work, checks one unscored warmup at the full workload shape, drains again, then schedules the 200 offers independently of response completion. The default client ceiling is 64 concurrent requests and the scheduling-lag limit is 50 ms. Missed scheduling slots receive terminal client-miss records and invalidate the offered-rate comparison. The client issues one attempt per sent offer and records HTTP refusals, stream errors and timeouts in the denominator. Increasing a client limit requires CPU, memory, network and scheduling evidence identifying the client bottleneck.

`requests.jsonl` retains one terminal row per scheduled offer, including `client_rid`, sequence, scheduled and actual start times, HTTP status, error details, input/output counts, TTFT and TPOT. SSE token IDs count empty-text and reasoning output; one identified token per event, a length finish, `[DONE]`, and matching final usage counts qualify a complete response. TTFT runs from HTTP dispatch to the first identified token; TPOT divides first-to-last token time by completed output tokens minus one. Latency percentiles describe complete responses. Match `client_rid` to the router journal's `client_rid` and reconcile each sent offer to its terminal class.

`summary.json` divides completed requests, output tokens and SLO-qualified requests by elapsed time through final response drain. Compare its client CPU time, peak memory and scheduling lag with workstation interface deltas in `network-before.json` and `network-after.json` and with router and engine metrics before attributing a throughput limit to serving. Read `state-before.json`, `state-after-warmup.json` and `state-after.json` to check admission and resident work around warmup and load.

Run artifacts use a fresh mode-0700 directory and mode-0600 files. The helper runs directly from the management checkout, so its recorded digest identifies a local helper update independently of the installed router revision. Continue with journal reconciliation, dashboard queries and the post-load KV ring in [Deploy step 10](Deploy.md#measure-and-retain-the-trial).

The deployment acceptance record should name the highest offered rate meeting the candidate client target, the tested request shape and SSH route, the preflight revision, and the retained artifact paths.
