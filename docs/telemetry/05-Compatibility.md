---
description: Check the schema versions of Narwhal interfaces and documents before a deployment.
---

# Interface versions and compatibility

## Checking interface compatibility before deployment

Validation fails when a document's schema name or version differs from the pair its Narwhal reader supports.

Print the installed contract manifest:

```bash
narwhal-check --print-contract-versions
```

The `contracts` object of the `narwhal.contract-manifest` document maps each manifest key to these fields:

| Field | Meaning |
| --- | --- |
| `schema` | Interface schema name. |
| `write` | Version the release writes. |
| `read` | Versions the release accepts. |

Interface versions in this release:

| Interface | Schema | Version | Manifest key |
| --- | --- | :--: | --- |
| Narwhal fleet configuration | `narwhal.fleet` | 2 | `fleet` |
| Effective fleet configuration | `narwhal.effective-config` | 2 | `effective_config` |
| Engine profile store | `narwhal.profiles` | 1 | `profiles` |
| Engine attestation | `narwhal.attestation` | 1 | `attestation` |
| Router state handoff | `narwhal.handoff` | 1 | `handoff` |
| Router lease | `narwhal.router-lease` | 1 | `lease` |
| Engine lifecycle | `narwhal.lifecycle` | 1 | `lifecycle` |
| Request journal | `narwhal.journal` | 1 | `journal` |
| Live state | `narwhal.state` | 1 | `state` |
| Prometheus metrics | `narwhal.metrics` | 2 | `metrics` |
| Command result | `narwhal.command-result` | 1 | `command_result` |
| Contract manifest | `narwhal.contract-manifest` | 1 | `cli` |
| Diagnostic bundle | `narwhal.diagnostic-bundle` | 1 | `diagnostic_bundle` |

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

3. Keep the previous release's code, configuration, profiles, and state together as a [rollback set](../operate/04-Upgrade-and-Validate.md#103-rolling-back).

When the manifests are identical, installed-release documents pass validation on the candidate. When `schema` or `write` changes for an interface, that interface's installed-release documents fail validation on the candidate.

## Schema version changes

Each interface, including the [command result](../Command-Results.md) envelope, needs a new schema version when a field is renamed, removed, or retyped. For Prometheus metrics, a new label on an existing series also needs a new version.

Metrics version `2` adds the `reason` label to `narwhal_failed_total`, `narwhal_rejected_total`, and `narwhal_expired_total`, the `cause` label to `narwhal_refused_total`, and the `stage` label to `narwhal_queue_wait_seconds`.

Fleet configuration version `2` and effective fleet configuration version `2` remove `serving.prefill_concurrency` and `serving.decode_concurrency`. The router derives [engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats) in their place. Delete both keys and set `schema_version` to `2` to upgrade a version `1` fleet file.
