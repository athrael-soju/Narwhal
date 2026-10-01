---
description: Reference for the narwhal command and the five narwhal-* commands in the narwhal-inference package.
---

# Narwhal CLI reference

`narwhal-inference` installs `narwhal` and the five `narwhal-*` commands.

- Each executable accepts `-h`, `--help`, and `--version`.
- Relative paths resolve from the process working directory.
- `python -m narwhal.cli` starts a router.

<div class="grid cards" markdown>

-   [`narwhal config`](Config-Inspection.md)

    ---

    Validate fleet files and inspect resolved defaults and paths offline.

-   [`narwhal diagnostics`](Diagnostic-Bundles.md)

    ---

    Collect private router snapshots and incident artifacts.

-   [`narwhal dev`](cli/Dev.md)

    ---

    Initialize, launch, verify, and stop a local shared-GPU development fleet.

-   [`narwhal-engine`](cli/Engine.md)

    ---

    Prepare and launch checked engine processes.

-   [`narwhal-attest`](cli/Attest.md)

    ---

    Serve engine identity and attestation data for one vLLM engine.

-   [`narwhal-serve`](cli/Serve.md)

    ---

    Run a Narwhal router.

-   [`narwhal-profile`](cli/Profile.md)

    ---

    Measure engine behaviour and write the profile store.

-   [`narwhal-check`](cli/Check.md)

    ---

    Run deployment preflight gates.

</div>

## Text-mode exit codes

`narwhal config`, `narwhal dev`, `narwhal-engine`, `narwhal-attest`, `narwhal-serve`, `narwhal-profile`, and `narwhal-check` use these codes in text mode:

| Exit code | Outcome                                                                                               |
| :-------: | ----------------------------------------------------------------------------------------------------- |
|       `0` | Success.                                                                                              |
|       `1` | Operation failure: engine HTTP request, runtime inspection, listener bind, verification, or teardown. |
|       `2` | Invalid arguments or configuration inputs.                                                            |

These commands add exit codes for specific cases:

| Command | Exit code | Case | Reference |
| --- | :--: | --- | --- |
| `narwhal dev` | `1` | The instance reports `degraded`. | [![narwhal dev output and exit codes](https://img.shields.io/badge/docs-Output%20and%20exit%20codes-0f766e)](cli/Dev.md#output-and-exit-codes) |
| `narwhal dev init` | `2` | An initialization check fails. | [![narwhal dev output and exit codes](https://img.shields.io/badge/docs-Output%20and%20exit%20codes-0f766e)](cli/Dev.md#output-and-exit-codes) |
| `narwhal-engine prepare` | `2` | Preparation fails. | [![narwhal-engine actions](https://img.shields.io/badge/docs-Actions-0f766e)](cli/Engine.md#actions) |
| `narwhal-engine` | `1` | An output artifact already exists. | [![narwhal-engine actions](https://img.shields.io/badge/docs-Actions-0f766e)](cli/Engine.md#actions) |
| `narwhal-check` | `1` | A gate is skipped with `--evidence-out`. | [![narwhal-check exit codes](https://img.shields.io/badge/docs-Exit%20codes-0f766e)](cli/Check.md#exit-codes) |
| `narwhal diagnostics collect` | `3` | Partial bundle. | [![narwhal diagnostics manifest and exit status](https://img.shields.io/badge/docs-Manifest%20and%20exit%20status-0f766e)](Diagnostic-Bundles.md#manifest-and-exit-status) |
| `narwhal diagnostics collect` | `4` | I/O failure. | [![narwhal diagnostics manifest and exit status](https://img.shields.io/badge/docs-Manifest%20and%20exit%20status-0f766e)](Diagnostic-Bundles.md#manifest-and-exit-status) |

## Failure diagnostics

- An expected failure writes one stderr line naming the command, operation, and affected path or value.
- An unexpected failure prints a Python traceback.
- Engine inspections keep subprocess output in the run's diagnostic logs.

## JSON command results

`narwhal config`, `narwhal diagnostics`, `narwhal dev`, `narwhal-engine`, `narwhal-profile`, and `narwhal-check` accept `--format json`. With it, the command prints a [versioned command result](Command-Results.md) and derives its exit code from the result `status`.
