# Fabric, CLI, and configuration operations

## 17. Fabric workload qualification

`deploy_hosts.py prepare` snapshots `tools/deployment/fabric_budget.py` on every engine host and records its SHA-256 in the manifest and engine role environment. It also packages the selected application revision in `source.bundle`. `install` verifies the transferred snapshot before copying it to `runs/deployment-tools/`.

If the helper changes, start a new preparation directory.

### 17.1 Calculate the workload budget

Fabric qualification ([Prove the transfer fabric against the serving cache](../deploy/04-Qualify-Fabric.md)) groups engine roles by:

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

3. Compare each directed host edge with its group budget as described in [Link evidence](#174-link-evidence).

`calculate` accepts these options:

| Option                        | Default  | Description                                                                                                  |
| ----------------------------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `--model-config PATH`         | required | Model configuration file. Hash must match the runtime layout.                                            |
| `--launch-config PATH`        | required | Engine launch record. Supplies the TP size. Hash must match the runtime layout.                      |
| `--runtime-layout PATH`       | one of three cache sources | Captured `cache-layout.json`.                                                                |
| `--prompt-tokens N`           | required | Prompt length, in tokens.                                                                                    |
| `--handoffs-per-s RATE`       | required | Peak remote KV handoff rate, in handoffs per second.                                                         |
| `--burst N`                   | required | Number of KV handoffs in a burst.                                                                            |
| `--transfer-budget-s SECONDS` | required | Time budget for transferring one burst.                                                                      |
| `--headroom FACTOR`           | required | Multiplier applied to the required rate.                                                                     |
| `--out PATH`                  | required | Fresh private output path for the budget.                                                                    |

Give `calculate` one cache source: `--runtime-layout`, `--uniform-cache`, or `--bytes-per-token`. `--headroom` must be at least 1.

`calculate` checks the model and launch-record hashes, sums the padded cache-page bounds across TP ranks, and derives the link rate the workload needs.

### 17.2 Retained budget evidence

`calculate` writes the mode-0600 budget file `runs/fabric-*/budget.json` on the representative engine host, which holds the input and runtime-layout hashes, the prompt length, the padded-page payload bound, the sizing assumptions, and the required decimal Gbit/s.

A budget fixes the rate one directed host edge must carry at the recorded workload, summed over all TP ranks. One budget covers every directed edge in its group. Each matching source role records the budget rate and hash in its private comparison file.

The captured layout retains per-rank page bytes, per-layer page bytes, token block size, state allowance, boundary allowance, image identity, package versions, application revision, and the launch-plan hash.

The representative captures its cache pages after model loading and memory profiling, then continues to HTTP startup. Attestation and workload trials run against that model and geometry.

### 17.3 Uniform-cache options

`--uniform-cache` selects an analytical attention or multi-head latent attention (MLA) estimate. `--bytes-per-token` supplies a measured override. Both require `--element-bytes` and `--block-tokens`.

Deployments use the runtime page record for every model, so these options serve offline estimates.

### 17.4 Link evidence

| Transport | Measured rate                                             |
| --------- | --------------------------------------------------------- |
| TCP       | Aggregate received bitrate that the iperf3 receiver reports |
| RDMA      | Average Gbit/s from the retained perftest report          |

`fabric_budget.py link` records these fields for each directed pair and prints their SHA-256 fingerprint: roles, addresses, interfaces, routes, transport, utility version, and test parameters.

`record-edge` binds the sample and source budget to that fingerprint and records whether the rate meets the budget. `reuse-edge` applies when the link fingerprint still matches: it compares the retained sample against a corrected budget and writes a new private comparison.

Running-engine KV probes and concurrent-capacity tests supply the later acceptance evidence.

---

## 18. CLI precedence

`narwhal-serve` applies these options after it loads the fleet configuration. Each default comes from the field named in the table.

| Option                       | Default                      | Description                                              |
| ---------------------------- | ---------------------------- | -------------------------------------------------------- |
| `--max-concurrent N`         | `serving.max_connections`    | Sets the router admission capacity, capped at `serving.max_connections`. |
| `--graceful-timeout SECONDS` | `serving.graceful_timeout_s` | Uvicorn shutdown drain time in whole seconds (0 or more). |
| `--resume`                   | `recovery.resume`            | Turns resume on.                                        |

To turn resume off, set `recovery.resume` to `false`.

See the [CLI reference](../CLI-Reference.md) for `--host`, `--port`, `--log-level`, `--journal`, and the warm-standby options.

---

## 19. Request journal

Narwhal writes request timing records to `journal.jsonl` beside `profiles.path`. Use `narwhal-serve --journal PATH` to select another location. See [Paths](01-Fleet-Schema.md#12-paths).

The record format is in the [request-journal reference](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

---

## 20. Configuration provenance and publication

Keep the exact fleet configuration beside every scored run. Replace the engine URLs inside it before publishing an artifact.

Store live fleet files in a Git-ignored path such as `runs/`, or `config/fleet.json`. Real host allocations, credentials, runtime evidence, and launch records also stay in ignored private paths.

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
