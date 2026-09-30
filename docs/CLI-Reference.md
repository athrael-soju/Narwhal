# Narwhal CLI reference

`narwhal-inference` installs `narwhal` and the five `narwhal-*` commands.

- Each executable accepts `-h`, `--help`, and `--version`.
- Relative paths resolve from the process working directory.
- `python -m narwhal.cli` starts a router.

| Command                                        | Purpose                                                                   |
| ---------------------------------------------- | ------------------------------------------------------------------------- |
| [`narwhal config`](Config-Inspection.md)       | Validate fleet files and inspect resolved defaults and paths offline      |
| [`narwhal diagnostics`](Diagnostic-Bundles.md) | Collect private router snapshots and incident artifacts                   |
| [`narwhal dev`](cli/Dev.md)                    | Initialize, launch, verify, and stop a local shared-GPU development fleet |
| [`narwhal-engine`](cli/Engine.md)              | Prepare and launch checked engine processes                               |
| [`narwhal-attest`](cli/Attest.md)              | Serve engine identity and attestation data for one vLLM engine            |
| [`narwhal-serve`](cli/Serve.md)                | Run a Narwhal router                                                      |
| [`narwhal-profile`](cli/Profile.md)            | Measure engine behaviour and write the profile store                      |
| [`narwhal-check`](cli/Check.md)                | Run deployment preflight gates                                            |

## Text-mode exit codes

`narwhal config`, `narwhal dev`, `narwhal-engine`, `narwhal-attest`, `narwhal-serve`, `narwhal-profile`, and `narwhal-check` use these codes in text mode:

| Exit code | Outcome                                                                                               |
| :-------: | ----------------------------------------------------------------------------------------------------- |
|       `0` | Success.                                                                                              |
|       `1` | Operation failure: engine HTTP request, runtime inspection, listener bind, verification, or teardown. |
|       `2` | Invalid arguments or configuration inputs.                                                            |

Command-specific exit codes:

| Command | Exit code | Case |
| --- | :--: | --- |
| [`narwhal dev`](cli/Dev.md#output-and-exit-codes) | `1` | The instance reports `degraded`. |
| [`narwhal dev init`](cli/Dev.md#output-and-exit-codes) | `2` | An initialization check fails. |
| [`narwhal-engine prepare`](cli/Engine.md#actions) | `2` | Preparation fails. |
| [`narwhal-engine`](cli/Engine.md#actions) | `1` | An output artifact already exists. |
| [`narwhal-check`](cli/Check.md#exit-codes) | `1` | A gate is skipped with `--evidence-out`. |
| [`narwhal diagnostics collect`](Diagnostic-Bundles.md#manifest-and-exit-status) | `3` | Partial bundle. |
| [`narwhal diagnostics collect`](Diagnostic-Bundles.md#manifest-and-exit-status) | `4` | I/O failure. |

## Failure diagnostics

- An expected failure writes one stderr line naming the command, operation, and affected path or value.
- An unexpected failure prints a Python traceback.
- Engine inspections keep subprocess output in the run's diagnostic logs.

## JSON command results

| Property  | Value                                                                                                    |
| --------- | -------------------------------------------------------------------------------------------------------- |
| Commands  | `narwhal config`, `narwhal diagnostics`, `narwhal dev`, `narwhal-engine`, `narwhal-profile`, `narwhal-check` |
| Flag      | `--format json`                                                                                          |
| Output    | A [versioned command result](Command-Results.md)                                                          |
| Exit code | Derived from the result `status`                                                                         |
