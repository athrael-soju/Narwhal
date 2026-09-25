# Command results for automation

Finite `narwhal-check`, `narwhal-profile`, `narwhal-engine` and `narwhal dev`
operations accept `--format json` before or after their operation arguments.
The command writes one `narwhal.command-result` version 1 object to stdout,
including argument and operational failures, and sends progress and diagnostics
to stderr. `--format json --help` places help on stderr and returns a success
result. Python progress streams as it occurs; inherited subprocess stdout and stderr are
spooled to a private temporary file and replayed to stderr when the operation
finishes.

```bash
narwhal-check --fleet runs/fleet.json --format json >check-result.json
narwhal-profile --fleet runs/fleet.json --format json >profile-result.json
narwhal-engine check --run runs/engine-1 --format json >engine-result.json
narwhal dev status --instance runs/dev --format json >status-result.json
```

`narwhal-serve` and `narwhal-attest` run until shutdown and retain their existing
logging and process-exit interfaces. Their HTTP endpoints and persisted artifacts
supply state while the processes run. The default finite-command output also
retains its existing interface, including development lifecycle JSON and the
original exit-code mapping. Select `--format json` to use the mapping below.

| Status | Exit code | Operation state |
| --- | ---: | --- |
| `success` | 0 | The requested operation completed. |
| `failed_gate` | 1 | A preflight, evidence or health gate rejected the operation. |
| `invalid_input` | 2 | Arguments, configuration or required inputs failed validation. |
| `degraded` | 3 | The operation completed with skipped preflight gates or a degraded development instance. |
| `error` | 4 | An operational failure or stage deadline interrupted completion. |
| `interrupted` | 130 | The command handled cancellation. |

A result contains these fields:

| Field | Contract |
| --- | --- |
| `schema`, `schema_version` | `narwhal.command-result`, `1`. |
| `command`, `operation` | Installed command and selected operation, such as `narwhal` and `dev status`. Argument failures before operation selection retain the command's default operation. |
| `status`, `exit_code` | The status and matching process exit code from the table above. |
| `data` | Operation data: lifecycle state, preflight failures/skips/pairs, selected profiling engines, engine launch directories, or a requested manifest. |
| `artifacts` | References with `kind`, absolute `path` and `state`. States are `created`, `updated`, `existing` and `missing`, determined by comparing file metadata before and after the operation. |
| `errors` | Entries containing stable `code`, diagnostic `message` and `command`; applicable entries also carry `stage`, `engine`, `field` or `context`. |

Artifact references describe the files present after a partial failure. A file's
presence establishes retention; its own schema and gates determine whether it
qualifies an engine. Profile, evidence and lifecycle documents retain their
existing contracts.

Error codes include `invalid_arguments`, `invalid_input`, `input_missing`,
`output_exists`, `permission_denied`, `gate_failed`, `evidence_gate_failed`,
`gates_skipped`, `engine_selection_empty`, `engine_unhealthy`,
`instance_degraded`, `engine_http_error`, `operation_failed`, `stage_timeout`,
`stage_cancelled` and `interrupted`. Stage failures carry their recovery context.
Treat `message` as diagnostic prose and branch on `code`, `status` and context
fields. Credential environment values, HTTP URL credentials and bearer values
are redacted in JSON results and their command diagnostics.

Readers reject unsupported versions before using the remaining fields:

```python
import json
from narwhal.contracts import COMMAND_RESULT, validate_document

with open("check-result.json") as source:
    result = json.load(source)
validate_document(result, COMMAND_RESULT)
```

`narwhal-check --print-contract-versions` advertises the result schema alongside
the persisted interfaces. Incompatible envelope changes require another schema
version; compatible additions may introduce data fields or error codes. Consumers
should retain unknown codes for inspection and use `status` for the outcome.
