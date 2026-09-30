# Engine profiles and capacity

## Validate the engine cost model

`narwhal-profile` writes one measured cost-model row per engine into `profiles.path`.

Profile store document:

```json
{
  "schema": "narwhal.profiles",
  "schema_version": 1,
  "meta": {"package": "narwhal-inference", "version": "0.3.1", "git": "<commit>", "source": "sha256:..."},
  "profiles": []
}
```

The profiler fails the run when an engine's generation digest changes between the start and end of its sweep.

| Fleet | Saved digest | `.samples.json` sidecar keeps |
| --- | --- | --- |
| With `engine_contract` | Digest of the verified attestation. | The full attestation response. |
| Otherwise | Digest of the process identity from `/version` and `/metrics`. | The process identity. |

For a stored profile with missing generation evidence or a digest that differs from the engine's live generation:

- Preflight, router startup, readmission, and automatic recovery require a fresh profile.
- The error names the engine.

Preflight checks the measured decode bounds and fit errors.

| Fleet | Load updated profiles |
| --- | --- |
| With `engine_contract` | [Activate the fresh store with router resume](../operate/03-Restart-Engines.md#activate-replacement-profiles). |
| Otherwise | Restart the router. |

A malformed profile aborts the operation and names the affected file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be nonnegative
```

Before preflight or router startup:

1. Combine rows from separate `narwhal-profile --only` runs with [`narwhal-profile --merge`](../cli/Profile.md#selection-refitting-and-output).
2. Confirm that the profile store's `iid` set matches the configured fleet.

### Profile fields

| Field | JSON type | Constraint |
| --- | --- | --- |
| `iid` | string | Nonempty. |
| `generation_digest` | string | `sha256:` digest of the verified attestation or the process identity. |
| `ttft_a`, `ttft_b`, `ttft_c` | number | Nonnegative prefill quadratic coefficients. |
| `tpot_slope` | number | Nonnegative decode interval per resident KV token. |
| `tpot_intercept` | number | Nonnegative zero-contention decode interval. |
| `tpot_request_slope` | number, optional | Nonnegative decode interval per active sequence, default `0`. |
| `kv_capacity_tokens` | integer, optional | Positive, and at least `decode_max_kv_tokens`. |
| `decode_min_requests`, `decode_max_requests` | integer | Positive measured concurrency range with `min <= max`. |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer | Positive measured resident-KV range with `min <= max`. |
| `decode_fit_mape`, `decode_cv_mape` | number | Nonnegative fit error and leave-one-out cross-validation error. |
| `prefill_min_tokens`, `prefill_max_tokens` | integer, optional | Positive measured prompt-length range with `min <= max`. |
| `decode_min_output_tokens`, `decode_max_output_tokens` | integer, optional | Positive measured output-length range with `min <= max`. |
| `colocated_group` | string, optional | Nonempty shared-device group of a colocated role-mix variant. |
| `colocated_target_role` | string | `prefill` or `decode`, required with `colocated_group`. |
| `colocated_prefill_engines`, `colocated_decode_engines` | integer | Positive measured role mix totalling at least two engines, required with `colocated_group`. |
| `colocated_prefill_rps`, `colocated_decode_rps` | number | Nonnegative neighbour load, required with `colocated_group`. |

| Input | Result |
| --- | --- |
| `true`, `"96"`, or `1.5` in an integer field | Rejected. |
| `NaN` or `Infinity` | Profile loading aborts at JSON decoding. |
| A field outside this table | Profile loading aborts. |

For the `profile has no generation evidence` error from preflight, router startup, or recovery on a row missing `generation_digest`:

1. Write a fresh store with `narwhal-profile` against the current engine processes.
2. Keep the `.samples.json` sidecar.

The [refit procedure](../measure/01-Profile.md#repair-profiles-produced-by-the-earlier-raw-repeat-fitter) requires:

- a `generation_digest` in each saved profile
- a `generation_evidence` object in its sample row

Samples missing either need a fresh sweep.

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the priced context length:

| Condition | Decode request limit |
| --- | --- |
| `context_tokens <= 0` or `decode_max_requests` is `null` | Zero. |
| Otherwise | The smaller of `decode_max_requests` and the KV budget divided by `context_tokens`, at least `1`. |

| Profile | KV budget |
| --- | --- |
| With `kv_capacity_tokens` | The smaller of `decode_max_kv_tokens` and `kv_capacity_tokens`. |
| Otherwise | `decode_max_kv_tokens`. |
