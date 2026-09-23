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

Serving and preflight use the same validator. Narwhal requires the declared schema structure, measured decode bounds, and error evidence before accepting a profile row.

A malformed profile aborts the operation with the affected file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be positive
```

The completed profile store must contain exactly the engines configured for the deployment. Missing rows and extra rows both fail validation, with the affected engine IDs included in the error.

When profiling engines independently:

```bash
narwhal-profile --only
```

combine the measured rows into one complete profile store before running preflight or starting the router.

### Profile fields

| Field                                          | JSON type         | Constraint                                                                                                         |
| ---------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `iid`                                          | string            | Nonempty.                                                                                                          |
| `ttft_a`, `ttft_b`, `ttft_c`                   | number            | Nonnegative prefill quadratic coefficients.                                                                        |
| `tpot_slope`                                   | number            | Strictly positive decode interval per resident KV token. A zero slope would price decode capacity as infinite.     |
| `tpot_intercept`                               | number            | Nonnegative zero-contention decode interval.                                                                       |
| `kv_capacity_tokens`                           | integer, optional | Positive when present. When `decode_max_kv_tokens` is also present, physical capacity must be at least that large. |
| `tpot_request_slope`                           | number            | Nonnegative decode interval per active sequence. Defaults to `0`.                                                  |
| `decode_min_requests`, `decode_max_requests`   | integer           | Positive measured concurrency range with `min <= max`.                                                             |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer           | Positive measured resident-KV range with `min <= max`.                                                             |
| `decode_fit_mape`, `decode_cv_mape`            | number            | Nonnegative fit error and leave-one-out cross-validation error.                                                    |

JSON type checking is strict.

Integer fields reject:

```json
true
"96"
1.5
```

Number fields accept integer and floating-point JSON numbers. Every numeric value must be finite. `NaN` and `Infinity` fail before row parsing.

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the smaller of:

1. `decode_max_requests`;
2. the number of requests that fit the KV budget at the priced context length.

The KV budget begins at `decode_max_kv_tokens`. When `kv_capacity_tokens` is present, physical capacity can reduce that budget further.

When Narwhal cannot apply the measured request bound, decode request capacity becomes zero.
