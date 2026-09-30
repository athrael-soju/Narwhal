# Interface versions and compatibility

## Check interface compatibility before deployment

Validation fails when a document's schema name or version differs from the pair its Narwhal reader supports.

Print the installed contract manifest:

```bash
narwhal-check --print-contract-versions
```

The `narwhal.contract-manifest` document lists each interface with these fields:

| Field    | Meaning                              |
| -------- | ------------------------------------ |
| `schema` | Interface schema name.               |
| `write`  | Version the release writes.          |
| `read`   | Versions the release accepts.        |

Interface versions in this release:

| Interface                     | Schema                        | Version |
| ----------------------------- | ----------------------------- | ------: |
| Narwhal fleet configuration   | `narwhal.fleet`               |       1 |
| Effective fleet configuration | `narwhal.effective-config`    |       1 |
| Engine profile store          | `narwhal.profiles`            |       1 |
| Engine attestation            | `narwhal.attestation`         |       1 |
| Router state handoff          | `narwhal.handoff`             |       1 |
| Router lease                  | `narwhal.router-lease`        |       1 |
| Engine lifecycle              | `narwhal.lifecycle`           |       1 |
| Request journal               | `narwhal.journal`             |       1 |
| Live state                    | `narwhal.state`               |       1 |
| Prometheus metrics            | `narwhal.metrics`             |       1 |
| Command result                | `narwhal.command-result`      |       1 |
| Contract manifest             | `narwhal.contract-manifest`   |       1 |
| Diagnostic bundle             | `narwhal.diagnostic-bundle`   |       1 |

To compare manifests:

1. Save the manifest from each environment:

    ```bash
    # installed environment
    narwhal-check --print-contract-versions > installed-contracts.json

    # candidate environment
    narwhal-check --print-contract-versions > candidate-contracts.json
    ```

2. Compare the two files:

    ```bash
    diff installed-contracts.json candidate-contracts.json
    ```

3. Keep the previous release's code, configuration, profiles, and state together as a [rollback set](../operate/04-Upgrade-and-Validate.md#103-roll-back).

A changed `schema` or `write` value can fail validation of installed-release documents on the candidate.

## Schema version changes

Each interface, including the [command result](../Command-Results.md) envelope, needs a new schema version when a field is renamed, removed, or retyped.
