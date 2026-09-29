# Narwhal CLI reference

`narwhal-inference` installs six executables: `narwhal` and the five `narwhal-*` commands. The table lists the `narwhal config`, `narwhal diagnostics`, and `narwhal dev` subcommands separately. Each executable accepts `-h`, `--help`, and `--version`, and resolves relative paths from the process working directory. `narwhal --help` lists all six executables.

In deployment scripts, use the installed commands, because internal Python module paths can change between releases. `python -m narwhal.cli` also starts a router.

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
