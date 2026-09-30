# Measurement contract and profiling

Before you can trust a latency number, you need to know exactly where it starts and stops. You also need a profile that tells the router what the engines can actually do. This page covers both.

## 1. Define the measurement contract

The router and the deployment client measure time to first token (TTFT) and time per output token (TPOT) from different starting points, so record the two separately.

| Metric | Router journal                                                        | Deployment client                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| TTFT   | Router arrival to prefill completion                                  | HTTP request start to the first identified output token                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1` | First identified output token to last identified output token, divided by `output_len - 1` |

Router TTFT covers everything that happens before the first token exists: token counting, waiting for placement, prefill queueing, and prefill itself. It stops before the key-value (KV) cache transfer. For a completed request, `first_byte_s - ttft_s` is the time spent moving KV to the decode engine and waiting in the decode queue before the first visible token.

The client has to keep a record for every scheduled request, including the ones that never succeed. Each record needs the scheduled start, actual start, response status, requested and completed output length, TTFT, TPOT, and any terminal error. Refused, failed, and canceled requests still count against the service-level objective (SLO). Leaving them out would make an overloaded fleet look healthy.

Narwhal counts a token toward output length whenever it has a token ID, even if its text is empty or it only appears as reasoning output. TPOT is only computed for requests with at least two identified tokens. Keep this accounting rule with each result set. When you compare two runs, use the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) to make sure they share the same denominator and the same terminal classes (the final outcome recorded for each request).

## 2. Reuse or create an idle-fleet latency profile

A profile is a pair of files, `profiles.json` and `profiles.samples.json`, which [Gate F](../deploy/06-Profile-and-Preflight.md) binds to specific engine generations. If the engine processes and runtime haven't changed since that gate, reuse the pair. You still need a passing preflight for the fleet configuration you're about to test.

If you've started a new engine process or changed the runtime, build a fresh profile before choosing SLOs. Reserve the production engine shape, warm the model with prefix caching disabled, and sweep the input lengths, decode contexts, and concurrency you expect in production:

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

### Prefill sweep

Choose at least three prefill lengths that span the production range, and include the longest inputs you expect to serve.

The profiler may not run every length you ask for. For each engine, it reads the live `max_model_len` from `/tokenize` and drops any length that leaves no room for the output token. It tokenizes each prompt exactly before sending it, then records the sweep it actually ran. Compare that recorded sweep with your serving plan. If the engine's context limit cut off your longest inputs, the profile won't cover them.

### Decode sweep

The minimum is two input lengths and two concurrency values. For production calibration, use at least three concurrency points: a single stream, plus values across your intended operating range.

As with prefill, the profiler skips any cell where the input plus the requested output won't fit in the live context limit. For long-context deployments, add longer inputs to the sweep. If the context limit leaves too few cells to fit a profile, use shorter inputs instead.

Each decode probe asks for one token per server-sent event (SSE). The profiler checks token identity as events arrive, and once the streams end it checks that each one completed with the right number of tokens. It keeps the intervals only if every stream passes and the run collected enough of them.

## 3. Retain profile samples and fits

Keep both output files from `narwhal-profile` with the deployment record. The router uses `profiles.json`. The samples file is what lets you check, refit, or defend that profile later. It holds:

- the software identity and sweep configuration
- every prefill repeat, and the per-length medians used for the TTFT fit
- decode intervals and cell medians
- the fitted profiles
- the verified attestation response or process identity that ties each fit to its engine generation

If a TTFT fit fails, the samples file still keeps the raw prefill measurements and the fit error. If one engine fails partway through, the data from engines that finished is kept. Pass `--overwrite` to write a new output pair for the selected engines.

### Where KV capacity comes from

If vLLM reports `kv_cache_size_tokens` in `cache_config_info`, the profiler records a physical KV limit from it. On builds that don't, it uses a limit derived from the TPOT measurements.

## 4. Validate the profile before using it

### Prefill fit

The profiler takes the median latency at each input length and fits the TTFT curve through those medians. Before it moves on to decode profiling, it rejects the curve if the mean error is above 20% or any single point is off by more than 50%.

These latencies are measured from the client, so they include the HTTP round trip and one generated token. Expect them to sit a little above pure prefill time.

### Decode fit

The profiler only measures token intervals while the whole cohort is decoding, after every stream has emitted its first token and before any stream finishes. That keeps the number of active requests fixed for the length of the measurement.

The active-request count and resident KV tokens are inferred from what the client sees, not reported by the engine. Near scheduler saturation, compare them with the engine's request and inter-token metrics.

Coefficients are kept nonnegative in both the full fit and the leave-one-cell-out validation. Each profile also records the smallest and largest values it observed on both fitted axes, and the controller only prices decode inside that range.

Leave-one-cell-out error tells you how well the fit interpolates between cells within one run. It won't tell you how much results move from run to run, or what the tail looks like. For that, repeat the sweep and add intermediate cells that look like your production workload.

The default error limits are:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Set both from measurements that cover the decode range you expect in production. The fit limit can't exceed `controller.reactive.movement_margin`; a fleet config that sets it higher fails validation.

If either limit is exceeded, a live sweep rejects the profile, and `narwhal-check` reports the engine, the measured value, and the configured limit. It also requires at least two measured points on each decode axis. Reactive control needs a profile in the current schema with an accepted decode sweep.

### Refitting profiles from older releases

Older releases of the profiler fitted the TTFT curve to every raw repeat instead of to per-length medians. You can refit one of those profile sets from its saved samples without running the sweep again:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

This only works if the samples file is bound to engine generations and includes profile snapshots for every configured engine. Sample files from before that was recorded need a fresh sweep against the current engine processes. The command writes a new pair with refitted prefill curves and the original measured decode coefficients, and leaves the original files alone.

After refitting:

1. Point `profiles.path` in the private fleet document at `runs/profiles-refit.json`.
2. Run the full preflight against that document and the same engine processes.
3. Keep both profile pairs in the deployment record.

Next: [targets and deployment freeze](02-Targets-and-Freeze.md).
