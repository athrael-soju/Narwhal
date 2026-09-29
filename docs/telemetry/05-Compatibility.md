# Interface versions and compatibility

## Check interface compatibility before deployment

Each Narwhal reader compares a document's schema name and version with the pair it supports. Validation fails on any mismatch.

Print the installed contract manifest:

```bash
narwhal-check --print-contract-versions
```

The `narwhal.contract-manifest` document gives each interface a `schema` name, a `write` version, and its accepted `read` versions.

This release uses version 1 of every interface below.

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

If the `schema` or `write` value changes, documents written by the installed release may fail validation on the candidate. The `read` list shows which versions the candidate still accepts.

## Schema version changes

Renaming or removing a field, or changing its type, needs a new schema version for that interface. The same rule applies to the [command result](../Command-Results.md) envelope.
