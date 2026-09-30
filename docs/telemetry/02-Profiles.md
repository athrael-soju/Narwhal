# Engine profiles and capacity

## Validate the engine cost model

`narwhal-profile` measures each engine and writes one cost-model row per engine to `profiles.path`. The file wraps those rows like this, with the same build metadata the journal records in `meta`:

```json
{
  "schema": "narwhal.profiles",
  "schema_version": 1,
  "meta": {"package": "narwhal-inference", "version": "0.1.0", "git": "<commit>", "source": "sha256:..."},
  "profiles": []
}
```

Any other top-level field is rejected.

The profiler reads each engine's identity both before and after its sweep. If the fleet declares an `engine_contract`, it verifies the engine's attestation and stores the resulting digest with the fit. The full attestation response, with the process start, contract fields, and evidence sources, goes into the sample sidecar. Without a contract, the profiler stores a digest of the process identity reported by `/version` and `/metrics` instead.

That digest is what ties a profile to a running engine. Preflight and router startup compare every configured engine's profile, including every stored variant, against the engine's live generation. Readmission and automatic recovery run the same check before they put an engine back into placement. If generation evidence is missing or the digest doesn't match, you need a fresh profile, and the error will tell you which engine. A row with no `generation_digest` at all fails with `profile has no generation evidence`, even in a store that has the current schema version.

Preflight goes on to check the measured decode bounds and fit errors before it prices any capacity. A malformed profile stops the operation, and the error names the file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be nonnegative
```

### Producing a fresh profile

Run `narwhal-profile` against the engines as they're running now, write the results to a new store, and hang on to the `.samples.json` sidecar. You'll need it later. Refitting requires both the `generation_digest` in each profile and a `generation_evidence` object in the matching sample row, and if either is missing the only fix is another sweep. The [refit procedure](../measure/01-Profile.md#refitting-profiles-from-older-releases) has the details.

The store has to cover exactly the configured fleet, with at least one row for every configured engine. Validation reports any engine IDs that are missing or extra. If you profiled engines one at a time with `narwhal-profile --only`, merge their stores into one with `narwhal-profile --merge` before you run preflight or start the router.

The router loads profiles when it starts, so restart it to pick up new ones. For fleets with an `engine_contract`, [activate the new store through router resume](../operate/03-Restart-Engines.md#activate-replacement-profiles). Resume keeps lifecycle holds and drain identities in place until the engines are readmitted.

### Profile fields

| Field                                                   | JSON type         | Constraint                                                                                                                                                          |
| ------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `iid`                                                   | string            | Can't be empty.                                                                                                                                                     |
| `generation_digest`                                     | string            | SHA-256 digest of the verified attestation, or of the process identity if the fleet declares no contract. Written as `sha256:` followed by 64 lowercase hex digits. |
| `ttft_a`, `ttft_b`, `ttft_c`                            | number            | Prefill quadratic coefficients. Zero or greater.                                                                                                                    |
| `tpot_slope`                                            | number            | Decode interval added per resident KV token. Zero or greater. A zero slope is accepted only alongside measured decode bounds, which keep decode capacity finite.    |
| `tpot_intercept`                                        | number            | Decode interval with no contention. Zero or greater.                                                                                                                |
| `kv_capacity_tokens`                                    | integer, optional | Positive if present. If `decode_max_kv_tokens` is also set, this has to be at least as large.                                                                       |
| `tpot_request_slope`                                    | number, optional  | Decode interval added per active sequence. Zero or greater, and `0` if omitted.                                                                                     |
| `decode_min_requests`, `decode_max_requests`            | integer           | The measured concurrency range. Positive, with the minimum no larger than the maximum.                                                                              |
| `decode_min_kv_tokens`, `decode_max_kv_tokens`          | integer           | The measured resident-KV range. Positive, with the minimum no larger than the maximum.                                                                              |
| `decode_fit_mape`, `decode_cv_mape`                     | number            | Fit error and leave-one-out cross-validation error. Zero or greater.                                                                                                |
| `prefill_min_tokens`, `prefill_max_tokens`              | integer, optional | The measured prompt-length range. Positive, with the minimum no larger than the maximum.                                                                            |
| `decode_min_output_tokens`, `decode_max_output_tokens`  | integer, optional | The measured output-length range. Positive, with the minimum no larger than the maximum.                                                                            |
| `colocated_group`                                       | string, optional  | The shared-device group of a role-mix variant. Can't be empty. Setting it requires every other `colocated_*` field, and leaving it out forbids them.                |
| `colocated_target_role`                                 | string, optional  | The profiled engine's role during the sweep: `prefill` or `decode`.                                                                                                 |
| `colocated_prefill_engines`, `colocated_decode_engines` | integer, optional | The group's prefill and decode engine counts during the sweep. Positive.                                                                                            |
| `colocated_prefill_rps`, `colocated_decode_rps`         | number, optional  | Neighbor prefill and decode requests completed per second during the sweep. Zero or greater.                                                                        |

Unknown fields are rejected. A shared-device engine can have one row per measured role mix, told apart by its `colocated_*` fields.

Integer fields are strict about type, so `true`, `"96"`, and `1.5` are all rejected. `NaN` and `Infinity` fail even earlier, while the JSON is being decoded, before any engine ID gets checked.

### Decode capacity derived from the profile

For each fitted engine, Narwhal caps decode concurrency at `decode_max_requests` or at the number of requests whose KV fits the budget at the priced context length, whichever is lower, but never below one. The KV budget is `decode_max_kv_tokens`. `kv_capacity_tokens` never lowers it, because validation already requires it to be at least as large.

Capacity comes out as zero when `context_tokens` is zero or negative, or when `decode_max_requests` is `null`.
