# Measurement contract and profiling

## 1. Define the measurement contract

The router journal and deployment client observe different portions of request latency. Preserve both views.

| Metric | Router journal                                                        | Deployment client                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| TTFT   | Router arrival to prefill completion                                  | HTTP request start to the first identified output token                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1` | First identified output token to last identified output token, divided by `output_len - 1` |

Router TTFT includes token counting, placement wait, prefill queueing, and prefill execution.

For a completed request:

```text
first_byte_s - ttft_s
```

covers KV transfer and decode queueing before the first visible token.

The deployment client must retain, for every scheduled request:

* scheduled start
* actual start
* response status
* requested output length
* completed output length
* TTFT
* TPOT
* terminal error information

Refused and failed scored requests remain in the SLO denominator.

Keep the stream-accounting rule with every result set. Changing token accounting changes the denominator and invalidates comparison with earlier runs.

The router journal counts all identified output tokens, including empty-text tokens and reasoning-only output. TPOT is defined only when at least two output tokens are present. Router-side denominators and terminal outcome semantics are defined by the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

## 2. Create an idle-fleet latency profile

Profile the production serving shape before selecting deployment SLOs.

Reserve the engines, warm the model, and disable prefix caching.

Measure the range of input lengths, decode context lengths, and active sequence counts expected in production:

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

### Prefill sweep

Use at least three prefill lengths. Include the longest production inputs.

For each engine, the profiler:

1. reads the live `max_model_len` from `/tokenize`;
2. removes candidate lengths that do not leave room for the requested output token;
3. tokenises the exact prompt before issuing the completion request;
4. retains the effective sweep used for that engine.

Compare the retained sweep with the serving plan before accepting the result.

### Decode sweep

Decode calibration must vary both context length and concurrency.

The profiler requires:

* at least two decode input lengths;
* at least two concurrency values.

For production calibration, use at least three concurrency points. Include one stream and values spanning the intended operating range.

Decode input candidates are bounded by the same live context limit plus requested output tokens. For long-context deployments, extend the sweep deliberately. If the context bound leaves too few usable cells, choose shorter points.

Each decode probe requests exact output token IDs with one token per SSE event. The profiler verifies:

* stream completion;
* token identity for each event;
* output token cardinality;
* interval count.

Any failed check aborts the probe.

## 3. Preserve the complete profile evidence

`narwhal-profile` produces:

```text
profiles.json
profiles.samples.json
```

Retain both files with the deployment record.

`profiles.samples.json` contains:

* software identity;
* sweep configuration;
* every prefill repeat;
* the per-length medians used for the TTFT fit;
* decode intervals;
* cell medians;
* fitted profiles.

If a TTFT fit fails, the sidecar retains the raw prefill measurements and the fit error.

If profiling fails on a later engine, already completed engine data remains in the sidecar.

Using `--overwrite` creates a new output pair containing only the selected engines.

### KV capacity source

The profiler reads `kv_cache_size_tokens` from vLLM's `cache_config_info` metric.

When that field is available, the profile receives a physical KV constraint. Builds that do not expose it use the TPOT-derived capacity limit.

## 4. Validate the profile before using it

### Prefill fit

The profiler fits one median latency for each exact input length.

Before decode profiling begins, the profiler rejects a TTFT curve whose error against those medians exceeds either of these bounds:

* mean error above 20%;
* worst-point error above 50%.

Measured prefill latency includes the HTTP round trip and one generated token.

### Repair profiles produced by the earlier raw-repeat fitter

A completed profile set produced by the earlier fitting method can be rebuilt from its saved sample sidecar without contacting the engines:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

The refit command requires saved samples and profile snapshots for every configured engine.

It:

* leaves the original pair unchanged;
* rebuilds the prefill fit from the retained samples;
* copies the measured decode coefficients into the new profiles.

After refitting:

1. set `profiles.path` in the private fleet document to `runs/profiles-refit.json`;
2. run the full preflight against that document and the unchanged engine processes;
3. retain both profile pairs in the deployment record.

### Decode fit

The profiler samples token intervals only from the interior of a complete decoding cohort:

* after every stream has emitted its first token;
* before any stream has completed.

Active-request count and resident KV tokens are inferred from client observations.

Near scheduler saturation, compare those inferred values with engine request metrics and inter-token metrics.

Decode fitting uses nonnegative coefficients. Leave-one-cell-out cross-validation uses the same constrained estimator.

Each profile records the observed minimum and maximum for both fitted axes. That rectangle defines the decode domain the controller is allowed to price.

Repeat the sweep, then add intermediate cells resembling the production workload.

The two tests answer different questions:

* leave-one-cell-out error measures interpolation within one run;
* the repeated sweep measures run-to-run stability and tail behaviour.

Default limits are:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Keep the fit limit at or below:

```text
controller.reactive.movement_margin
```

Set both error limits from measurements covering the deployment's expected decode domain.

`narwhal-check` reports the engine, measured value, and configured limit when either threshold is exceeded.

It also rejects profiles whose measured domain contains only one point on either axis.

Reactive control should operate only against the current profile schema and an accepted decode sweep.

Continue with [target selection and deployment freeze](02-Targets-and-Freeze.md).
