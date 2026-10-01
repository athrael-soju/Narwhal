---
description: Validate the measured per-engine cost model that narwhal-profile writes.
---

# Engine profiles and capacity

## Validating the engine cost model

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

The profiler fails the run when an engine's process digest changes between the start and end of its sweep.

| Fleet | Saved digest | `.samples.json` sidecar keeps |
| --- | --- | --- |
| With `engine_contract` and a launch digest in the attestation | Attested launch digest. | The full attestation response. |
| Other fleets with `engine_contract` | Per-process attestation digest. | The full attestation response. |
| Otherwise | Digest of the process identity from `/version` and `/metrics`. | The process identity. |

The [attested launch digest](../configuration/01-Fleet-Schema.md#33-attestation) covers:

- the contract fields
- engine arguments with the values of `--host`, `--port`, `--served-model-name`, and `--kv-events-config` removed
- image
- packages
- model configuration and revision
- launcher, launch-record, and cache-capture hook hashes
- the image check's `ucx_version` and `peer_release` values

For a stored profile with missing generation evidence or a digest that differs from the engine's live generation:

- Preflight, router startup, readmission, and automatic recovery require a fresh profile.
- The error names the engine.

Preflight checks the measured decode bounds and fit errors.

| Fleet | Load updated profiles |
| --- | --- |
| With `engine_contract` | [Activate the fresh store with router resume](../operate/03-Restart-Engines.md#activating-replacement-profiles). |
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
| `generation_digest` | string | `sha256:` digest of the attested launch, the verified attestation, or the process identity. |
| `ttft_a`, `ttft_b`, `ttft_c` | number | Nonnegative prefill quadratic coefficients. |
| `ttft_block_tokens`, `ttft_split` | integer and number, optional | Positive cache block size and nonnegative added prefill time for a prompt that ends inside a block past the first, both set or both `null`. |
| `tpot_slope` | number | Nonnegative decode interval per resident KV token. |
| `tpot_intercept` | number | Nonnegative zero-contention decode interval. |
| `tpot_request_slope` | number, optional | Nonnegative decode interval per active sequence, default `0`. |
| `kv_capacity_tokens` | integer, optional | Positive, and at least `decode_max_kv_tokens`. |
| `decode_min_requests`, `decode_max_requests` | integer | Positive measured concurrency range with `min <= max`. |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer | Positive measured resident-KV range with `min <= max`. |
| `decode_fit_mape`, `decode_cv_mape` | number | Nonnegative fit error and leave-one-out cross-validation error. |
| `cached_ttft_a`, `cached_ttft_b`, `cached_ttft_c`, `cached_ttft_d` | number, optional | Nonnegative coefficients of the [warm prefill fit](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix). |
| `cached_cv_mape` | number, optional | Nonnegative leave-one-case-out warm prefill error. |
| `cached_min_prefix_tokens`, `cached_max_prefix_tokens` | integer, optional | Positive measured cached-prefix range with `min <= max`. |
| `cached_min_suffix_tokens`, `cached_max_suffix_tokens` | integer, optional | Positive measured uncached-suffix range with `min <= max`. |
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
| A partial set of `cached_` fields | Profile loading aborts. |

The router prices a request with a cached prefix from the `cached_` fields under the [prefix-cache pricing rules](../configuration/02-Serving-and-Role-Control.md#51-prefix-cache-pricing).

For the `profile has no generation evidence` error from preflight, router startup, or recovery on a row missing `generation_digest`:

1. Write a fresh store with `narwhal-profile` against the current engine processes.
2. Keep the `.samples.json` sidecar.

The [refit procedure](../measure/01-Profile.md#refitting-saved-profile-samples) requires:

- a `generation_digest` in each saved profile
- a `generation_evidence` object in its sample row

Samples missing either need a fresh sweep.

### Decode capacity derived from the profile

Narwhal caps decode concurrency for each fitted engine at the priced context length:

| Condition | Decode request limit |
| --- | --- |
| `context_tokens <= 0` or `decode_max_requests` is `null` | Zero. |
| Otherwise | The smallest of `decode_max_requests`, a positive `serving.decode_concurrency`, and the KV budget divided by `context_tokens`, at least `1`. |

| Profile | KV budget |
| --- | --- |
| With `kv_capacity_tokens` | The smaller of `decode_max_kv_tokens` and `kv_capacity_tokens`. |
| Otherwise | `decode_max_kv_tokens`. |

Token interval floors for decode request capacity:

| Input | Floor |
| --- | --- |
| Batch requests | `decode_min_requests` |
| Resident KV tokens | `decode_min_kv_tokens` |
