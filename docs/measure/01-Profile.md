# Measurement contract and profiling

## 1. Define the measurement contract

Record time to first token (TTFT) and time per output token (TPOT) separately for each source:

| Metric | Router journal                                                        | Deployment client                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| TTFT   | Router arrival to prefill completion                                  | HTTP request start to the first identified output token                                    |
| TPOT   | Prefill completion to final decode token, divided by `output_len - 1` | First identified output token to last identified output token, divided by `output_len - 1` |

| Interval | Covers |
| --- | --- |
| Router TTFT | Token counting, placement wait, prefill queueing, and prefill execution |
| `first_byte_s - ttft_s` for a completed request | KV transfer and decode queueing before the first visible token |

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

- Output length includes empty-text and reasoning-only token IDs.
- TPOT requires at least two identified tokens.

Record the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) stream-accounting rule with each result set.

## 2. Reuse or create an idle-fleet latency profile

A measurement run can reuse the `profiles.json` and `profiles.samples.json` pair from [Gate F: Profile idle engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines) while the engine processes and runtime stay the same.

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

The effective sweep is each prefill length whose input plus one output token fits the live `max_model_len` from `/tokenize`.

Compare each engine's effective sweep in `profiles.samples.json` with the serving plan.

### Decode sweep

A decode cell is one decode input length and concurrency pair.

- Use at least two decode input lengths and two concurrency values.
- For production calibration, use at least three concurrency points, including one stream and the intended operating range.
- Long-context deployments need a broader sweep.
- Each decode input plus its requested output must fit the live context limit.
- When fewer than two decode input lengths fit, choose shorter inputs.
- Rerun a sweep that fails on a rise in `vllm:prefix_cache_hits_total`.

## 3. Retain profile samples and fits

Keep `profiles.json` and `profiles.samples.json` from `narwhal-profile` with the deployment record.

`profiles.samples.json` contains:

- software identity
- sweep configuration
- every prefill repeat
- the per-length medians used for the TTFT fit
- decode intervals and cell medians
- fitted profiles
- `prefix_cache_hit_tokens`: prefix-cache hits per engine sweep
  - `null` when `vllm:prefix_cache_hits_total` is absent from engine metrics
- the attestation response or process identity per fit

| Case                  | Sample sidecar result                               |
| --------------------- | --------------------------------------------------- |
| TTFT fit fails        | Keeps the raw prefill measurements and fit error    |
| A later engine fails  | Keeps the data from completed engines               |
| `--overwrite`         | Writes a new output pair for the selected engines   |

### KV capacity source

| vLLM `cache_config_info` | KV capacity source |
| --- | --- |
| Reports `kv_cache_size_tokens` | Physical KV constraint |
| Omits `kv_cache_size_tokens` | TPOT-derived limit |

## 4. Validate the profile before using it

### Prefill fit

Measured prefill latency covers the HTTP round trip plus one generated token.

TTFT curve rejection bounds:

- mean error above 20%
- worst-point error above 50%

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

The refit writes a new pair holding:

- refitted prefill curves
- copied decode coefficients

Activate the refitted pair:

1. Set `profiles.path` in the private fleet file to `runs/profiles-refit.json`.
2. Run full preflight against that fleet file and the same engines.
3. Retain both profile pairs in the deployment record.

### Decode fit

Near scheduler saturation, compare these profiler measurements with the engine's request and inter-token metrics:

- client-side active-request count
- resident KV memory

| Evidence | Measures |
| --- | --- |
| Leave-one-cell-out error | Interpolation between cells within one run |
| Repeated sweeps | Run-to-run stability and tail behavior |

Fill gaps between cells with intermediate cells that match the production workload.

Set the default limits from measurements covering the expected decode domain:

```text
profiles.max_decode_fit_mape = 0.05
profiles.max_decode_cv_mape  = 0.13
```

Fleet validation requires `profiles.max_decode_fit_mape` at or below `controller.reactive.movement_margin`.

The `narwhal-check` `profile` gate requires:

- two measured points on each decode axis
- both decode errors within their limits

Reactive control needs:

- the current profile schema
- a decode sweep that passed the `profile` gate
