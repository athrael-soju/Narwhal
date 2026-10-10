---
description: Fabric qualification, CLI precedence, request journal and configuration provenance settings in Narwhal.
---

# Fabric, CLI, and configuration operations

## Fabric workload qualification

`deploy_hosts.py prepare` copies a snapshot of the fabric helper, `tools/deployment/fabric_budget.py`, for every engine host. The prepared manifest and `NARWHAL_FABRIC_BUDGET_SHA256` hold the snapshot's SHA-256.

`deploy_hosts.py install` verifies the installed snapshot at `runs/deployment-tools/fabric_budget.py`, and `NARWHAL_FABRIC_BUDGET_TOOL` holds its path.

If the helper changes, start a new preparation directory.

### Calculating the workload budget

[Cache-equivalence grouping](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) groups engine roles by:

- discovered image
- model configuration
- accelerator
- tensor parallel (TP) size
- GPU visibility variable
- runtime inputs
- transfer transport

Engines with matching captured cache layouts share one budget from a representative engine.

To compute the budget:

1. Capture each engine's `cache-layout.json` in its engine-role shell:

    ```bash
    narwhal-engine capture-cache --run "$ENGINE_RUN"
    ```

2. Run `calculate` in the shell of each representative engine role:

    ```bash
    python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
      --model-config "$NARWHAL_MODEL_DIR/config.json" \
      --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" \
      --runtime-layout "$ENGINE_RUN/cache-layout.json" \
      --prompt-tokens 1024 --handoffs-per-s 1 --burst 1 \
      --transfer-budget-s 1 --headroom 1.25 \
      --out "$FABRIC_RUN/budget.json"
    ```

3. Compare each directed host edge with its group budget using the [link evidence](#link-evidence).

`calculate` accepts these options:

| Option                        | Default          | Description                                                                         |
| ----------------------------- | ---------------- | ----------------------------------------------------------------------------------- |
| `--model-config PATH`         | required         | Model configuration file whose hash matches the runtime layout.                     |
| `--launch-config PATH`        | required         | Engine launch record that supplies the TP size and matches the runtime layout hash. |
| `--runtime-layout PATH`       | one cache source | Captured `cache-layout.json`.                                                       |
| `--prompt-tokens N`           | required         | Prompt length, in tokens.                                                           |
| `--handoffs-per-s RATE`       | required         | Peak remote KV handoff rate, in handoffs per second.                                |
| `--burst N`                   | required         | Number of KV handoffs in a burst.                                                   |
| `--transfer-budget-s SECONDS` | required         | Time budget for transferring one burst.                                             |
| `--headroom FACTOR`           | required         | Multiplier of at least 1 applied to the required rate.                              |
| `--out PATH`                  | required         | Fresh private output path for the budget.                                           |

Give `calculate` one cache source: `--runtime-layout`, `--uniform-cache`, or `--bytes-per-token`.

### Retained budget evidence

Budget file: `runs/fabric-*/budget.json`, mode 0600, on the representative engine host.

The budget file holds:

- the model, launch, and runtime-layout hashes
- the image identity, launch-plan hash, and TP size
- the prompt length
- the padded-page payload bound per handoff
- the sizing assumptions
- the required decimal Gbit/s

The budget is the total rate over all TP ranks that one directed host edge must carry at the recorded workload.

Each matching source role's private comparison budget holds the representative's budget rate and hash.

The captured layout holds per-layer page bytes for each TP rank, the token block size, and extra blocks for state and boundary allowance. It also holds the image identity, package versions, application revision and launch-plan hash.

### Uniform-cache options

Deployments use `--runtime-layout` for every model.

Offline estimates use `--uniform-cache` for an analytical attention or multi-head latent attention (MLA) estimate, or `--bytes-per-token N` for a measured override. Both options require `--element-bytes` of `1`, `2` or `4`, and `--block-tokens`.

### Link evidence

For `ucx_tcp`, the measured rate is the aggregate received bitrate that the iperf3 receiver reports, and `record-edge` takes the iperf3 JSON as `--sample`. For `ucx_rdma`, the measured rate is the average Gbit/s from the retained perftest report, and `record-edge` takes that rate as `--gbps` with the report as `--sample`.

`fabric_budget.py link` prints a SHA-256 link fingerprint per directed pair over these inputs:

- roles
- addresses
- interfaces
- routes
- transport
- utility version
- test parameters

These `fabric_budget.py` commands compare a link sample with a budget:

| Command       | Result                                                                                                              |
| ------------- | ------------------------------------------------------------------------------------------------------------------- |
| `compare`     | Compares one sample, from `--iperf` or `--gbps`, with a budget.                                                     |
| `record-edge` | Records whether the sample rate meets the source budget for the link fingerprint.                                   |
| `reuse-edge`  | Writes a new private comparison of the retained sample against a corrected budget, for a matching link fingerprint. |


## CLI precedence

`narwhal-serve` options take precedence over fleet fields.

`--host`, `--port`, `--log-level`, and the warm-standby options have [command-line defaults](../CLI-Reference.md).

Options that default to a fleet field:

| Option                       | Default                      | Description                                                     |
| ---------------------------- | ---------------------------- | --------------------------------------------------------------- |
| `--max-concurrent N`         | `serving.max_connections`    | Router [in-flight limit](02-Serving-and-Role-Control.md#in-flight-limit), from 1 to `serving.max_connections`. |
| `--graceful-timeout SECONDS` | `serving.graceful_timeout_s` | Uvicorn shutdown drain time in zero or more whole seconds.      |
| `--resume`                   | `recovery.resume`            | Turns resume on.                                                |

To keep resume off, set `recovery.resume` to `false` and omit `--resume`.


## Request journal

`narwhal-serve` writes request timing records to [`journal.jsonl`](../telemetry/01-Journal.md#diagnosing-a-request-from-the-journal) beside [`profiles.path`](03-Recovery-and-Validation.md#profile-validation). `--journal PATH` writes them to `PATH`.


## Configuration provenance and publication

- Keep the exact fleet configuration beside every scored run.
- Replace the engine URLs in that configuration before publishing an artifact.
- Keep live fleet files in a Git-ignored path, such as `runs/` or `config/fleet.json`.
- Keep real host allocations, credentials, runtime evidence and launch records in Git-ignored private paths.


## Operational sequence

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
