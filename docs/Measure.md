# Measure a fleet

Treat every production result as a property of the exact model, engine build, hardware shape, topology, workload, and latency targets used to produce it.

## Measurement boundaries

The router journal and deployment client measure different intervals.

| Metric | Router journal                                                         | Deployment client                                                                           |
| ------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| TTFT   | Router arrival to prefill completion.                                  | HTTP request start to the first identified output token.                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1`. | First identified output token to last identified output token, divided by `output_len - 1`. |

Router TTFT covers token counting, placement wait, prefill queueing, and prefill execution. `first_byte_s - ttft_s` covers KV transfer and decode queueing before the first visible token.

The deployment client must record scheduled requests, response status, TTFT, TPOT, requested output length, completed output length, and terminal errors. Refused and failed scored requests count as SLO misses. Retain the stream-accounting rule with the results so later runs use the same denominator.

The router journal counts identified output tokens, including empty-text tokens and reasoning-only output. TPOT is defined only when at least two output tokens are present. The [journal contract](Telemetry-and-Artifacts.md#request-journal) defines router-side denominators and terminal outcomes.

## 1. Calibrate SLOs

### Profile an idle fleet

Reserve the engines, warm the model, and disable prefix caching.

Profile the production serving shape across its expected input lengths, decode context lengths, and active sequence counts.

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

Use at least three prefill lengths, including the longest inputs expected in production. The profiler reads each engine's live `max_model_len` from `/tokenize`, keeps candidate lengths that leave room for the output token, and checks the exact tokenised prompt before the completion request. Inspect the effective sweep retained for each engine against its checked serving plan.

For decode, cover both context length and concurrency. The profiler requires at least two input lengths and two concurrency values. Production calibration should use at least three concurrency values, including one stream and the intended operating range. It bounds decode input candidates against the same live context limit plus the requested output tokens. Extend the sweep explicitly for longer-context deployments or select shorter points when the bound leaves too few cells.

Decode probes request exact output token IDs with one token per SSE event. The profiler verifies stream completion, per-event token identity, token cardinality, and interval count. Any failed check aborts the probe.

### Retain the profile data

`narwhal-profile` writes `profiles.json` and `profiles.samples.json`.

`profiles.samples.json` contains software identity, sweep settings, per-engine prefill observations, decode intervals, cell medians, and fitted profiles. If the run stops part-way through, completed engines remain in the output. `--overwrite` creates a new pair containing only the selected engines.

Keep both files with the deployment record.

The profiler reads `kv_cache_size_tokens` from vLLM's `cache_config_info` metric. Builds exposing that field receive a physical KV constraint. Builds without it fall back to the TPOT-derived capacity limit.

### Accept or reject the profile

Inspect prefill coverage and fit error manually. Measured prefill latency includes the HTTP round trip and one generated token.

Resident-token count alone does not determine decode interval. Two cells with the same resident KV total can behave differently when their sequence counts differ.

The profiler samples token intervals from the interior of a complete decoding cohort: after every stream has emitted its first token and before any stream finishes. Active-request count and resident KV tokens are inferred from client observations. Near scheduler saturation, compare those estimates with engine request metrics and inter-token metrics.

Decode fitting uses nonnegative coefficients. Leave-one-cell-out cross-validation uses the same constrained estimator. Each profile records the measured minimum and maximum on both axes; that rectangle defines the decode domain the controller may price.

Repeat the sweep, then measure intermediate cells shaped like the target workload. Leave-one-cell-out error tests interpolation within a run. The repeated sweep measures run-to-run stability and tail behaviour.

The defaults are:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Keep the fit limit at or below `controller.reactive.movement_margin`. Choose both error limits from measurements covering the deployment's expected decode domain.

`narwhal-check` reports the engine, measured value, and configured limit when either error bound is exceeded. It also rejects profiles whose measured domain contains only one point on an axis. Reactive control should run only against the current schema and an accepted decode sweep.

Once the profiles are accepted, run light traffic and set `slo.ttft_s` and `slo.tpot_s` from the service requirement and the measured latency distribution. A TPOT target below the engine shape's measured per-token floor produces zero feasible decode capacity.

Run preflight after either SLO changes:

```bash
narwhal-check --fleet config/fleet.production.json
```

With at least three successful probes, the pace gate compares each engine with the fleet median. Smaller fleets require a saved prefill profile for every engine and an exact `usage.prompt_tokens` count. Both paths use the same 1.5× slowdown limit.

## 2. Validate the deployment under load

Run `narwhal-check` against the final fleet before sending trial traffic.

Assign a deployment identifier before the load test. Bind it to the exact Narwhal release, source revision, distribution digest, fleet configuration, profile files, sample store, engine image digest and launcher, attestation documents, router and engine configuration, preflight output, endpoint captures, deployment-client output, router journal, state snapshots, and metrics.

Record the workstation host, router host, and SSH tunnel mapping under the same identifier.

During the rate sweep, keep these fixed:

- source revision
- model
- runtime
- profiles
- router targets
- workload shape
- cache policy
- TTFT and TPOT targets

Change only the offered request rate. Drain resident work and transfer leases between rates. Stop when client attainment fails or the intended operating ceiling has been tested.

### Synthetic trial

Use the [private tunnel](Deploy.md#open-the-ssh-forwards) from the management workstation.

The initial workload is 200 requests, each with 8,192 input tokens and exactly 128 output tokens. Run it first at 0.5 request/s, then at 1 request/s.

A rate satisfies the candidate 95% target when at least 190 requests complete with:

```text
TTFT <= 2.0 s
TPOT <= 0.0333 s
```

Before starting, confirm that the workload lies inside the accepted profile domain and engine context limit. Keep launch records showing `--no-enable-prefix-caching`. Reserve the router for trial traffic so every client record can be reconciled with the router journal.

Install the load helper dependencies and create a private run directory:

```bash
make setup
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
umask 077
mkdir -p runs

