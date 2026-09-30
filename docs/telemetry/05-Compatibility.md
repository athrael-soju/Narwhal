# Interface versions and compatibility

## Check interface compatibility before deployment

Every Narwhal reader checks the schema name and version of each document it reads, and rejects the document if either one doesn't match what the reader supports. In this release, each reader supports only the version its build writes, so documents with an older or a newer schema version are both rejected.

To see which contracts shipped with the installed package, run:

```bash
narwhal-check --print-contract-versions
```

The manifest lists each interface's `schema`, the version the build writes (`write`), and the versions it reads (`read`). The current versions are:

| Interface              | Schema                      | Version |
| ---------------------- | --------------------------- | ------: |
| Native fleet config    | `narwhal.fleet`             |       1 |
| Effective fleet config | `narwhal.effective-config`  |       1 |
| Engine profile store   | `narwhal.profiles`          |       1 |
| Engine attestation     | `narwhal.attestation`       |       1 |
| Router handoff         | `narwhal.handoff`           |       1 |
| Router lease           | `narwhal.router-lease`      |       1 |
| Engine lifecycle       | `narwhal.lifecycle`         |       1 |
| Request journal        | `narwhal.journal`           |       1 |
| Live state             | `narwhal.state`             |       1 |
| Prometheus metrics     | `narwhal.metrics`           |       1 |
| Command result         | `narwhal.command-result`    |       1 |
| Contract manifest      | `narwhal.contract-manifest` |       1 |
| Diagnostic bundle      | `narwhal.diagnostic-bundle` |       1 |

Prometheus output has no document header. Narwhal reports the metrics contract version in the `version` label of `narwhal_contract_info{contract="metrics"}`.

Before you upgrade, compare the installed contract manifest with the candidate's. A field change that would break an existing reader needs a new schema version for that interface. Keep the previous code, configuration, profiles, and compatible state together so you can roll all of it back at once.
