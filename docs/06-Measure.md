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

The router journal counts identified output tokens, including tokens with empty text and reasoning-only output. Its TPOT requires at least two tokens. The [journal contract](09-API-and-Data-Reference.md#request-journal) defines the router denominator and terminal outcomes.

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

Run `narwhal-check` against the final fleet, then send the deployment workload through the same ingress, authentication, model route, cache policy, and request limits used by clients.

Before the run, bind one deployment identifier to the exact Narwhal release, source revision, distribution digest, fleet config, profile and sample stores, engine image digest and launcher, attestation documents, router, engine, ingress and supervisor configuration, preflight output, endpoint captures, deployment-client output, router journal, state snapshots, metrics and canary results.

Hold the source revision, model, runtime, profiles, workload shape, cache policy, and latency targets fixed across the run. Start with two offered rates to establish the scaling direction, then extend the sweep until the client attainment target fails or the intended operating ceiling passes. Let resident requests and transfer leases drain between runs.

For each rate, retain the offered count, terminal count, response classes, TTFT and TPOT distribution, output-length validity, refusal causes and client errors under that deployment identifier.

Copy the canary template, set its model, prompt and exact completion, then derive `expected_token_ids` from that completion under the deployed tokenizer. Add at least one plausible distractor to `allowed_token_ids`; `narwhal-canary` constrains generation to that set and rejects a case whose allowed set equals its expected set.

```bash
cp config/canary-cases.example.json runs/local/canary-cases.json
narwhal-canary \
  --base http://router:8000 \
  --cases runs/local/canary-cases.json \
  --duration <seconds> \
  --out runs/local/canary-results.jsonl
```

The run compares returned text and token IDs with the case, then writes verdicts, timing, token counts, digests and nearby controller events under the [canary artifact contracts](09-API-and-Data-Reference.md#canary-artifacts).

The deployment acceptance record should name the highest offered rate meeting the client SLO target, the tested request shape, the canary result, the preflight revision, and the retained artifact paths. The deployment system owns that policy and decides when a configuration change requires another run.

Return the completed run to the [deployment acceptance sequence](03-Deploy.md#8-validate-production-capacity) for reconciliation, dashboard queries and the post-load KV ring. The [API and data reference](09-API-and-Data-Reference.md) defines journals, state, metrics and canary contracts; [Operate Narwhal](04-Operate.md) covers rollout and recovery.
