---
description: Machine-readable JSON results and exit codes for automating Narwhal commands.
---

# Command results for automation

| Command | Machine-readable outcome |
| --- | --- |
| `narwhal-check`, `narwhal-profile`, `narwhal-engine`, `narwhal config`, `narwhal diagnostics`, `narwhal dev` | `--format json`, before or after operation arguments |
| `narwhal-serve`, `narwhal-attest` | Logs, exit status, HTTP endpoints, and persisted artifacts |

Streams with `--format json`:

| Stream | Content |
| --- | --- |
| stdout | One `narwhal.command-result` version 1 object for every outcome |
| stderr | Live progress and diagnostics |
| stderr | Subprocess stdout and stderr, replayed at completion |
| stderr | Help text for `--format json --help` |

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
| `success` | 0 | The requested operation completed. | |
| `failed_gate` | 1 | A preflight, evidence, or health gate rejected the operation. | `gate_failed`, `evidence_gate_failed`, `engine_unhealthy`, `failed_gate` |
| `invalid_input` | 2 | Arguments, configuration, or required inputs failed validation. | `invalid_arguments`, `invalid_input`, `input_missing`, `output_exists`, `engine_selection_empty`, `runtime_package_missing` |
| `degraded` | 3 | The operation completed with skipped preflight gates, a degraded development instance, or a partial diagnostic bundle. | `gates_skipped`, `instance_degraded`, `collection_partial` |
| `error` | 4 | An operational failure or stage deadline interrupted completion. | `permission_denied`, `engine_http_error`, `operation_failed`, `stage_timeout` |
| `interrupted` | 130 | The command received cancellation. | `stage_cancelled`, `interrupted` |

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

`data` contents:

| Operation | `data` |
| --- | --- |
| `narwhal dev` subcommands | `instance` and the returned lifecycle state |
| `narwhal config` actions | The `narwhal.effective-config` document |
| `narwhal diagnostics collect` | `bundle`, `manifest`, `collection_status`, and `sources` |
| `narwhal-engine` actions | `runs`, the launch directories |
| `narwhal-profile` | `engines`, the selected engine IDs |
| `narwhal-check` preflight | `failed`, `skipped`, `warnings`, and `pairs` |
| `narwhal-check --verify-evidence` | `failed` |
| `narwhal-check --print-example-config` | The packaged example fleet configuration |
| `narwhal-check --print-contract-versions` | The versioned interface registry |

## Redaction

JSON results and diagnostics redact:

- credential environment values
- HTTP URL credentials
- bearer values
- credential query parameters

## Validate a result

```python
import json
from narwhal.contracts import COMMAND_RESULT, validate_document

with open("check-result.json") as source:
    result = json.load(source)
validate_document(result, COMMAND_RESULT)
```

## Schema versions

`narwhal-check --print-contract-versions` lists the result schema with the persisted interfaces.

| Change | Schema version |
| --- | --- |
| New `data` field or error code | Unchanged |
| Incompatible envelope change | New version |
