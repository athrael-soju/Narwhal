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

The profiler reads each engine's identity before and after its sweep. With `engine_contract`, it verifies the attestation and saves the digest with the fit; without one, it saves a digest of the process identity from `/version` and `/metrics`. The sample sidecar keeps the full attestation response. If the digest changes between the two readings, the profiler fails the run.

Preflight and router startup compare each configured engine's profile, including every stored variant, with its live generation. Readmission and automatic recovery run the same comparison before an engine returns to placement.

Missing generation evidence or a digest mismatch requires a fresh profile. The error names the engine. Preflight checks the measured decode bounds and fit errors before pricing capacity.

Restart the router to load updated profiles. For fleets with `engine_contract`, [activate the fresh store with router resume](../operate/03-Restart-Engines.md#activate-replacement-profiles). Resume preserves lifecycle holds and drain identities until readmission.

A malformed profile aborts the operation and names the affected file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be positive
```

The profile store's `iid` set must match the configured fleet. Validation reports missing and extra engine IDs. After profiling engines separately with `narwhal-profile --only`, combine the rows with [`narwhal-profile --merge`](../cli/Profile.md#selection-refitting-and-output). Do this before preflight or router startup.

### Profile fields

| Field                                          | JSON type         | Constraint                                                                                                         |
| ---------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `iid`                                          | string            | Nonempty.                                                                                                          |
| `generation_digest`                            | string            | SHA-256 digest of the verified attestation with `engine_contract`. Otherwise, the process identity from `/version` and `/metrics`. |
| `ttft_a`, `ttft_b`, `ttft_c`                   | number            | Nonnegative prefill quadratic coefficients.                                                                        |
| `tpot_slope`                                   | number            | Strictly positive decode interval per resident KV token. A zero slope would price decode capacity as infinite.     |
| `tpot_intercept`                               | number            | Nonnegative zero-contention decode interval.                                                                       |
| `kv_capacity_tokens`                           | integer, optional | Positive when present. At least `decode_max_kv_tokens` when both are present.                                      |
| `tpot_request_slope`                           | number            | Nonnegative decode interval per active sequence. Defaults to `0`.                                                  |
| `decode_min_requests`, `decode_max_requests`   | integer           | Positive measured concurrency range with `min <= max`.                                                             |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer           | Positive measured resident-KV range with `min <= max`.                                                             |
| `decode_fit_mape`, `decode_cv_mape`            | number            | Nonnegative fit error and leave-one-out cross-validation error.                                                    |

Integer fields reject `true`, `"96"`, and `1.5`.

`NaN` and `Infinity` abort profile loading at JSON decoding.

Preflight, router startup, and recovery reject any row without a `generation_digest` and report `profile has no generation evidence`. To fix it, run `narwhal-profile` against the current engine processes and write a fresh store. Keep the `.samples.json` sidecar.

Refitting requires both a `generation_digest` in each saved profile and a `generation_evidence` object in its sample row. Samples missing either need a fresh sweep. The [refit procedure](../measure/01-Profile.md#repair-profiles-produced-by-the-earlier-raw-repeat-fitter) covers it.

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the smaller of:

1. `decode_max_requests`;
2. the number of requests that fit the KV budget at the priced context length.

The KV budget starts at `decode_max_kv_tokens`, and the physical capacity in `kv_capacity_tokens` can reduce it further when present. For positive `context_tokens` and a measured `decode_max_requests`, Narwhal prices capacity from these limits. Capacity is zero when `context_tokens <= 0` or `decode_max_requests` is `null`.
