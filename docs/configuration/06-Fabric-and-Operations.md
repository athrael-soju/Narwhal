# Fabric, CLI, and configuration operations

## 17. Fabric workload qualification

For each engine host, `prepare` writes a snapshot of `tools/deployment/fabric_budget.py`, stores its SHA-256 in the manifest and the engine role environment, and packages the selected application revision in `source.bundle`. `install` checks the transferred snapshot before copying it into `runs/deployment-tools/`. When the helper changes, start a new preparation directory so each run keeps its own source bundle, helper snapshot, and recorded digest.

### 17.1 Calculate the workload budget

Run the calculator once for each representative engine role and matching cache configuration:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
  --model-config <config.json> --launch-config <launch-record.json> \
  --runtime-layout <cache-layout.json> \
  --prompt-tokens <tokens> --handoffs-per-s <rate> --burst <handoffs> \
  --transfer-budget-s <seconds> --headroom <factor> \
  --out <budget.json>
```

`calculate` reads the representative's `cache-layout.json` through `--runtime-layout`. Capture that file from the running engine with `launch_engine.py capture-cache`. The calculator verifies the model and launch-record hashes, sums the padded cache-page bounds across TP ranks, and computes the link rate the workload needs. The workload is described by prompt length, peak remote-handoff rate, burst, transfer-time budget, and a headroom factor of at least 1.

[Engine validation](../deploy/03-Validate-Engines.md#derive-cache-equivalence-groups) groups roles by image, model config, accelerator, TP shape, GPU visibility, runtime inputs, and transport. Capture the live cache layout from every engine. In [transfer fabric qualification](../deploy/04-Qualify-Fabric.md), engines with matching layouts share one source budget, derived from a single representative capture. Each directed host edge is compared against that budget.

### 17.2 Retained budget evidence

The representative writes a mode-0600 `runs/fabric-*/budget.json` containing the model-config, launch-record, and runtime-layout hashes, the prompt length, padded-page payload bound, sizing assumptions, and the required rate in decimal Gbit/s. Each source role with a matching layout copies the budget rate and hash into its private comparison file.

The captured layout records per-rank and per-layer page bytes, the token block size, state and boundary allowances, image identity, package versions, application revision, and launch-plan hash. The representative captures its cache pages after model loading and memory profiling, then continues to HTTP startup. Attestation and workload trials run against the same model and recorded cache geometry.

### 17.3 Uniform-cache options

`--uniform-cache` switches to an analytical attention/MLA estimate, and `--bytes-per-token` supplies a measured uniform-cache figure. Both uniform modes need `--element-bytes` and `--block-tokens`. The deployment path uses neither option. It reads the runtime page record for both hybrid and uniform models.

### 17.4 Link evidence

TCP comparisons use the aggregate bitrate reported by the iperf3 receiver, and RDMA comparisons use the average Gbit/s from the retained perftest report.

`fabric_budget.py link` fingerprints each directed pair by its roles, addresses, interfaces, routes, transport, utility version, and test parameters, and `record-edge` binds the bandwidth sample and source budget to that fingerprint. If a budget is corrected later and the fingerprint still matches, `reuse-edge` compares the retained sample against the new budget and writes a new private comparison.

Each budget applies to one directed host edge at the recorded workload. The budget is a sizing input. KV probes on the running engines and concurrent-capacity tests provide acceptance.

---

## 18. CLI precedence

`narwhal-serve` loads the fleet document first and then applies these overrides:

| CLI option           | Config field                 | Behavior                                                         |
| -------------------- | ---------------------------- | ---------------------------------------------------------------- |
| `--max-concurrent`   | `serving.max_connections`    | Sets the router's admission limit to at most `serving.max_connections`. |
| `--graceful-timeout` | `serving.graceful_timeout_s` | Replaces the Uvicorn shutdown drain time.                        |
| `--resume`           | `recovery.resume`            | Enables resume. If `recovery.resume` is already `true`, it stays enabled. |

The CLI flags `--host`, `--port`, `--log-level`, `--journal`, and the warm-standby options set the bind address, port, log level, journal path, and warm-standby behavior. The [CLI reference](../CLI-Reference.md) gives their defaults and validation rules.

---

## 19. Request journal

Narwhal writes request timing records to `journal.jsonl` next to `profiles.path`. `narwhal-serve --journal PATH` sets a different location. The [request-journal reference](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) describes the record format.

---

## 20. Configuration provenance and publication

Keep the fleet configuration with every scored run to reproduce it.

Fleet documents contain engine URLs that can reveal site addresses, so replace them before publishing any artifact. The repository's annotated example uses placeholder addresses. Keep live fleet files in `runs/` or in a Git-ignored `config/fleet.*.json` path. Keep real host allocations, deployment credentials, runtime captures, and launch records in Git-ignored private paths too.

---

## 21. Operational sequence

Follow these steps for a new deployment or after a material change:

1. Define the fleet's model, SLOs, engine set, controller policy, serving bounds, recovery policy, and profile limits.
2. Load the private workstation `.env`.
3. Run deployment discovery to produce the host inventory, SSH trust store, fleet endpoints, launch records, source indexes, and deployment environment.
4. Prepare a new deployment output directory from the management revision.
5. Install the verified source bundle and role-specific configuration on each physical host.
6. Inspect every engine host against its generated launch record.
7. Verify image, package, and connector identity, then start every engine and capture its live cache layout.
8. Qualify the directed transfer fabric against budgets derived from the matching cache layouts.
9. Start one attestation sidecar per engine and finalize the fleet contract from the live processes.
10. Profile the deployed engine shape with the generated `profiling-limits.json`.
11. Run `narwhal-check` against the fleet and engine build.
12. Start the router and observability services with the qualified fleet configuration.
13. Measure workload capacity with the final settings for authentication, queueing, retries, byte limits, and timeouts.
14. Run the controller in advisory mode against representative traffic before enabling role movement in production.

A material change to the engine build, serving policy, queueing, concurrency, retries, handoff timeout, or byte limits invalidates the dependent capacity measurements. Repeat them.

Keep the fleet, the generated private inputs, the launch records, the revision, the launcher digest, and the container identity with the run artifacts.
