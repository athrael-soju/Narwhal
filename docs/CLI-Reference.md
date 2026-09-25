# Narwhal CLI reference

`narwhal-inference` installs the commands below. Each executable accepts `-h`, `--help` and `--version`, and resolves relative paths from the process working directory. `narwhal --help` lists all six installed commands.

Use the installed `narwhal-*` commands in deployment scripts because internal Python module paths can change between releases. `python -m narwhal.cli` also starts a router.

In default text mode, the deployment and lifecycle commands exit 0 on success,
2 for invalid arguments or configuration inputs, and 1 when an operation fails,
such as an engine HTTP request, runtime inspection, listener bind, verification gate or teardown. A
degraded development instance returns 1 from `narwhal dev status` in that mode.
Expected failures identify the command, operation and affected path or value on
stderr; Python tracebacks identify unexpected failures. Engine inspections retain
subprocess output in the run's diagnostic logs.

| Command                             | Purpose                                                                 |
| ----------------------------------- | ----------------------------------------------------------------------- |
| [`narwhal config`](Config-Inspection.md) | Validate fleet files and inspect resolved defaults and paths offline |
| [`narwhal diagnostics`](Diagnostic-Bundles.md) | Collect private router snapshots and selected incident artifacts |
| [`narwhal dev`](cli/Dev.md) | Initialize, launch, verify and stop a local shared-GPU development fleet |
| [`narwhal-engine`](cli/Engine.md) | Prepare and launch checked engine processes |
| [`narwhal-attest`](cli/Attest.md)   | Serve engine identity and attestation data for one vLLM engine          |
| [`narwhal-serve`](cli/Serve.md)     | Run a Narwhal router                                                    |
| [`narwhal-profile`](cli/Profile.md) | Measure engine behaviour and write the profile store used by the router |
| [`narwhal-check`](cli/Check.md)     | Run deployment preflight gates                                          |

Finite commands accept `--format json` for [versioned command results](Command-Results.md), including failures, artifact references and a documented status-to-exit-code mapping.
