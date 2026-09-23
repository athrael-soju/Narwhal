# Interface versions and compatibility

## Check interface compatibility before deployment

Every versioned Narwhal interface declares both a schema name and a schema version.

Readers accept the expected schema name at the current supported version. Any other schema/version combination fails validation.

Inspect the contracts installed with the package:

```bash
narwhal-check --print-contract-versions
```

Current interfaces are:

| Interface            | Schema                      | Version |
| -------------------- | --------------------------- | ------: |
| Native fleet config  | `narwhal.fleet`             |       1 |
| Engine profile store | `narwhal.profiles`          |       1 |
| Engine attestation   | `narwhal.attestation`       |       1 |
| Router handoff       | `narwhal.handoff`           |       1 |
| Router lease         | `narwhal.router-lease`      |       1 |
| Engine lifecycle     | `narwhal.lifecycle`         |       1 |
| Request journal      | `narwhal.journal`           |       1 |
| Live state           | `narwhal.state`             |       1 |
| Prometheus metrics   | `narwhal.metrics`           |       1 |
| Contract manifest    | `narwhal.contract-manifest` |       1 |

Fields documented by these versions form the compatibility contract. An incompatible change requires a new schema version.

Before an upgrade, compare contract manifests. Keep the previous code, configuration, profiles, and compatible state together as the rollback set.
