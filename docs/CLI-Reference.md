# Narwhal CLI reference

`narwhal-inference` installs `narwhal` and the five `narwhal-*` commands.

- Each executable accepts `-h`, `--help`, and `--version`.
- `narwhal --help` lists all six executables.
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

| Exit code | Outcome                                                                                                               |
| --------: | --------------------------------------------------------------------------------------------------------------------- |
|       `0` | Success.                                                                                                              |
|       `1` | Operation failure, such as an engine HTTP request, runtime inspection, listener bind, verification gate, or teardown. |
|       `2` | Invalid arguments or configuration inputs.                                                                            |

Command-specific cases:

| Command                       | Exit code | Case                               |
| ----------------------------- | --------: | ---------------------------------- |
| `narwhal dev`                 |       `1` | The instance reports `degraded`.   |
| `narwhal dev init`            |       `2` | An initialization check fails.     |
| `narwhal-engine prepare`      |       `2` | Preparation fails.                 |
| `narwhal-engine prepare`      |       `1` | An output artifact already exists. |
| `narwhal diagnostics collect` |       `3` | Partial bundle.                    |
| `narwhal diagnostics collect` |       `4` | I/O failure.                       |

Full lists: [`narwhal dev` output and exit codes](cli/Dev.md#output-and-exit-codes) and [`narwhal diagnostics` exit statuses](Diagnostic-Bundles.md#manifest-and-exit-status).

## Failure diagnostics

- An expected failure writes one stderr line naming the command, operation, and affected path or value.
- An unexpected failure prints a Python traceback.
- Engine inspections keep subprocess output in the run's diagnostic logs.

## JSON command results

- Commands: `narwhal config`, `narwhal diagnostics`, `narwhal dev`, `narwhal-engine`, `narwhal-profile`, and `narwhal-check`.
- Flag: `--format json`.
- Output: a [versioned command result](Command-Results.md).
- Exit code: derived from the result `status`.
