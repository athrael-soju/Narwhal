# Machine-readable command results

To run Narwhal from CI or a script, add `--format json` to the command.
The command prints one JSON object to stdout with the status, the files touched,
and any errors. Progress messages, diagnostics, and subprocess output go to
stderr, so stdout can be redirected into a file or a parser.

The flag works with `narwhal-check`, `narwhal-profile`, `narwhal-engine`,
`narwhal dev`, `narwhal config`, and `narwhal diagnostics`, and it can go anywhere
on the command line.

```bash
narwhal-check --fleet runs/fleet.json --format json >check-result.json
narwhal-profile --fleet runs/fleet.json --format json >profile-result.json
narwhal-engine check --run runs/engine-1 --format json >engine-result.json
narwhal dev status --instance runs/dev --format json >status-result.json
```

Every invocation returns a result object, including invocations that fail on bad
arguments. With `--help` and `--format json`, the help text goes to stderr and the result reports
success.

Narwhal's own progress messages appear on stderr as they happen. Subprocess
output is collected while the command runs and written to stderr in one piece
when the command finishes.

Without `--format json`, commands print their usual output and use their own exit
codes, which differ from the ones below. `narwhal dev` also prints lifecycle JSON
by default, in a separate format from the result object described here.

`narwhal-serve` and `narwhal-attest` reject `--format json`. They run until
stopped, so no single result exists. While they run, query their HTTP endpoints or
read the files they write.

## Statuses and exit codes

| Status          | Exit code | Meaning                                                                                                         |
| --------------- | --------: | --------------------------------------------------------------------------------------------------------------- |
| `success`       |         0 | Everything the command was asked to do finished.                                                                |
| `failed_gate`   |         1 | A preflight check, an evidence requirement, or an engine health check stopped the operation.                    |
| `invalid_input` |         2 | Something you supplied was wrong or missing: an argument, a configuration value, or an input file.              |
| `degraded`      |         3 | The operation finished, but some preflight checks were skipped, the development instance isn't fully healthy, or a diagnostic bundle is partial. |
| `error`         |         4 | Something broke partway through, a stage ran past its deadline, or a file couldn't be accessed.                 |
| `interrupted`   |       130 | The command was canceled, for example with Ctrl+C, and still reported its result.                               |

The exit code gives pass/fail. The JSON gives the reason.

## What a result looks like

This is the result of running `narwhal dev status` against an instance directory
that doesn't exist. It's formatted for reading; the command prints it on one
line with its keys sorted.

```json
{
  "schema": "narwhal.command-result",
  "schema_version": 1,
  "command": "narwhal",
  "operation": "dev status",
  "status": "invalid_input",
  "exit_code": 2,
  "data": {"instance": "/home/you/runs/dev"},
  "artifacts": [
    {"kind": "instance", "path": "/home/you/runs/dev/instance.json", "state": "missing"},
    {"kind": "lifecycle", "path": "/home/you/runs/dev/lifecycle.json", "state": "missing"},
    {"kind": "fleet", "path": "/home/you/runs/dev/fleet.json", "state": "missing"}
  ],
  "errors": [
    {
      "code": "input_missing",
      "message": "[Errno 2] No such file or directory: '/home/you/runs/dev/instance.json'",
      "command": "narwhal",
      "stage": "dev status"
    }
  ]
}
```

| Field                      | What it holds                                                                                                                                                                                                                                                        |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`, `schema_version` | `narwhal.command-result` and `1` for this version.                                                                                                                                                                  |
| `command`, `operation`     | The executable you ran and the operation within it, such as `narwhal` and `dev status`. If the arguments are too broken to identify the operation, `operation` is the command's default.                                                                    |
| `status`, `exit_code`      | One of the statuses above and its exit code.                                                                                                                                                                                                       |
| `data`                     | Results for the operation you ran. Depending on the command, this is the dev instance's lifecycle state, the preflight results (failures, skips, warnings, and pairs), the engines chosen for profiling, the engine launch directories, the resolved fleet configuration, the diagnostic bundle summary, or a manifest you asked for. |
| `artifacts`                | Files the command wrote or depends on. See below.                                                                                                                                                                                                                    |
| `errors`                   | Errors the command recorded. See below.                                                                                                                                                                                                                             |

## Artifacts

Each entry in `artifacts` has a `kind`, an absolute `path`, and a `state` that
records what happened to the file during the run:

| State      | Meaning                                       |
| ---------- | --------------------------------------------- |
| `created`  | The file didn't exist before the command ran. |
| `updated`  | The file existed and was changed.             |
| `existing` | The file existed and was left alone.          |
| `missing`  | The file doesn't exist after the run.         |

Narwhal compares each file's metadata before and after the run to set the state.

When a command fails, `artifacts` lists what is on disk afterward.
Files left by a failed run can fail their own schema checks or gates, so validate
them before using them to qualify an engine. Profile, evidence, and lifecycle files
each have their own formats, documented separately.

## Errors

Every error has a `code`, a `message`, and the `command` that raised it. Some also
include `stage`, `engine`, `field`, or `context`. When a stage fails, the error
names the stage and uses `context` to describe the recovery steps.

Build logic on `code`, `status`, and those extra fields. The `message` is
written for people and its wording changes between releases.

| Status          | Error codes                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------- |
| `invalid_input` | `invalid_arguments`, `invalid_input`, `input_missing`, `output_exists`, `engine_selection_empty`, `runtime_package_missing` |
| `failed_gate`   | `gate_failed`, `evidence_gate_failed`, `engine_unhealthy`                                                           |
| `degraded`      | `gates_skipped`, `instance_degraded`, `collection_partial`                                                          |
| `error`         | `engine_http_error`, `operation_failed`, `permission_denied`, `stage_timeout`                                       |
| `interrupted`   | `stage_cancelled`, `interrupted`                                                                                    |

If a command exits with a failure status and records no specific error, the
error's `code` is the status name, such as `failed_gate` or `error`.

Future releases may add error codes. For an unrecognized code, log it and use `status` to decide what to do.

Narwhal redacts secrets in results and in any diagnostics they include:
credentials taken from environment variables, usernames and passwords embedded in
HTTP URLs, and bearer tokens.

## Reading results safely

Check `schema_version` before reading any other field, and refuse versions you
don't support. `validate_document` checks a result against the contract:

```python
import json
from narwhal.contracts import COMMAND_RESULT, validate_document

with open("check-result.json") as source:
    result = json.load(source)
validate_document(result, COMMAND_RESULT)
```

To list the versions your installation produces, run
`narwhal-check --print-contract-versions`. It lists the result schema and
the versions of Narwhal's other file formats.

Version 1 can gain new `data` fields and new error codes without a version bump.
Any change that breaks an existing reader gets a new version number.