TRIAL_DIR=$(mktemp -d "$PWD/runs/load-trial-XXXXXXXX")

.venv/bin/python tools/measurement/load_trial.py prepare \
  --base "$NARWHAL_TRIAL_URL" \
  --input-tokens 8192 --output-tokens 128 --seed 1729 \
  --out "$TRIAL_DIR/workload"
```

`prepare` queries the served model, requests an unscored 32-token completion from a fixed public seed prompt, and writes those token IDs to `workload/workload.json`.

For request sequence `n`, the sampler draws 8,192 input IDs from that pool using the configured seed plus `n`. Both rate tests reuse the same workload file with:

```text
temperature = 0
add_special_tokens = false
min_tokens = 128
max_tokens = 128
ignore_eos = true
```

Each run manifest records the workload digest, helper digest, workstation hostname, source revision, command, Python version, httpx version, and selected limits.

Run 0.5 request/s:

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" \
  --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 0.5 --requests 200 \
  --ttft 2 --tpot 0.0333 --attainment 0.95 \
  --out "$TRIAL_DIR/rate-0.5"
```

Inspect the saved records before changing the rate.

| Exit  | Recorded condition                                                | Operator action                                                                                                                                                                                                                                                                  |
| ----- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`   | Schedule and candidate attainment target both passed.             | Drain the router, then run the next rate.                                                                                                                                                                                                                                        |
| `2`   | The run finished with an attainment miss or client-schedule miss. | Inspect `client_schedule_valid` in `summary.json`. Keep a correctly scheduled run with latency misses or HTTP refusals as a valid rate measurement. If client start times were missed, inspect `requests.jsonl`, correct the client-side scheduling problem, and repeat the run. |
| `1`   | The helper reported a blocking error.                             | Diagnose the reported error and retained artifacts before retrying.                                                                                                                                                                                                              |
| `130` | The run was interrupted and partial artifacts were retained.      | Inspect the partial record before retrying.                                                                                                                                                                                                                                      |

After the router has drained, run 1 request/s:

```bash
.venv/bin/python tools/measurement/load_trial.py run \
  --base "$NARWHAL_TRIAL_URL" \
  --workload "$TRIAL_DIR/workload/workload.json" \
  --rate 1 --requests 200 \
  --ttft 2 --tpot 0.0333 --attainment 0.95 \
  --out "$TRIAL_DIR/rate-1"
```

Before each measured run, the helper waits for empty router admission queues and no resident work. It sends one unscored warmup at the full workload shape, drains again, then schedules all 200 offers independently of response completion.

The client permits 64 concurrent requests by default. Its scheduling-lag limit is 50 ms. A missed scheduling slot produces a terminal client-miss record and invalidates that offered-rate comparison.

Each sent offer receives one attempt. HTTP refusals, stream errors, and timeouts remain in the denominator. Raise a client-side limit only after CPU, memory, network, and scheduling measurements identify the client as the bottleneck.

### Reconcile client and router records

`requests.jsonl` contains one terminal row for every scheduled offer. Each row records:

- `client_rid`
- request sequence
- scheduled start
- actual start
- HTTP status
- error details
- input token count
- output token count
- TTFT
- TPOT

SSE accounting includes identified token IDs even when their text is empty or they belong only to reasoning output.

A response is complete only when the stream contains one identified token per event, terminates with a length finish and `[DONE]`, and reports matching final usage counts.

Client TTFT runs from HTTP dispatch to the first identified output token. Client TPOT is the elapsed time from the first identified token to the last, divided by completed output tokens minus one. Latency percentiles include complete responses only.

Join client records to the router journal on `client_rid`. Every sent offer should reconcile to one terminal class.

`summary.json` reports completed-request throughput, output-token throughput, and SLO-qualified-request throughput using elapsed time through the final response drain as the denominator.

Before assigning a throughput ceiling to serving, compare the client's CPU time, peak memory, and scheduling lag with workstation interface counters from `network-before.json` and `network-after.json`, plus router and engine metrics.

Use `state-before.json`, `state-after-warmup.json`, and `state-after.json` to verify admission queues and resident work before warmup, after warmup, and after the measured run.

Each run uses a new mode-0700 directory with mode-0600 files. The helper executes directly from the management checkout. Its recorded digest therefore identifies local helper changes separately from the installed router revision.

Continue with journal reconciliation, dashboard queries, and the post-load KV ring described in [Deploy step 10](Deploy.md#run-and-retain-the-trial).

The deployment acceptance record must contain the highest tested offered rate that met the candidate client target, the request shape, SSH route, preflight revision, and retained artifact paths.
