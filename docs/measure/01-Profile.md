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

Refused, failed, and cancelled scored requests remain in the SLO denominator.

Narwhal includes empty-text and reasoning-only token IDs in output length and computes TPOT for requests with at least two identified tokens. Retain the stream-accounting rule with each result set and use the [journal contract](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) to compare runs with the same denominator and terminal classes.

## 2. Reuse or create an idle-fleet latency profile

[Gate F](../deploy/06-Profile-and-Preflight.md) binds the retained `profiles.json` and `profiles.samples.json` pair to attested engine generations. Measurement runs can reuse the pair while the engine processes and runtime remain unchanged. Retain a passing preflight for the fleet configuration under test.

For a new engine process or runtime, reserve the production engine shape, warm the model, and sweep the input lengths, decode contexts, and active sequence counts expected in serving before selecting deployment SLOs:

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

The prefill fit is `a*n*n + b*n + c` for `n` prompt tokens. When the engine exports its cache block size in `vllm:cache_config_info`, the fit adds `ttft_split` for a prompt that ends inside a cache block past the first. An engine that prefills such a prompt in two steps measures a positive `ttft_split`. Include lengths within the first block and lengths between later block boundaries, such as the defaults.

### Decode sweep

Give the decode sweep at least two input lengths and two concurrency values. For production calibration, use at least three concurrency points, including one stream and the intended operating range.

The profiler keeps decode inputs whose input and requested output fit the live context limit. Extend the sweep for long-context deployments; when that limit leaves too few usable cells to fit the profile, select shorter inputs.

Each cold probe request sets a unique vLLM `cache_salt` to prevent prefix cache reuse. The profile measures cold prefill regardless of the engine's prefix caching configuration. The profiler checks `vllm:prefix_cache_hits_total` before and after each cold sweep. If any prompt tokens were served from the prefix cache, the sweep fails and must be rerun.

Each decode probe requests one identified token per SSE event. The profiler validates token identity as events arrive, then checks stream completion and output token counts. It retains the intervals only after all streams pass these checks and enough intervals have been collected.

### Warm prefill with a cached prefix

After the cold sweeps, the profiler measures prefill when part of the prompt comes from the engine's prefix cache. It runs only when the engine exports `vllm:prefix_cache_hits_total` and reuses a cached prefix; otherwise the profile keeps cold pricing only.

For each `--cached-prefix-lens` and `--cached-suffix-lens` pair within the engine's `max_model_len`, the profiler repeats three requests three times:

1. It sends the prefix and the suffix's first word under a fresh cache salt. A hybrid attention and Mamba engine keeps boundary state for a prompt's last full block when the prompt extends past that block.
2. It times the prefix plus the suffix under the same salt. The hit counter's increase is the sample's cached prefix length; the remaining prompt tokens are its uncached suffix.
3. It times the same prompt under another fresh salt as a cold control.

The warm fit is `c + b*S + d*P + a*(2*P*S + S*S)` for `P` cached tokens and `S` uncached tokens. The `d*P` term covers each step's read of the cached prefix, and the `P*S` term covers the suffix's attention to it. Each case contributes the medians of its repeats, and the fit needs at least five cases. The profiler reports four errors:

* the fit's leave-one-case-out error;
* pricing only the suffix on the cold curve;
* pricing the full prompt on the cold curve;
* the cold curve against the measured cold controls.

Retain a threshold for the held-out error in the private execution record before profiling. An engine keeps cold pricing when its samples fall short of a warm fit or its held-out error exceeds 20%. The profiler prints the reason.

## 3. Retain profile samples and fits

Retain `profiles.json` and `profiles.samples.json` from `narwhal-profile` with the deployment record.

`profiles.samples.json` contains:

* software identity;
* sweep configuration;
* every prefill repeat;
* the per-length medians used for the TTFT fit;
* decode intervals;
* cell medians;
* fitted profiles;
* `prefill_block_tokens`, the engine's cache block size, or `null` when the engine exports none;
* `cached_prefill`: warm samples and cold controls with their prefix and suffix tokens, plus the fit points, `cv_mape`, `suffix_on_cold_curve_mape`, `full_prompt_cold_mape` and `cold_control_curve_mape`;
* `cached_prefill.reason` for an engine that keeps cold pricing;
* `prefix_cache_hit_tokens`, the prefix-cache hits observed during each engine's cold sweeps, or `null` when the hit counter is absent;
* the verified attestation response or process identity that binds each fit to its engine generation.

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

The command requires generation-bound saved samples and profile snapshots for every configured engine. It writes a new output pair with refitted cold and warm prefill curves and copied measured decode coefficients, preserving the original pair. Earlier sample files require a fresh sweep against the current engine processes.

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
