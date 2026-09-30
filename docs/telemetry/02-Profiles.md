# Engine profiles and capacity

## Validate the engine cost model

`narwhal-profile` writes one measured cost-model row per engine into `profiles.path`.

The profile file declares:

```json
{
  "schema": "narwhal.profiles",
  "schema_version": 1,
  "profiles": []
}
```

The profiler reads each engine's identity at the start and end of its sweep and saves a generation digest with the fit.

| Fleet                  | Saved digest                                                                                   |
| ---------------------- | ---------------------------------------------------------------------------------------------- |
| With `engine_contract` | Digest of the verified attestation. The sample sidecar keeps the full attestation response.    |
| Otherwise              | Digest of the process identity from `/version` and `/metrics`.                                 |

The profiler fails the run when the digest changes between the two readings.

Preflight, router startup, readmission, and automatic recovery compare every stored profile variant of an engine with its live generation. Missing generation evidence or a digest mismatch requires a fresh profile. The error names the engine. Preflight checks the measured decode bounds and fit errors before pricing capacity.

| Fleet                  | Load updated profiles                                                                                                     |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| With `engine_contract` | [Activate the fresh store with router resume](../operate/03-Restart-Engines.md#activate-replacement-profiles).           |
| Otherwise              | Restart the router.                                                                                                       |

A malformed profile aborts the operation and names the affected file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be positive
```

The profile store's `iid` set must match the configured fleet. Validation reports missing and extra engine IDs. Combine rows from separate `narwhal-profile --only` runs with [`narwhal-profile --merge`](../cli/Profile.md#selection-refitting-and-output) before preflight or router startup.

### Profile fields

| Field                                          | JSON type         | Constraint                                                                                                         |
| ---------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `iid`                                          | string            | Nonempty.                                                                                                          |
| `generation_digest`                            | string            | SHA-256 digest of the verified attestation with `engine_contract`. Otherwise, the process identity from `/version` and `/metrics`. |
| `ttft_a`, `ttft_b`, `ttft_c`                   | number            | Nonnegative prefill quadratic coefficients.                                                                        |
| `tpot_slope`                                   | number            | Strictly positive decode interval per resident KV token.                                                           |
| `tpot_intercept`                               | number            | Nonnegative zero-contention decode interval.                                                                       |
| `kv_capacity_tokens`                           | integer, optional | Positive when present. At least `decode_max_kv_tokens` when both are present.                                      |
| `tpot_request_slope`                           | number            | Nonnegative decode interval per active sequence. Defaults to `0`.                                                  |
| `decode_min_requests`, `decode_max_requests`   | integer           | Positive measured concurrency range with `min <= max`.                                                             |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer           | Positive measured resident-KV range with `min <= max`.                                                             |
| `decode_fit_mape`, `decode_cv_mape`            | number            | Nonnegative fit error and leave-one-out cross-validation error.                                                    |

Integer fields reject `true`, `"96"`, and `1.5`.

`NaN` and `Infinity` abort profile loading at JSON decoding.

Preflight, router startup, and recovery report `profile has no generation evidence` for a row missing `generation_digest`. To fix it:

1. Run `narwhal-profile` against the current engine processes and write a fresh store.
2. Keep the `.samples.json` sidecar.

The [refit procedure](../measure/01-Profile.md#repair-profiles-produced-by-the-earlier-raw-repeat-fitter) requires a `generation_digest` in each saved profile and a `generation_evidence` object in its sample row. Samples missing either need a fresh sweep.

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the smaller of:

1. `decode_max_requests`;
2. the number of requests that fit the KV budget at the priced context length.

The KV budget is `decode_max_kv_tokens`, or the smaller of `decode_max_kv_tokens` and `kv_capacity_tokens` when both are present.

| Condition                                                        | Decode capacity                  |
| ---------------------------------------------------------------- | -------------------------------- |
| Positive `context_tokens` and a measured `decode_max_requests`   | Priced from the limits above.    |
| `context_tokens <= 0` or `decode_max_requests` is `null`         | Zero.                            |
