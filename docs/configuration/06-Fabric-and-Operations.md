# Fabric, CLI, and configuration operations

## 17. Fabric workload qualification

| Command                                       | Fabric helper result                                                    |
| --------------------------------------------- | ----------------------------------------------------------------------- |
| `deploy_hosts.py prepare`                     | Snapshot of `tools/deployment/fabric_budget.py` for every engine host   |
| `deploy_hosts.py prepare` (manifest)          | SHA-256 of the snapshot recorded in the manifest                        |
| `deploy_hosts.py prepare` (role environment)  | SHA-256 of the snapshot recorded in the engine role environment         |
| `install`                                     | Verified snapshot in `runs/deployment-tools/`                           |

If the helper changes, start a new preparation directory.

### 17.1 Calculate the workload budget

[Fabric qualification](../deploy/04-Qualify-Fabric.md) groups engine roles by:

- discovered image
- model
- accelerator
- tensor parallel (TP) shape
- runtime inputs

Engines with matching captured cache layouts share one budget from a representative engine.

To compute the budget:

1. Capture each engine's `cache-layout.json` with `launch_engine.py capture-cache`.
2. Run `calculate` with `--runtime-layout` in the shell of each representative engine role:

    ```bash
    python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate
    ```

3. Compare each directed host edge with its group budget using the [link evidence](#174-link-evidence).

`calculate` accepts these options:

| Option                        | Default                    | Description                                                     |
| ----------------------------- | -------------------------- | --------------------------------------------------------------- |
| `--model-config PATH`         | required                   | Model configuration file whose hash matches the runtime layout. |
| `--launch-config PATH`        | required                   | Engine launch record whose hash matches the runtime layout.     |
| `--runtime-layout PATH`       | one of three cache sources | Captured `cache-layout.json`.                                   |
| `--prompt-tokens N`           | required                   | Prompt length, in tokens.                                       |
| `--handoffs-per-s RATE`       | required                   | Peak remote KV handoff rate, in handoffs per second.            |
| `--burst N`                   | required                   | Number of KV handoffs in a burst.                               |
| `--transfer-budget-s SECONDS` | required                   | Time budget for transferring one burst.                         |
| `--headroom FACTOR`           | required                   | Multiplier of at least 1 applied to the required rate.          |
| `--out PATH`                  | required                   | Fresh private output path for the budget.                       |

Give `calculate` one cache source: `--runtime-layout`, `--uniform-cache`, or `--bytes-per-token`.

### 17.2 Retained budget evidence

Budget file: `runs/fabric-*/budget.json`, mode 0600, on the representative engine host.

The budget file holds:

- the input and runtime-layout hashes
- the prompt length
- the padded-page payload bound
- the sizing assumptions
- the required decimal Gbit/s

The budget is the total rate over all TP ranks that one directed host edge must carry at the recorded workload.

Each matching source role's private comparison file holds the budget rate and hash.

The captured layout holds:

- per-rank page bytes
- per-layer page bytes
- token block size
- state allowance
- boundary allowance
- image identity
- package versions
- application revision
- launch-plan hash

### 17.3 Uniform-cache options

Deployments use the runtime page record for every model.

Offline estimates use these options:

| Option              | Cache source                                                       | Requires                               |
| ------------------- | ------------------------------------------------------------------ | -------------------------------------- |
| `--uniform-cache`   | Analytical attention or multi-head latent attention (MLA) estimate | `--element-bytes` and `--block-tokens` |
| `--bytes-per-token` | Measured override                                                  | `--element-bytes` and `--block-tokens` |

### 17.4 Link evidence

| Transport | Measured rate                                               |
| --------- | ----------------------------------------------------------- |
| TCP       | Aggregate received bitrate that the iperf3 receiver reports |
| RDMA      | Average Gbit/s from the retained perftest report            |

Link fingerprint (SHA-256, per directed pair) fields, printed by `fabric_budget.py link`:

- roles
- addresses
- interfaces
- routes
- transport
- utility version
- test parameters

| Command       | Result                                                                                                             |
| ------------- | ------------------------------------------------------------------------------------------------------------------ |
| `record-edge` | Records whether the sample rate meets the source budget for the link fingerprint.                                  |
| `reuse-edge`  | New private comparison of the retained sample against a corrected budget, for a matching link fingerprint.        |

---

## 18. CLI precedence

`narwhal-serve` options take precedence over fleet fields.

`--host`, `--port`, `--log-level`, and the warm-standby options have [command-line defaults](../CLI-Reference.md).

Options that default to a fleet field:

| Option                       | Default                      | Description                                                              |
| ---------------------------- | ---------------------------- | ------------------------------------------------------------------------ |
| `--max-concurrent N`         | `serving.max_connections`    | Sets the router admission capacity, capped at `serving.max_connections`. |
| `--graceful-timeout SECONDS` | `serving.graceful_timeout_s` | Uvicorn shutdown drain time in zero or more whole seconds.               |
| `--resume`                   | `recovery.resume`            | Turns resume on.                                                         |

To turn resume off, set `recovery.resume` to `false`.

---

## 19. Request journal

Request timing records: [`journal.jsonl`](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) beside [`profiles.path`](01-Fleet-Schema.md#12-paths).

`narwhal-serve --journal PATH` selects another location.

---

## 20. Configuration provenance and publication

- Keep the exact fleet configuration beside every scored run.
- Replace the engine URLs in that configuration before publishing an artifact.

| Data                                                                 | Location                                                   |
| -------------------------------------------------------------------- | ---------------------------------------------------------- |
| Live fleet files                                                     | A Git-ignored path, such as `runs/` or `config/fleet.json` |
| Real host allocations, credentials, runtime evidence, launch records | Ignored private paths                                      |

---

## 21. Operational sequence

For a new or changed deployment:

1. Define model, SLOs, engines, controller, serving, recovery, and profile settings.
2. Load the private workstation `.env`.
3. Run deployment discovery.
4. Prepare a new deployment output directory from the exact management revision.
5. Install the source bundle on each physical host.
6. Install the role configuration on each physical host.
7. Inspect every engine host against its generated launch record.
8. Verify image, package, and connector identity.
9. Start every engine.
10. Capture each engine's live cache layout.
11. Qualify the directed transfer fabric against the cache-layout budgets.
12. Start one attestation sidecar per engine.
13. Finalize the fleet contract from the live processes.
14. Profile the deployed engine shape with generated `profiling-limits.json`.
15. Run `narwhal-check` against the exact fleet and engine build.
16. Start the router and monitoring stack with the qualified fleet.
17. Measure capacity with final authentication, queueing, retry, byte-limit, and timeout settings.
18. Run advisory mode on representative traffic before production role movement.
19. Archive the fleet, private inputs, and launch evidence.

Remeasure capacity after changing the engine build or a serving setting: queueing, concurrency, retry, KV handoff timeout, or byte limits.
