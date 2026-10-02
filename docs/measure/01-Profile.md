---
description: Define the TTFT and TPOT measurement contract and build a validated idle-fleet latency profile.
---

# Measurement contract and profiling

## 1. Defining the measurement contract

Record time to first token (TTFT) and time per output token (TPOT) separately for each source.

The router journal measures TTFT from router arrival to prefill completion. It measures TPOT from prefill completion to the final decode token, divided by `output_len - 1`.

The deployment client measures TTFT from HTTP request start to the first identified output token. It measures TPOT from the first identified output token to the last identified output token, divided by `output_len - 1`.

Router TTFT covers token counting, placement wait, prefill queueing, and prefill execution. For a completed request, `first_byte_s - ttft_s` covers KV transfer and decode queueing before the first visible token.

For every scheduled request, the deployment client must retain:

- scheduled start
- actual start
- response status
- requested output length
- completed output length
- TTFT
- TPOT
- terminal error information

The service-level objective (SLO) denominator counts every scored request, including refused, failed, and cancelled requests. Output length counts every identified token ID, including empty-text and reasoning-only tokens. TPOT requires at least two identified tokens.

Record the [journal contract](../telemetry/01-Journal.md#diagnosing-a-request-from-the-journal) stream-accounting rule with each result set.

## 2. Reusing or creating an idle-fleet latency profile

A measurement run can reuse the `profiles.json` and `profiles.samples.json` pair from [Gate F: Profiling idle engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) while each engine's process generation stays the same.

Profile each engine whose process generation changed before you select deployment SLOs:

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

- Select at least three prefill lengths spanning the production range, including its longest inputs.
- The effective sweep is each prefill length whose input plus one output token fits the live `max_model_len` from `/tokenize`.
- Compare each engine's effective sweep in `profiles.samples.json` with the serving plan.

The prefill fit is `a*n*n + b*n + c + ttft_split*s`. In it, `n` is the prompt token count, and `s` is 1 for a prompt that ends inside a cache block past the first, otherwise 0. The cache block size comes from `vllm:cache_config_info`.

The profiler sets `ttft_split` when all of these hold:

- The sweep has at least six lengths.
- At least two lengths end within the first block or on a block boundary.
- At least two lengths end between later block boundaries.
- The fit with the split term has under half the mean error of the plain fit.

Otherwise `ttft_split` is `null`.

### Decode sweep

A decode cell is one decode input length and concurrency pair.

- Use at least two decode input lengths and two concurrency values.
- For production calibration, use at least three concurrency points, including one stream and the intended operating range.
- With `--limits`, each engine's top concurrency point is its `max_num_seqs`.
- Use a broader sweep for long-context deployments.
- Each decode input plus its requested output must fit the live context limit.
- When fewer than two decode input lengths fit, choose shorter inputs.
- When a cold sweep fails on a rise in `vllm:prefix_cache_hits_total`, rerun the sweep with the engine reserved for profiling.

### Warm prefill with a cached prefix

The warm sweep measures prefill for a prompt whose prefix sits in the engine's prefix cache.

A warm case is one `--cached-prefix-lens` and `--cached-suffix-lens` pair whose prefix, suffix, and one output token fit the live `max_model_len`.

Each case runs three times with three requests per run:

| Request | Cache salt | Prompt | Recorded |
| --- | --- | --- | --- |
| Primer | Fresh | Prefix plus up to eight padding words, ending inside a block past the prefix's last full block | |
| Warm | The primer's | Prefix and suffix | Latency, cached tokens from the hit counter, uncached tokens |
| Cold control | Fresh | Prefix and suffix | Latency |

The warm fit is `c + b*S + d*P + a*(2*P*S + S*S) + ttft_split*s` over the median of each case's runs:

| Term | Value |
| --- | --- |
| `P` | Cached prefix tokens |
| `S` | Uncached suffix tokens |
| `s` | 1 for a suffix that ends inside a cache block past its first, otherwise 0 |
| `d*P` | Cached-prefix read |
| `a*2*P*S` | Suffix attention to the prefix |

The profiler reports four errors:

| Error | `cached_prefill` field |
| --- | --- |
| Leave-one-case-out error of the warm fit | `cv_mape` |
| Suffix priced on the cold curve | `suffix_on_cold_curve_mape` |
| Full prompt priced on the cold curve | `full_prompt_cold_mape` |
| Cold curve against the measured cold controls | `cold_control_curve_mape` |

The warm sweep runs on an engine when all of these hold:

- The engine exports `vllm:prefix_cache_hits_total`.
- At least two prefix lengths, two suffix lengths, and five cases fit within `max_model_len`.

An engine keeps cold pricing when any of these hold:

- The engine fails a warm sweep condition.
- A primer ends at or before the end of its prefix's last full block, or on a block boundary, after eight padding words.
- `vllm:prefix_cache_hits_total` becomes unreadable during the warm sweep.
- A warm case reuses zero cached tokens.
- The measured warm cases cover fewer than two prefix lengths, two suffix lengths, or five cases.
- The held-out error exceeds 20%.

The profiler records the reason in `cached_prefill.reason`.

If the warm sweep fails with `a cold control reused a cached prefix; reserve the engine`:

1. Reserve the engine.
2. Rerun the profile.

Roll out the warm fit:

1. Record a held-out error threshold in the private execution record.
2. Set `profiles.path` in a copy of the private fleet file to a separate path.
3. Profile one engine with `--only <iid>` against the fleet copy.
4. Compare its held-out error with the recorded threshold.
5. When the held-out error is at or below the recorded threshold, profile every engine with `--overwrite` against the private fleet file.

## 3. Retaining profile samples and fits

Keep `profiles.json` and `profiles.samples.json` from `narwhal-profile` with the deployment record.

`profiles.samples.json` contains:

- software identity
- sweep configuration
- every prefill repeat
- the per-length medians used for the TTFT fit
- decode intervals and cell medians
- fitted profiles
- `prefill_block_tokens`: the cache block size the engine exports, otherwise `null`
- `cached_prefill`: warm prefill evidence per engine
    - warm samples and cold controls with their prefix and suffix tokens
    - fit points
    - `cv_mape`, `suffix_on_cold_curve_mape`, `full_prompt_cold_mape`, and `cold_control_curve_mape`
    - `reason` for an engine that keeps cold pricing
- `prefix_cache_hit_tokens`: prefix-cache hits per engine cold sweep
    - `null` when `vllm:prefix_cache_hits_total` is absent from engine metrics or decreases
- the attestation response or process identity per fit

When the TTFT fit fails, the sample sidecar keeps the raw prefill measurements and fit error. When a later engine fails, it keeps the data from completed engines. With `--overwrite`, `narwhal-profile` writes a new output pair for the selected engines.

### KV capacity source

When vLLM `cache_config_info` reports `kv_cache_size_tokens`, the KV capacity source is the physical KV constraint. When it omits `kv_cache_size_tokens`, the source is the TPOT-derived limit.

## 4. Validating the profile before using it

### Prefill fit

Measured prefill latency covers the HTTP round trip plus one generated token.

The profiler rejects a TTFT curve over the per-length medians when its mean error exceeds 20% or its worst-point error exceeds 50%.

### Refitting saved profile samples

When the sample sidecar holds saved samples and profile snapshots bound to a process generation for every engine, refit with `--refit-samples`. For any other sidecar, run a fresh sweep against the current engine processes.

Refit from the saved sample sidecar:

```bash
narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --refit-samples runs/profiles.samples.json \
  --out runs/profiles-refit.json
```

The refit writes a new pair holding:

- refitted cold and warm prefill curves
- copied decode coefficients

An engine whose saved warm evidence records a `reason` keeps cold pricing with that reason.

Activate the refitted pair:

1. Set `profiles.path` in the private fleet file to `runs/profiles-refit.json`.
2. Run full preflight against that fleet file and the same engines.
3. Retain both profile pairs in the deployment record.

### Decode fit

Near scheduler saturation, compare these profiler measurements with the engine's request and inter-token metrics:

- client-side active-request count
- resident KV memory

The leave-one-cell-out error measures interpolation between cells within one run. Repeated sweeps measure run-to-run stability and tail behavior.

Fill gaps between cells with intermediate cells that match the production workload.

Set the decode error limits from measurements covering the expected decode domain. `profiles.max_decode_fit_mape` caps the in-sample fit error and defaults to `0.05`. `profiles.max_decode_cv_mape` caps the leave-one-cell-out cross-validation error and defaults to `0.13`.

Fleet validation requires `profiles.max_decode_fit_mape` at or below `controller.reactive.movement_margin`.

The `narwhal-check` `profile` gate requires:

- two measured points on each decode axis
- both decode errors within their limits

Reactive control needs:

- the current profile schema
- a decode sweep that passed the `profile` gate
