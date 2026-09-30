# Interface versions and compatibility

## Check interface compatibility before deployment

Each reader accepts the schema name and version its build writes and rejects any other document, older or newer.

To list the interface versions of the installed build, run:

```bash
narwhal-check --print-contract-versions
```

The command prints a contract manifest with `schema`, `write` and `read` for each interface. `write` is the version the build writes. `read` lists the versions it accepts, which in this release is the same version. Current versions:

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

Prometheus output has no document header. Narwhal reports the metrics version in the `version` label of `narwhal_contract_info{contract="metrics"}`.

Narwhal bumps an interface's schema version for any change that would break an existing reader.

To upgrade:

1. Save the output of `narwhal-check --print-contract-versions` from the installed build and from the new one, then diff them.
2. Keep the previous code, config, profiles and compatible state to roll back together.
