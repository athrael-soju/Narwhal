# Measurement contract and profiling

This page defines how time to first token (TTFT) and time per output token (TPOT) are measured, and how to build and validate the latency profile the router uses.

## 1. Define the measurement contract

The router and the deployment client measure TTFT and TPOT from different starting points. Record the two separately.

| Metric | Router journal                                                        | Deployment client                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| TTFT   | Router arrival to prefill completion                                  | HTTP request start to the first identified output token                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1` | First identified output token to last identified output token, divided by `output_len - 1` |

Router TTFT includes token counting, placement wait, prefill queueing, and prefill. It excludes the key-value (KV) cache transfer. For a completed request, `first_byte_s - ttft_s` is the time spent moving KV to the decode engine and waiting in the decode queue before the first visible token.

The client must keep a record for every scheduled request, including requests that fail. Each record needs the scheduled start, actual start, response status, requested and completed output length, TTFT, TPOT, and any terminal error. Refused, failed, and canceled requests count against the service-level objective (SLO). Leaving them out makes an overloaded fleet look healthy.

Narwhal counts a token toward output length when it has a token ID, including tokens with empty text or reasoning-only output. TPOT is computed only for requests with at least two identified tokens. Record this rule alongside each result set. To compare two runs, apply the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) to both so they share the same denominator and the same terminal classes. A terminal class is the final outcome recorded for a request.

## 2. Reuse or create an idle-fleet latency profile

A profile pair is two files, `profiles.json` and `profiles.samples.json`, which the profile gate ([Gate F](../deploy/06-Profile-and-Preflight.md)) binds to specific engine generations. If the engine processes and runtime are unchanged since that gate, reuse the pair. The fleet configuration under test still needs a passing preflight.

If you started a new engine process or changed the runtime, build a fresh profile before choosing SLOs. Reserve the production engine shape, warm the model with prefix caching disabled, and sweep the production input lengths, decode contexts, and concurrency:

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

### Prefill sweep

Use at least three prefill lengths that span the production range, including the longest inputs you serve.

For each engine, the profiler reads the live `max_model_len` from `/tokenize` and drops any length that leaves no room for the output token. It tokenizes each prompt before sending it and records the sweep it ran. Compare the recorded sweep with your serving plan: the profile does not cover inputs dropped because of the context limit.

### Decode sweep

A decode cell is one combination of input length and concurrency value. Use at least two input lengths and two concurrency values. For production calibration, use at least three concurrency points: a single stream, plus values across the intended operating range.

As in the prefill sweep, the profiler skips any cell where the input plus the requested output does not fit in the live context limit. For long-context deployments, add longer inputs. If too few cells remain to fit a profile, use shorter inputs.

Each decode probe asks for one token per server-sent event (SSE). The profiler checks token identity as events arrive, and after the streams end it checks that each one completed with the expected number of tokens. It keeps the intervals when every stream passes and the run collected enough of them.

## 3. Retain profile samples and fits

Keep both output files from `narwhal-profile` with the deployment record. The router uses `profiles.json`. The samples file supports re-checking or refitting the profile later and contains:

- the software identity and sweep configuration
- every prefill repeat, and the per-length medians used for the TTFT fit
- decode intervals and cell medians
- the fitted profiles
- the verified attestation response or process identity that ties each fit to its engine generation

If a TTFT fit fails, the samples file still keeps the raw prefill measurements and the fit error. If one engine fails partway through, the data from engines that finished is kept. Pass `--overwrite` to write a new profile pair for the selected engines.

### Where KV capacity comes from

If vLLM reports `kv_cache_size_tokens` in `cache_config_info`, the profiler records a physical KV limit from it. On builds that omit it, the profiler uses a limit derived from the TPOT measurements.

## 4. Validate the profile before using it

### Prefill fit

The profiler takes the median latency at each input length and fits the TTFT curve through those medians. Before decode profiling, it rejects the fit if the mean error is above 20% or any single point is off by more than 50%.

Latencies are measured from the client, so they include the HTTP round trip and one generated token. Measured TTFT exceeds prefill time by that amount.

### Decode fit

The profiler measures token intervals only while the whole cohort is decoding: after every stream has emitted its first token and before any stream finishes. This fixes the number of active requests during the measurement.

The profiler infers the active-request count and resident KV tokens from client observations. The engine does not report them. Near scheduler saturation, compare them with the engine's request and inter-token metrics.

Coefficients stay nonnegative in both the full fit and the leave-one-cell-out validation. Each profile records the smallest and largest values observed on each fitted axis, and the controller uses the decode fit only inside that range.

Leave-one-cell-out error measures interpolation between cells within one run. It does not capture run-to-run variance or tail behavior. To assess those, repeat the sweep and add intermediate cells that match the production workload.

The default error limits are:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Override both only with measurements that cover the production decode range. The fit limit cannot exceed `controller.reactive.movement_margin`; a fleet config that sets it higher fails validation.

If either limit is exceeded, a live sweep rejects the profile, and `narwhal-check` reports the engine, the measured value, and the configured limit. `narwhal-check` also requires at least two measured points on each decode axis. Reactive control requires a profile in the current schema with an accepted decode sweep.

### Refitting profiles from older releases

Older releases of the profiler fitted the TTFT curve to every raw repeat. Refit such a profile from its saved samples to use per-length medians from the existing data:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

The refit requires a samples file bound to engine generations with profile snapshots for every configured engine. Samples files without engine-generation binding need a fresh sweep against the current engine processes. The command writes a new pair with refitted prefill curves and the original measured decode coefficients, and leaves the original files unchanged.

After refitting:

1. Point `profiles.path` in the private fleet document at `runs/profiles-refit.json`.
2. Run the full preflight against that document and the same engine processes.
3. Keep both profile pairs in the deployment record.

Next: [targets and deployment freeze](02-Targets-and-Freeze.md).
