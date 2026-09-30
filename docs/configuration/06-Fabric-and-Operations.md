# Fabric, CLI, and configuration operations

## 17. Fabric workload qualification

| Command                   | Fabric helper result                                                                                                                                                         |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `deploy_hosts.py prepare` | Packages the selected application revision in `source.bundle`. Snapshots `tools/deployment/fabric_budget.py` on every engine host and records its SHA-256 in the manifest and engine role environment. |
| `install`                 | Verifies the transferred snapshot and copies it to `runs/deployment-tools/`.                                                                                                 |

If the helper changes, start a new preparation directory.

### 17.1 Calculate the workload budget

[Fabric qualification](../deploy/04-Qualify-Fabric.md) groups engine roles by:

- discovered image
- model
- accelerator
- tensor parallel (TP) shape
- runtime inputs

Engines in a group whose captured cache layouts match share one source budget, derived from one representative capture. To compute it:

1. Capture each engine's `cache-layout.json` with `launch_engine.py capture-cache`.
2. Run `calculate` with `--runtime-layout` in the shell of each representative engine role:

    ```bash
    python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate
    ```

3. Compare each directed host edge with its group budget using the [link evidence](#174-link-evidence).

`calculate` accepts these options:

| Option                        | Default  | Description                                                                                                  |
| ----------------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `--model-config PATH`         | required | Model configuration file. Hash must match the runtime layout.                                                |
| `--launch-config PATH`        | required | Engine launch record. Supplies the TP size. Hash must match the runtime layout.                              |
| `--runtime-layout PATH`       | one of three cache sources | Captured `cache-layout.json`.                                                              |
| `--prompt-tokens N`           | required | Prompt length, in tokens.                                                                                    |
| `--handoffs-per-s RATE`       | required | Peak remote KV handoff rate, in handoffs per second.                                                         |
| `--burst N`                   | required | Number of KV handoffs in a burst.                                                                            |
| `--transfer-budget-s SECONDS` | required | Time budget for transferring one burst.                                                                      |
| `--headroom FACTOR`           | required | Multiplier applied to the required rate. At least 1.                                                         |
| `--out PATH`                  | required | Fresh private output path for the budget.                                                                    |

Give `calculate` one cache source: `--runtime-layout`, `--uniform-cache`, or `--bytes-per-token`.

### 17.2 Retained budget evidence

`calculate` writes the budget file `runs/fabric-*/budget.json` at mode 0600 on the representative engine host. The budget file holds:

- the input and runtime-layout hashes
- the prompt length
- the padded-page payload bound
- the sizing assumptions
- the required decimal Gbit/s

A budget fixes the total rate, summed over all TP ranks, that one directed host edge must carry at the recorded workload. One budget covers every directed edge in its group. Each matching source role records the budget rate and hash in its private comparison file.

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

Attestation and workload trials run against the model and cache geometry that the representative engine captured.

### 17.3 Uniform-cache options

Deployments use the runtime page record for every model. These options serve offline estimates:

| Option              | Cache source                                                       | Requires                               |
| ------------------- | ------------------------------------------------------------------ | -------------------------------------- |
| `--uniform-cache`   | Analytical attention or multi-head latent attention (MLA) estimate | `--element-bytes` and `--block-tokens` |
| `--bytes-per-token` | Measured override                                                  | `--element-bytes` and `--block-tokens` |

### 17.4 Link evidence

| Transport | Measured rate                                             |
| --------- | --------------------------------------------------------- |
| TCP       | Aggregate received bitrate that the iperf3 receiver reports |
| RDMA      | Average Gbit/s from the retained perftest report          |

`fabric_budget.py link` records these fields for each directed pair and prints their SHA-256 fingerprint:

- roles
- addresses
- interfaces
- routes
- transport
- utility version
- test parameters

| Command       | Result                                                                                                                  |
| ------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `record-edge` | Binds the sample and source budget to the link fingerprint and records whether the rate meets the budget.               |
| `reuse-edge`  | For a matching link fingerprint, compares the retained sample against a corrected budget and writes a new private comparison. |

---

## 18. CLI precedence

`narwhal-serve` options take precedence over fleet fields. The [CLI reference](../CLI-Reference.md) covers `--host`, `--port`, `--log-level`, `--journal`, and the warm-standby options.

Options that default to a fleet field:

| Option                       | Default                      | Description                                              |
| ---------------------------- | ---------------------------- | -------------------------------------------------------- |
| `--max-concurrent N`         | `serving.max_connections`    | Sets the router admission capacity, capped at `serving.max_connections`. |
| `--graceful-timeout SECONDS` | `serving.graceful_timeout_s` | Uvicorn shutdown drain time in whole seconds (0 or more). |
| `--resume`                   | `recovery.resume`            | Turns resume on.                                        |

To turn resume off, set `recovery.resume` to `false`.

---

## 19. Request journal

Narwhal writes [request timing records](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) to `journal.jsonl` beside [`profiles.path`](01-Fleet-Schema.md#12-paths). Use `narwhal-serve --journal PATH` to select another location.

---

## 20. Configuration provenance and publication

- Keep the exact fleet configuration beside every scored run.
- Replace the engine URLs in that configuration before publishing an artifact.

| Data                                                                   | Location                                                  |
| ---------------------------------------------------------------------- | --------------------------------------------------------- |
| Live fleet files                                                       | A Git-ignored path, such as `runs/` or `config/fleet.json` |
| Real host allocations, credentials, runtime evidence, launch records   | Ignored private paths                                     |

---

## 21. Operational sequence

For a new or changed deployment:

1. Define model, SLOs, engines, controller, serving, recovery, and profile settings.
2. Load the private workstation `.env`.
3. Run deployment discovery.
4. Prepare a new deployment output directory from the exact management revision.
5. Install the source bundle and role configuration on each physical host.
6. Inspect every engine host against its generated launch record.
7. Verify image, package, and connector identity.
8. Start every engine and capture its live cache layout.
9. Qualify the directed transfer fabric against the cache-layout budgets.
10. Start one attestation sidecar per engine.
11. Finalize the fleet contract from the live processes.
12. Profile the deployed engine shape with generated `profiling-limits.json`.
13. Run `narwhal-check` against the exact fleet and engine build.
14. Start the router and monitoring stack with the qualified fleet.
15. Measure capacity with final authentication, queueing, retry, byte-limit, and timeout settings.
16. Run advisory mode on representative traffic before production role movement.
17. Archive the fleet, private inputs, and launch evidence.

Remeasure capacity after changing the engine build or any serving setting: queueing, concurrency, retry, KV handoff timeout, or byte limits.
