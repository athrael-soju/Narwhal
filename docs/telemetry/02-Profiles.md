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

With `engine_contract` configured, the profiler reads each engine's process identity and verified attestation before its sweep, repeats the read after the sweep, and saves the attestation digest with the fit. The sample sidecar retains the full attestation response, including the process start, contract fields, and their evidence sources. With `engine_contract` omitted, the same two reads bind the fit to the live `/version` and `/metrics` process identity.

Preflight and router startup read the live generation for every configured engine. A changed digest names the engine and requires a fresh profile before admission; preflight also checks measured decode bounds and error evidence before pricing capacity.

A malformed profile aborts the operation with the affected file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be positive
```

Build one profile store whose `iid` set matches the configured fleet; validation reports any missing or extra engine IDs.

`narwhal-profile --reuse <saved-store> --out <fresh-store>` checks saved rows against the live fleet, measures missing or changed engines, and writes the complete store required by preflight and router startup. A standalone `--only` sweep contains its selected engines.

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

Integer fields reject Boolean, string, and fractional JSON values such as:

```json
true
"96"
1.5
```

`NaN` and `Infinity` abort profile loading during JSON decoding, before the row validator examines engine IDs.

Profile stores written before generation binding retain their schema version and carry rows from an earlier measurement contract. Preflight and router startup reject those rows with `profile has no generation evidence`; run `narwhal-profile` against the current engine processes into a fresh store and keep its `.samples.json` sidecar. Refit accepts saved samples carrying the generation digest and evidence; earlier samples require a fresh sweep to establish process provenance.

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the smaller of:

1. `decode_max_requests`;
2. the number of requests that fit the KV budget at the priced context length.

The KV budget begins at `decode_max_kv_tokens`. When `kv_capacity_tokens` is present, physical capacity can reduce that budget further.

Narwhal prices capacity from the limits above for positive `context_tokens` with a measured `decode_max_requests`. The zero-capacity result covers `context_tokens <= 0` and `decode_max_requests: null`.
