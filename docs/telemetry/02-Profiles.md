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

The profiler reads each engine's identity before and after its sweep. With
`engine_contract`, it verifies attestation and saves the digest with the fit.
The sample sidecar stores the full attestation response, including the process
start, contract fields, and evidence sources. Without `engine_contract`, it
saves a digest of the process identity from `/version` and `/metrics`.

Preflight and startup check every configured engine's profile against its
live generation. Readmission and automatic recovery repeat this check before
returning an engine to placement. These checks cover every stored variant.

Missing generation evidence or a digest mismatch requires a fresh profile;
the error names the engine. Preflight also checks measured decode bounds and
fit errors before pricing capacity.

Restart the router to load updated profiles. For fleets with `engine_contract`,
[activate the fresh store with router resume](../operate/03-Restart-Engines.md#activate-replacement-profiles).
Resume preserves lifecycle holds and drain identities until readmission.

A malformed profile aborts the operation with the affected file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be positive
```

Build one profile store whose `iid` set matches the configured fleet; validation reports any missing or extra engine IDs.

When profiling engines independently with `narwhal-profile --only`, combine their measured rows into one fleet-wide profile store before preflight or router startup.

### Profile fields

| Field                                          | JSON type         | Constraint                                                                                                         |
| ---------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `iid`                                          | string            | Nonempty.                                                                                                          |
| `generation_digest`                            | string            | SHA-256 digest of the verified attestation, or the process identity when the fleet has no declared contract.      |
| `ttft_a`, `ttft_b`, `ttft_c`                   | number            | Nonnegative prefill quadratic coefficients.                                                                        |
| `tpot_slope`                                   | number            | Strictly positive decode interval per resident KV token. A zero slope would price decode capacity as infinite.     |
| `tpot_intercept`                               | number            | Nonnegative zero-contention decode interval.                                                                       |
| `kv_capacity_tokens`                           | integer, optional | Positive when present. When `decode_max_kv_tokens` is also present, physical capacity must be at least that large. |
| `tpot_request_slope`                           | number            | Nonnegative decode interval per active sequence. Defaults to `0`.                                                  |
| `decode_min_requests`, `decode_max_requests`   | integer           | Positive measured concurrency range with `min <= max`.                                                             |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer           | Positive measured resident-KV range with `min <= max`.                                                             |
| `decode_fit_mape`, `decode_cv_mape`            | number            | Nonnegative fit error and leave-one-out cross-validation error.                                                    |
| `cached_ttft_a`, `cached_ttft_b`, `cached_ttft_c`, `cached_ttft_d` | number, optional | Nonnegative warm prefill coefficients: `c + b*S + d*P + a*(2*P*S + S*S)` for `P` cached prefix tokens and `S` uncached suffix tokens. |
| `cached_cv_mape`                               | number, optional  | Nonnegative leave-one-case-out warm prefill error.                                                                 |
| `cached_min_prefix_tokens`, `cached_max_prefix_tokens`, `cached_min_suffix_tokens`, `cached_max_suffix_tokens` | integer, optional | Positive measured warm domain with `min <= max`. |

The `cached_` fields appear as a complete set or stay absent. When the fields are absent, a request with a cached prefix gets cold pricing for its full input. The same applies outside their measured domain.

Integer fields reject Boolean, string, and fractional JSON values such as:

```json
true
"96"
1.5
```

`NaN` and `Infinity` abort profile loading during JSON decoding, before the row validator examines engine IDs.

Preflight, router startup, and recovery reject rows without `generation_digest` with `profile has no generation evidence`, even when the store's schema version is current.

Run `narwhal-profile` against the current engine processes into a fresh store and keep its `.samples.json` sidecar.

Refitting requires both `generation_digest` in each saved profile and a `generation_evidence` object in its sample row. Samples missing either require a fresh sweep; see the [refit procedure](../measure/01-Profile.md#repair-profiles-produced-by-the-earlier-raw-repeat-fitter).

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the smaller of:

1. `decode_max_requests`;
2. the number of requests that fit the KV budget at the priced context length.

The KV budget begins at `decode_max_kv_tokens`. When `kv_capacity_tokens` is present, physical capacity can reduce that budget further.

Narwhal prices capacity from the limits above for positive `context_tokens` with a measured `decode_max_requests`. The zero-capacity result covers `context_tokens <= 0` and `decode_max_requests: null`.
