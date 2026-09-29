# Command results for automation

`narwhal-check`, `narwhal-profile`, `narwhal-engine`, `narwhal config`, `narwhal diagnostics`, and `narwhal dev` accept `--format json` before or after operation arguments.

| Stream | Content                                                                                                  |
| ------ | -------------------------------------------------------------------------------------------------------- |
| stdout | One `narwhal.command-result` version 1 object, including argument and operational failures.          |
| stderr | Progress and diagnostics as they occur. Subprocess stdout and stderr replay when the operation finishes. |

`--format json --help` writes help to stderr and returns a success result.

JSON mode examples:

```bash
narwhal-check --fleet runs/fleet.json --format json >check-result.json
narwhal-profile --fleet runs/fleet.json --format json >profile-result.json
narwhal-engine check --run runs/engine-1 --format json >engine-result.json
narwhal dev status --instance runs/dev --format json >status-result.json
```

`narwhal-serve` and `narwhal-attest` reject `--format`. They report through logs, exit status, HTTP endpoints, and persisted artifacts.

Text mode uses the [text-mode exit codes](CLI-Reference.md#text-mode-exit-codes). `--format json` uses this mapping:

| Status          | Exit code | Operation state                                                                                                        |
| --------------- | --------: | ---------------------------------------------------------------------------------------------------------------------- |
| `success`       |         0 | The requested operation completed.                                                                                     |
| `failed_gate`   |         1 | A preflight, evidence, or health gate rejected the operation.                                                          |
| `invalid_input` |         2 | Arguments, configuration, or required inputs failed validation.                                                        |
| `degraded`      |         3 | The operation completed with skipped preflight gates, a degraded development instance, or a partial diagnostic bundle. |
| `error`         |         4 | An operational failure or stage deadline interrupted completion.                                                       |
| `interrupted`   |       130 | The command handled cancellation.                                                                                      |

Result fields:

| Field                      | Contract                                                                                                                                                                                                       |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`, `schema_version` | `narwhal.command-result`, `1`.                                                                                                                                                                                 |
| `command`, `operation`     | Installed command and selected operation, such as `narwhal` and `dev status`. Argument failures before operation selection report the default operation.                                                       |
| `status`, `exit_code`      | Status and matching exit code from the status table.                                                                                                                                                           |
| `data`                     | Operation-specific output: lifecycle state, preflight results (failures, skips, and pairs), selected profiling engines, engine launch directories, or a requested manifest.                                    |
| `artifacts`                | References with `kind`, absolute `path`, and `state`: `created`, `updated`, `existing`, or `missing`, from file metadata before and after the operation. After a partial failure, they list the files present. |
| `errors`                   | Entries with stable `code`, diagnostic `message`, and `command`; optionally `stage`, `engine`, `field`, or `context`.                                                                                          |

Error codes include `invalid_arguments`, `invalid_input`, `input_missing`, `output_exists`, `permission_denied`, `gate_failed`, `evidence_gate_failed`, `gates_skipped`, `engine_selection_empty`, `engine_unhealthy`, `instance_degraded`, `runtime_package_missing`, `collection_partial`, `engine_http_error`, `operation_failed`, `stage_timeout`, `stage_cancelled`, and `interrupted`.

Stage failures carry recovery context in `context`. Branch on `code`, `status`, and context fields; treat `message` as diagnostic prose.

JSON results and their diagnostics redact credential environment values, HTTP URL credentials, and bearer values.

Validate a result before reading it:

```python
import json
from narwhal.contracts import COMMAND_RESULT, validate_document

with open("check-result.json") as source:
    result = json.load(source)
validate_document(result, COMMAND_RESULT)
```

`narwhal-check --print-contract-versions` lists the result schema with the persisted interfaces. Incompatible envelope changes get a new schema version.

Compatible additions can add data fields or error codes. Retain unknown codes, and use `status` for the outcome.
