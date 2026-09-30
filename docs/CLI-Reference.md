# Narwhal CLI reference

Installing `narwhal-inference` gives you the commands below. `narwhal --help` lists all six installed commands.

Every installed executable accepts `-h`, `--help`, and `--version`. Subcommands such as `narwhal config` accept `-h` and `--help`; put `--version` straight after the executable name. Relative paths are resolved from the directory you run the command in.

In deployment scripts, call the installed `narwhal-*` commands rather than Python module paths, because module paths can change between releases. (`python -m narwhal.cli` also starts a router.)

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

| Code | Meaning                                                                                                                                                                                                      |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0    | Success.                                                                                                                                                                                                     |
| 1    | An operation failed, such as an engine HTTP request, a runtime inspection, a listener bind, a verification gate, or teardown. `narwhal dev status` also returns 1 when the development instance is degraded. |
| 2    | Invalid arguments or configuration input.                                                                                                                                                                    |

`narwhal diagnostics collect` also returns 3 for a partial bundle and 4 for an I/O error. See [Manifest and exit status](Diagnostic-Bundles.md#manifest-and-exit-status).

When a failure is expected, the command prints the command name, the operation, and the path or value involved to stderr. Unexpected failures print a Python traceback. If an engine inspection fails, its subprocess output is kept in the run's diagnostic logs.

## JSON output

Commands that exit on their own, rather than running as a server, accept `--format json`. They then return a [versioned command result](Command-Results.md), which covers failures and artifact references and defines how each status maps to an exit code.
