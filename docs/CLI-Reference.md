# Narwhal CLI reference

Installing `narwhal-inference` provides six executables: `narwhal`, `narwhal-engine`, `narwhal-check`, `narwhal-attest`, `narwhal-serve`, and `narwhal-profile`. The `config`, `diagnostics`, and `dev` commands are subcommands of `narwhal`. `narwhal --help` lists all six installed commands.

Every executable accepts `-h`, `--help`, and `--version`. Subcommands such as `narwhal config` accept only `-h` and `--help`, so place `--version` directly after the executable name. Relative paths are resolved from the directory you run the command in.

In deployment scripts, call the installed commands (`narwhal`, `narwhal-*`) instead of Python module paths. Module paths can change between releases. `python -m narwhal.cli` also starts a router.

| Command                                        | Purpose                                                                      |
| ---------------------------------------------- | ---------------------------------------------------------------------------- |
| [`narwhal config`](Config-Inspection.md)       | Validate fleet files and inspect resolved defaults and paths, offline.       |
| [`narwhal diagnostics`](Diagnostic-Bundles.md) | Collect private router snapshots and selected incident artifacts.            |
| [`narwhal dev`](cli/Dev.md)                    | Set up, start, verify, and stop a local development fleet on one shared GPU. |
| [`narwhal-engine`](cli/Engine.md)              | Prepare and launch checked engine processes.                                 |
| [`narwhal-attest`](cli/Attest.md)              | Serve the identity and attestation of one vLLM engine.                       |
| [`narwhal-serve`](cli/Serve.md)                | Run a Narwhal router.                                                        |
| [`narwhal-profile`](cli/Profile.md)            | Measure engine performance and write the profile store the router uses.      |
| [`narwhal-check`](cli/Check.md)                | Run preflight checks before a fleet takes traffic.                           |

## Exit codes

In the default text mode, the deployment and lifecycle commands use these exit codes:

| Code | Meaning                                                                                        |
| ---- | ---------------------------------------------------------------------------------------------- |
| 0    | Success.                                                                                       |
| 1    | A runtime operation failed (engine HTTP request, runtime inspection, listener bind, verification gate, teardown). |
| 2    | Invalid arguments or configuration input.                                                      |

`narwhal dev status` also returns 1 when the development instance is degraded. `narwhal diagnostics collect` also returns 3 for a partial bundle and 4 for an I/O error. See [Manifest and exit status](Diagnostic-Bundles.md#manifest-and-exit-status).

Handled errors print the command name, the operation, and the offending path or value to stderr. Unhandled errors print a Python traceback. When an engine inspection fails, its subprocess output is kept in the run's diagnostic logs.

## JSON output

Commands that run to completion accept `--format json` and return a [versioned command result](Command-Results.md). That page defines failures, artifact references, and how each status maps to an exit code.
