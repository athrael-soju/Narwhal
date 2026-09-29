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

Keep this stream-accounting rule with each result set so that runs stay comparable under the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

## 2. Reuse or create an idle-fleet latency profile

[Gate F: Profile idle engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines) binds the `profiles.json` and `profiles.samples.json` pair to the attested process generations that produced it. A measurement run can reuse that pair while the engine processes and runtime stay the same.

Retain a passing preflight for the fleet configuration under test. Profile every new engine process or runtime before you select deployment SLOs. Reserve the production engine shape and warm the model, then sweep the expected traffic shape:

```bash
narwhal-profile \
  --fleet config/fleet.production.json \
  --prefill-lens <comma-separated-input-lengths> \
  --decode-input-lens <comma-separated-input-lengths> \
  --decode-concurrency <comma-separated-stream-counts>
```

### Prefill sweep

Select at least three prefill lengths spanning the production range, including its longest inputs.

The profiler keeps only lengths that fit the live `max_model_len` from `/tokenize` plus one output token. It tokenises each exact prompt before issuing the completion request.

The profiler retains each engine's effective sweep. Compare it with the serving plan before accepting the result.

### Decode sweep

Use at least two decode input lengths and two concurrency values. Each input length and concurrency pair is one decode cell.

For production calibration, use at least three concurrency points, including one stream and the intended operating range.

Decode inputs plus their requested output must fit the live context limit. Long-context deployments need a broader sweep. The profiler stops when fewer than two input lengths fit; choose shorter inputs.

Each probe sets a unique vLLM `cache_salt`, which forces a cold prefill even with prefix caching on. Decode probes request one identified token per SSE event. The profiler checks token identity as events arrive, then checks stream completion and output token counts before it keeps the intervals.

The sweep fails and must be rerun if `vllm:prefix_cache_hits_total` rises during the run, because some prompt tokens came from the cache.

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

The TTFT curve fits the median latency at each exact input length. The profiler rejects a curve that exceeds either bound:

* mean error above 20%;
* worst-point error above 50%.

Measured prefill latency includes the HTTP round trip and one generated token.

### Repair profiles produced by the earlier raw-repeat fitter

Refit from the saved sample sidecar:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

The refit requires saved samples and profile snapshots bound to a process generation for every engine. It writes a new pair with refitted prefill curves and copied decode coefficients.

Samples without process-generation evidence need a fresh sweep against the current engine processes.

After refitting:

1. Set `profiles.path` in the private fleet file to `runs/profiles-refit.json`.
2. Run full preflight against that fleet file and the same engines.
3. Retain both profile pairs in the deployment record.

### Decode fit

The profiler samples token intervals after every stream has emitted its first token and before any stream completes, so the whole decoding cohort is resident. From the client's view it infers the active-request count and resident KV memory. Near scheduler saturation, check those figures against the engine's request and inter-token metrics.

Each profile records the observed range of both fitted axes, which bounds the decode domain the role controller prices.

The fitter constrains coefficients to nonnegative values in both the full fit and the leave-one-cell-out validation. Leave-one-cell-out error shows how well the fit interpolates between cells within one run. Repeat the sweep to see run-to-run stability and tail behaviour. Add intermediate cells that match the production workload to cover gaps.

Default limits:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Keep `profiles.max_decode_fit_mape` at or below `controller.reactive.movement_margin`; fleet validation rejects a larger value. Set both limits from measurements covering the expected decode domain.

When a measured error exceeds its limit, `narwhal-check` reports the engine, the measured value, and the configured threshold. The `profile` gate also requires two measured points on each decode axis.

Reactive control needs the current profile schema and a decode sweep that passed the gate.

Continue with [target selection and deployment freeze](02-Targets-and-Freeze.md).
