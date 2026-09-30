# Engine profiles and capacity

## Validate the engine cost model

`narwhal-profile` measures each engine and writes one cost-model row per engine to `profiles.path`. The file has this shape, where `meta` holds the build metadata the journal records:

```json
{
  "schema": "narwhal.profiles",
  "schema_version": 1,
  "meta": {"package": "narwhal-inference", "version": "0.1.0", "git": "<commit>", "source": "sha256:..."},
  "profiles": []
}
```

Unknown top-level fields are rejected.

The profiler reads each engine's identity before and after its sweep. If the fleet declares an `engine_contract`, it verifies the engine's attestation and stores the resulting digest with the fit. The full attestation response, with the process start, contract fields, and evidence sources, goes into the sample sidecar. Without a contract, the profiler stores a digest of the process identity reported by `/version` and `/metrics`.

The digest ties a profile to a running engine. Preflight and router startup compare every stored profile variant of every configured engine against the engine's live generation. Readmission and automatic recovery run the same check before returning an engine to placement. If generation evidence is missing or the digest does not match, the check fails and the error names the engine. Re-profile the engine. A row without `generation_digest` fails with `profile has no generation evidence`, even in a store that has the current schema version.

Preflight then validates the decode bounds and fit-error fields before pricing capacity. A malformed profile stops the operation, and the error names the file, engine, and field:

```text
profiles.json: profile n4: tpot_slope must be nonnegative
```

### Producing a fresh profile

Run `narwhal-profile` against the live engines and write the results to a new store. Keep the `.samples.json` sidecar, which refitting needs. Refitting requires a `generation_digest` in each profile and a `generation_evidence` object in the matching sample row. If either is missing, run another sweep. See the [refit procedure](../measure/01-Profile.md#refitting-profiles-from-older-releases).

The set of engine IDs in the store must equal the configured engine IDs. An engine can have several rows. Validation reports missing and extra engine IDs. If you profiled engines separately with `narwhal-profile --only`, combine the stores with `narwhal-profile --merge` before running preflight or starting the router.

The router loads profiles at startup, so restart it to pick up new ones. For fleets with an `engine_contract`, [activate the new store through router resume](../operate/03-Restart-Engines.md#activate-replacement-profiles). Resume keeps lifecycle holds and drain identities in place until the engines are readmitted.

### Profile fields

| Field                                                   | JSON type         | Constraint                                                                                                                                                          |
| ------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `iid`                                                   | string            | Must not be empty.                                                                                                                                                     |
| `generation_digest`                                     | string            | SHA-256 digest of the verified attestation, or of the process identity if the fleet declares no contract. Written as `sha256:` followed by 64 lowercase hex digits. |
| `ttft_a`, `ttft_b`, `ttft_c`                            | number            | Prefill quadratic coefficients. Zero or greater.                                                                                                                    |
| `tpot_slope`                                            | number            | Decode interval added per resident KV token. Zero or greater. A zero slope is accepted only alongside measured decode bounds, which keep decode capacity finite.    |
| `tpot_intercept`                                        | number            | Decode interval with no contention. Zero or greater.                                                                                                                |
| `kv_capacity_tokens`                                    | integer, optional | Must be positive if present. If `decode_max_kv_tokens` is also set, this must be at least as large.                                                                       |
| `tpot_request_slope`                                    | number, optional  | Decode interval added per active sequence. Zero or greater, and `0` if omitted.                                                                                     |
| `decode_min_requests`, `decode_max_requests`            | integer           | The measured concurrency range. Positive, with the minimum no larger than the maximum.                                                                              |
| `decode_min_kv_tokens`, `decode_max_kv_tokens`          | integer           | The measured resident-KV range. Positive, with the minimum no larger than the maximum.                                                                              |
| `decode_fit_mape`, `decode_cv_mape`                     | number            | Fit error and leave-one-out cross-validation error, as mean absolute percentage error (MAPE). Zero or greater.                                                                                                |
| `prefill_min_tokens`, `prefill_max_tokens`              | integer, optional | The measured prompt-length range. Positive, with the minimum no larger than the maximum.                                                                            |
| `decode_min_output_tokens`, `decode_max_output_tokens`  | integer, optional | The measured output-length range. Positive, with the minimum no larger than the maximum.                                                                            |
| `colocated_group`                                       | string, optional  | The shared-device group of a role-mix variant. Must not be empty. Setting it requires every other `colocated_*` field, and leaving it out forbids them.                |
| `colocated_target_role`                                 | string, optional  | The profiled engine's role during the sweep: `prefill` or `decode`.                                                                                                 |
| `colocated_prefill_engines`, `colocated_decode_engines` | integer, optional | The group's prefill and decode engine counts during the sweep. Positive.                                                                                            |
| `colocated_prefill_rps`, `colocated_decode_rps`         | number, optional  | Neighbor prefill and decode requests completed per second during the sweep. Zero or greater.                                                                        |

Unknown profile fields are rejected. A shared-device engine has one row per measured role mix, distinguished by its `colocated_*` fields.

Integer fields must be JSON integers. `true`, `"96"`, and `1.5` are rejected. `NaN` and `Infinity` are rejected at JSON decode, before engine IDs are checked.

### Decode capacity derived from the profile

For each fitted engine, Narwhal caps decode concurrency at the lower of `decode_max_requests` and the number of requests whose KV fits the budget at the priced context length, with a floor of one. The KV budget is `decode_max_kv_tokens`. `kv_capacity_tokens` is validated to be at least as large, so the budget is always `decode_max_kv_tokens`.

Capacity is zero when `context_tokens` is zero or negative, or when `decode_max_requests` is `null`.
