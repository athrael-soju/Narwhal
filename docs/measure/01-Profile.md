# Measurement contract and profiling

## 1. Define the measurement contract

Record time to first token (TTFT) and time per output token (TPOT) separately for each source:

| Metric | Router journal                                                        | Deployment client                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| TTFT   | Router arrival to prefill completion                                  | HTTP request start to the first identified output token                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1` | First identified output token to last identified output token, divided by `output_len - 1` |

Router TTFT includes token counting, placement wait, prefill queueing, and prefill execution. For a completed request, `first_byte_s - ttft_s` covers KV transfer and decode queueing before the first visible token.

For every scheduled request, the deployment client must retain:

* scheduled start
* actual start
* response status
* requested output length
* completed output length
* TTFT
* TPOT
* terminal error information

The service-level objective (SLO) denominator includes refused, failed, and cancelled scored requests.

Output length includes empty-text and reasoning-only token IDs. TPOT requires at least two identified tokens.

Record the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) stream-accounting rule with each result set.

## 2. Reuse or create an idle-fleet latency profile

[Gate F: Profile idle engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines) binds the `profiles.json` and `profiles.samples.json` pair to the attested process generations that produced it. A measurement run can reuse that pair while the engine processes and runtime stay the same.

Profile every new engine process or runtime before you select deployment SLOs:

1. Retain a passing preflight for the fleet configuration under test.
2. Reserve the production engine shape.
3. Warm the model.
4. Sweep the expected traffic shape:

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

### Prefill sweep

Select at least three prefill lengths spanning the production range, including its longest inputs.

The profiler keeps each length that, plus one output token, fits the live `max_model_len` from `/tokenize`.

The profiler retains each engine's effective sweep. Compare it with the serving plan before you accept the result.

### Decode sweep

Use at least two decode input lengths and two concurrency values. Each input length and concurrency pair is one decode cell.

For production calibration, use at least three concurrency points, including one stream and the intended operating range.

Decode inputs plus their requested output must fit the live context limit. Long-context deployments need a broader sweep. When fewer than two input lengths fit, the profiler stops; choose shorter inputs.

Each probe sets a unique vLLM `cache_salt` for a cold prefill. A sweep fails when `vllm:prefix_cache_hits_total` rises during the run; rerun it.

## 3. Retain profile samples and fits

Keep `profiles.json` and `profiles.samples.json` from `narwhal-profile` with the deployment record.

`profiles.samples.json` contains:

* software identity;
* sweep configuration;
* every prefill repeat;
* the per-length medians used for the TTFT fit;
* decode intervals and cell medians;
* fitted profiles;
* `prefix_cache_hit_tokens`: prefix-cache hits per engine sweep, or `null` when engine metrics omit `vllm:prefix_cache_hits_total`;
* the attestation response or process identity that ties each fit to its process generation.

| Case                  | Sample sidecar result                               |
| --------------------- | --------------------------------------------------- |
| TTFT fit fails        | Keeps the raw prefill measurements and fit error    |
| A later engine fails  | Keeps the data from completed engines               |
| `--overwrite`         | Writes a new output pair for the selected engines   |

### KV capacity source

When vLLM's `cache_config_info` reports `kv_cache_size_tokens`, the profiler writes a physical KV constraint. Otherwise it uses the TPOT-derived limit.

## 4. Validate the profile before using it

### Prefill fit

Measured prefill latency includes the HTTP round trip and one generated token. The TTFT curve fits the median latency at each exact input length. The profiler rejects a curve that exceeds either bound:

* mean error above 20%;
* worst-point error above 50%.

### Repair profiles produced by the earlier raw-repeat fitter

| Sample sidecar | Repair |
| --- | --- |
| Saved samples and profile snapshots bound to a process generation for every engine | Refit with `--refit-samples` |
| Any other sidecar | Fresh sweep against the current engine processes |

Refit from the saved sample sidecar:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

The refit writes a new pair with refitted prefill curves and copied decode coefficients.

Activate the refitted pair:

1. Set `profiles.path` in the private fleet file to `runs/profiles-refit.json`.
2. Run full preflight against that fleet file and the same engines.
3. Retain both profile pairs in the deployment record.

### Decode fit

The profiler infers the active-request count and resident KV memory from the client side. Near scheduler saturation, check both figures against the engine's request and inter-token metrics.

The role controller prices decode within each profile's observed range on both fitted axes.

The fitter constrains coefficients to nonnegative values in the full fit and in leave-one-cell-out validation.

| Evidence | Measures |
| --- | --- |
| Leave-one-cell-out error | Interpolation between cells within one run |
| Repeated sweeps | Run-to-run stability and tail behaviour |

Fill gaps between cells with intermediate cells that match the production workload.

Default limits:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Fleet validation requires `profiles.max_decode_fit_mape` at or below `controller.reactive.movement_margin`. Set both limits from measurements covering the expected decode domain.

The `narwhal-check` `profile` gate requires two measured points on each decode axis and both decode errors within their limits. For an error over its limit, it reports the engine, the measured value, and the configured threshold.

Reactive control needs the current profile schema and a decode sweep that passed the `profile` gate.

Next: [target selection and deployment freeze](02-Targets-and-Freeze.md).
