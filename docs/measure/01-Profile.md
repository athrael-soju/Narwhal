# Measurement contract and profiling

## 1. Define the measurement contract

Record router-journal and deployment-client latency separately because they use these request boundaries:

| Metric | Router journal                                                        | Deployment client                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| TTFT   | Router arrival to prefill completion                                  | HTTP request start to the first identified output token                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1` | First identified output token to last identified output token, divided by `output_len - 1` |

Router TTFT includes token counting, placement wait, prefill queueing, and prefill execution. For a completed request, `first_byte_s - ttft_s` covers KV transfer and decode queueing before the first visible token.

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

Narwhal includes empty-text and reasoning-only token IDs in output length and computes TPOT for requests with at least two identified tokens. Retain the stream-accounting rule with each result set and use the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) to compare runs with the same denominator and terminal classes.

## 2. Create an idle-fleet latency profile

Reserve the production engine shape, warm the model with prefix caching disabled, and sweep the input lengths, decode contexts, and active sequence counts expected in serving before selecting deployment SLOs:

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

### Prefill sweep

Select at least three prefill lengths spanning the production range, including its longest inputs.

For each engine, the profiler:

1. reads the live `max_model_len` from `/tokenize`;
2. keeps candidate lengths with room for the requested output token;
3. tokenises the exact prompt before issuing the completion request;
4. retains the effective sweep used for that engine.

Compare the retained sweep with the serving plan before accepting the result.

### Decode sweep

Give the decode sweep at least two input lengths and two concurrency values. For production calibration, use at least three concurrency points, including one stream and the intended operating range.

The profiler keeps decode inputs whose input and requested output fit the live context limit. Extend the sweep for long-context deployments; when that limit leaves too few usable cells to fit the profile, select shorter inputs.

Each decode probe requests one identified token per SSE event. The profiler retains token intervals after stream completion, per-event identity, output cardinality, and interval count pass validation.

## 3. Retain profile samples and fits

Retain `profiles.json` and `profiles.samples.json` from `narwhal-profile` with the deployment record.

`profiles.samples.json` contains:

* software identity;
* sweep configuration;
* every prefill repeat;
* the per-length medians used for the TTFT fit;
* decode intervals;
* cell medians;
* fitted profiles.

The sample sidecar retains raw prefill measurements and the fit error when a TTFT fit fails, and keeps completed engine data if a later engine fails. `--overwrite` creates a new output pair for the selected engines.

### KV capacity source

When vLLM's `cache_config_info` reports `kv_cache_size_tokens`, the profiler writes a physical KV constraint. For builds whose capacity input comes from TPOT measurements, it uses the TPOT-derived limit.

## 4. Validate the profile before using it

### Prefill fit

The profiler computes a median latency at each exact input length and fits the TTFT curve against those points. Before decode profiling, it rejects curves whose error exceeds either bound:

* mean error above 20%;
* worst-point error above 50%.

Measured prefill latency includes the HTTP round trip and one generated token.

### Repair profiles produced by the earlier raw-repeat fitter

Refit a completed raw-repeat profile set from its saved sample sidecar:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

The command requires saved samples and profile snapshots for every configured engine. It writes a new output pair with refitted prefill curves and copied measured decode coefficients, preserving the original pair.

After refitting:

1. set `profiles.path` in the private fleet document to `runs/profiles-refit.json`;
2. run the full preflight against that document and the same engine processes;
3. retain both profile pairs in the deployment record.

### Decode fit

The profiler samples token intervals after every stream emits its first token and before any stream completes, keeping the complete decoding cohort resident.

Active-request count and resident KV tokens are inferred from client observations.

Near scheduler saturation, compare those inferred values with engine request metrics and inter-token metrics.

The fitter constrains coefficients to nonnegative values in both the full fit and leave-one-cell-out validation.

Each profile records the observed minimum and maximum of both fitted axes, bounding the decode domain the controller prices.

Leave-one-cell-out error measures interpolation between cells within one run. Repeat the sweep to measure run-to-run stability and tail behaviour, then add intermediate cells resembling the production workload.

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

It requires at least two measured points on each decode axis. Run reactive control with the current profile schema and an accepted decode sweep.

Continue with [target selection and deployment freeze](02-Targets-and-Freeze.md).
