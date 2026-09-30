# Command results for automation

`narwhal-check`, `narwhal-profile`, `narwhal-engine`, `narwhal config`, `narwhal diagnostics`, and `narwhal dev` accept `--format json` before or after operation arguments.

| Stream | Content                                                                                    |
| ------ | ------------------------------------------------------------------------------------------ |
| stdout | One `narwhal.command-result` version 1 object for every outcome.                            |
| stderr | Live progress and diagnostics.                                                             |
| stderr | Subprocess stdout and stderr, replayed at completion.                                      |
| stderr | Help text for `--format json --help`.                                                      |

Examples:

```bash
narwhal-check --fleet runs/fleet.json --format json >check-result.json
narwhal-profile --fleet runs/fleet.json --format json >profile-result.json
narwhal-engine check --run runs/engine-1 --format json >engine-result.json
narwhal dev status --instance runs/dev --format json >status-result.json
```

`narwhal-serve` and `narwhal-attest` report through logs, exit status, HTTP endpoints, and persisted artifacts.

Exit codes with `--format json` (text mode: [text-mode exit codes](CLI-Reference.md#text-mode-exit-codes)):

| Status          | Exit code | Operation state                                                                                                        |
| --------------- | --------: | ---------------------------------------------------------------------------------------------------------------------- |
| `success`       |         0 | The requested operation completed.                                                                                     |
| `failed_gate`   |         1 | A preflight, evidence, or health gate rejected the operation.                                                          |
| `invalid_input` |         2 | Arguments, configuration, or required inputs failed validation.                                                        |
| `degraded`      |         3 | The operation completed with skipped preflight gates, a degraded development instance, or a partial diagnostic bundle. |
| `error`         |         4 | An operational failure or stage deadline interrupted completion.                                                       |
| `interrupted`   |       130 | The command received cancellation.                                                                                     |

Result fields:

| Field                      | Contract                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `schema`, `schema_version` | `narwhal.command-result`, `1`.                                                                                                 |
| `command`, `operation`     | Installed command and selected operation, such as `narwhal` and `dev status`.                                                  |
| `status`, `exit_code`      | Status and matching exit code from the status table.                                                                           |
| `data`                     | Output of the selected operation.                                                                                              |
| `artifacts`                | References with `kind`, absolute `path`, and a `state` of `created`, `updated`, `existing`, or `missing`.                      |
| `errors`                   | Entries with `code`, `message`, `command`, and optional `stage`, `engine`, `field`, and `context`.                             |

`data` contents:

| Source             | Content                                                       |
| ------------------ | ------------------------------------------------------------- |
| Lifecycle commands | Lifecycle state.                                              |
| Preflight          | Failures, skips, and pairs.                                   |
| Profiling          | Selected profiling engines.                                   |
| Engine             | Engine launch directories.                                    |
| Manifest requests  | The requested manifest.                                       |

Error codes:

| Code                      | Code                      | Code                     |
| ------------------------- | ------------------------- | ------------------------ |
| `invalid_arguments`       | `invalid_input`           | `input_missing`          |
| `output_exists`           | `permission_denied`       | `gate_failed`            |
| `evidence_gate_failed`    | `gates_skipped`           | `engine_selection_empty` |
| `engine_unhealthy`        | `instance_degraded`       | `runtime_package_missing` |
| `collection_partial`      | `engine_http_error`       | `operation_failed`       |
| `stage_timeout`           | `stage_cancelled`         | `interrupted`            |

Branch on `status`, `code`, and the recovery data for stage failures in `context`.

JSON results and diagnostics redact:

- Credential environment values
- HTTP URL credentials
- Bearer values

Validate a result:

```python
import json
from narwhal.contracts import COMMAND_RESULT, validate_document

with open("check-result.json") as source:
    result = json.load(source)
validate_document(result, COMMAND_RESULT)
```

`narwhal-check --print-contract-versions` lists the result schema with the persisted interfaces.

| Change                                 | Schema version |
| -------------------------------------- | -------------- |
| New `data` field or error code         | Unchanged      |
| Incompatible envelope change           | New version    |
