---
description: Machine-readable JSON results and exit codes for automating Narwhal commands.
---

# Command results for automation

`narwhal-check`, `narwhal-profile`, `narwhal-engine`, `narwhal config`, `narwhal diagnostics`, `narwhal dev`, and `narwhal deploy plan`, `run`, `status`, `down`, `discover`, `access`, `package`, `install`, `transfer-measure`, `cache-groups` and `fabric` accept `--format json` before or after operation arguments. `narwhal-serve`, `narwhal-attest`, and `narwhal deploy shell` and `tunnel` do not.

With `--format json`, stdout carries one `narwhal.command-result` version 1 object for every outcome. stderr carries live progress and diagnostics, subprocess stdout and stderr replayed at completion, and the help text for `--format json --help`.

Examples:

```bash
narwhal-check --fleet runs/fleet.json --format json >check-result.json
narwhal-profile --fleet runs/fleet.json --format json >profile-result.json
narwhal-engine check --run runs/engine-1 --format json >engine-result.json
narwhal dev status --instance runs/dev --format json >status-result.json
```

## Status and exit codes

Text mode uses the [text-mode exit codes](CLI-Reference.md#text-mode-exit-codes).

| Status | Exit code | Operation state | Error codes |
| --- | :--: | --- | --- |
| `success` | 0 | The requested operation completed | |
| `failed_gate` | 1 | A preflight, evidence, health, host inspection, engine probe or deployment stage gate rejected the operation, or `narwhal deploy status` found the last run stopped at a failed stage | `gate_failed`, `evidence_gate_failed`, `engine_unhealthy`, `failed_gate` |
| `invalid_input` | 2 | Arguments, configuration, or required inputs failed validation | `invalid_arguments`, `invalid_input`, `input_missing`, `output_exists`, `engine_selection_empty`, `runtime_package_missing` |
| `degraded` | 3 | Completed with skipped preflight gates, a degraded development instance, a deployment whose recorded process or route no longer holds, or a partial diagnostic bundle | `gates_skipped`, `instance_degraded`, `collection_partial` |
| `error` | 4 | An operational failure or stage deadline interrupted completion | `permission_denied`, `engine_http_error`, `operation_failed`, `stage_timeout`, `apply_conflict`, `follow_timeout` |
| `interrupted` | 130 | The command received cancellation | `stage_cancelled`, `interrupted` |

Branch on `status`, the error `code`, and the stage recovery data in the error `context`.

## Result fields

| Field | Contract |
| --- | --- |
| `schema`, `schema_version` | `narwhal.command-result`, `1` |
| `command`, `operation` | Installed command and selected operation, such as `narwhal` and `dev status` |
| `status`, `exit_code` | Status and matching exit code from the status table |
| `data` | Output of the selected operation |
| `artifacts` | References with `kind`, absolute `path`, and a `state` of `created`, `updated`, `existing`, or `missing` |
| `errors` | Entries with `code`, `message`, `command`, and optional `stage`, `engine`, `field`, and `context` |

`data` holds different content for each operation:

| Operation | `data` |
| --- | --- |
| `narwhal dev` subcommands | `instance` and the returned lifecycle state |
| `narwhal deploy plan` | `run`, the run directory, `stages`, each with its `stage` instance, whether it `runs` and its `reasons`, and for a skipped stage the live facts `plan` could not observe without changing the target, as `unobserved`, `hosts`, the systemd problems of each host that has any, and `notes`, what the plan assumed, such as a restarted unit whose check only `run` performs |
| `narwhal deploy run` | `run` and `run_id`, and `ran`, the stage instances that ran, or on failure `failed_stage` or `cancelled_stage`. The failure's error `context` holds `recovery`, the URL of its gate section. With `--trial`, `trial` holds the [trial result](deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial) of the last trial attempt: `passed`, `rates`, each with its `rate_rps`, `offered`, `within_candidate_limits`, `attainment`, `required_attainment`, `client_schedule_valid`, `ttft_s` and `tpot_s` percentiles, `passed`, `reconciled` and `control`, the `router`, `epoch` and `holder` that held control `before` and `after` the rate, `series`, each with its `series` and `passed`, `ring`, and the `failure` of a failed trial |
| `narwhal deploy status` | `run`, `run_id`, `locked`, whether another runner held the run so nothing was recorded, `stages`, each with its `stage`, `status`, `attempt`, `evidence` directory, and a failed stage's `failure` and `recovery`, and `live`, each recorded process or route with its `key`, `stage`, `state` and `detail` |
| `narwhal deploy down` | `run`, and `processes`, each with its `name`, `host` and whether it was `stopped` |
| `narwhal deploy plan`, `run`, `status`, `down` with `--target kubernetes` | The fields above, and `fleet`, the `NarwhalFleet`'s `namespace`, `name`, `uid`, `generation`, `observed_generation`, `stage`, `engines`, `serving` and its `Ready`, `Reconciling`, `Stalled` and `Stopped` `conditions`. `plan` and `run` add `managers`, the field managers other than `narwhal-cli` that own the fleet's spec, and `run` adds `applied`. `run`, `status` and `down` add `evidence`, what the export Jobs copied: `files`, `jobs`, `too_large`, `unnamed` and `complete`, or the `error` that stopped them. `run` holds no `ran`, and on a stall `failed_stage`. `down` adds `stopped` and `deleted`. An `apply_conflict` error's `context.fields` lists each `field` and `manager`, and `follow_timeout` means the fleet was neither ready nor stalled, or not stopped, within `--timeout` |
| `narwhal deploy discover` | `out`, the discovery directory, and `deployment_env`, the derived variables file |
| `narwhal deploy access` | `logs`, the log directory, and `hosts`, each with its `id` and `roles` |
| `narwhal deploy package`, `narwhal deploy install` | `run`, the package directory, and `hosts`, the packaged or installed host IDs |
| `narwhal deploy transfer-measure` | `out`, the measurement directory, and the contents of its `transfer-measure.json`: `prompt_tokens` and `engines` |
| `narwhal deploy cache-groups` | `out`, the evidence directory, and `groups`, each with its `id`, `representative` and `roles` |
| `narwhal deploy fabric` | `out`, the evidence directory, and on success the contents of its `fabric.json`: `inputs`, `budgets` and `edges` |
| `narwhal config` actions | The `narwhal.effective-config` document |
| `narwhal diagnostics collect` | `bundle`, `manifest`, `collection_status`, and the `sources` count |
| `narwhal-engine` actions | `runs`, the launch directories |
| `narwhal-engine inspect-host` | `out`, the evidence directory, and on success `role` and `passed` |
| `narwhal-engine probe` | `out`, the evidence directory, `runs`, the launch directory, and on success `role` and `passed` |
| `narwhal-profile` | `engines`, the selected engine IDs |
| `narwhal-check` preflight | `failed`, `skipped`, `warnings`, `pairs`, and [`first_token_calibration`](cli/Check.md#the-calibration-gate) |
| `narwhal-check --verify-evidence` | `failed` |
| `narwhal-check --print-example-config` | The packaged example fleet configuration |
| `narwhal-check --print-contract-versions` | The versioned interface registry |

## Redaction

JSON results and diagnostics redact:

- credential environment values
- HTTP URL credentials
- bearer values
- credential query parameters

## Validating a result

```python
import json
from narwhal.contracts import COMMAND_RESULT, validate_document

with open("check-result.json") as source:
    result = json.load(source)
validate_document(result, COMMAND_RESULT)
```

## Schema versions

`narwhal-check --print-contract-versions` lists the result schema with the persisted interfaces, each with the version it writes and the versions it reads, as [Interface versions and compatibility](telemetry/05-Compatibility.md) describes. The deployment run record is written as version 3 and read as version 2 or 3.

A new `data` field or error code keeps the current schema version. An incompatible envelope change gets a new version.
