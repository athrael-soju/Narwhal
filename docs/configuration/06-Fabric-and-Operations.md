# Fabric, CLI, and configuration operations

## 17. Fabric workload qualification

`prepare` writes a snapshot of `tools/deployment/fabric_budget.py` for each engine host, stores its SHA-256 in the manifest and engine role environment, and packages the selected application revision in `source.bundle`. `install` checks the transferred snapshot before copying it to `runs/deployment-tools/`.

Start a new preparation directory when the helper changes so each run retains its source bundle, helper snapshot, and recorded digest.

### 17.1 Calculate the workload budget

For each representative engine role and matching cache configuration:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate
```

Use:

```text
--runtime-layout
```

with the `cache-layout.json` captured from the running cache representative by:

```text
launch_engine.py capture-cache
```

The calculator:

1. verifies model and launch-record hashes,
2. sums padded cache-page bounds across TP ranks,
3. derives the link rate required by the configured workload.

The workload inputs are:

- prompt length
- peak remote-handoff rate
- burst
- transfer-time budget

[Transfer fabric preparation](../deploy/04-Qualify-Fabric.md) groups roles by:

- discovered image
- model
- accelerator
- TP shape
- runtime inputs

Capture the live cache layout from every engine. Matching layouts share a source budget derived from one representative capture; each directed host edge is compared with that source budget.

### 17.2 Retained budget evidence

The representative writes mode-0600 `runs/fabric-*/budget.json` containing:

- input hash
- runtime-layout hash
- prompt length
- padded-page payload bound
- sizing assumptions
- required decimal Gbit/s

Each matching source role records the budget rate and hash in its private comparison file.

The captured layout retains:

- per-rank page bytes
- per-layer page bytes
- token block size
- state allowance
- boundary allowance
- image identity
- package versions
- application revision
- launch-plan hash

After model loading and memory profiling, the representative captures its cache pages and continues to HTTP startup. Attestation and workload trials use that running model and its recorded cache geometry.

### 17.3 Uniform-cache options

`--uniform-cache` selects an analytical attention/MLA estimate.

`--bytes-per-token` supplies a measured uniform-cache override.

Both uniform modes require:

```text
--element-bytes
--block-tokens
```

The deployment path uses the runtime page record for both hybrid and uniform models.

### 17.4 Link evidence

TCP comparisons use aggregate bitrate reported by the iperf3 receiver.

RDMA comparisons use average Gbit/s from the retained perftest report.

`fabric_budget.py link` records each directed pair's:

- roles
- addresses
- interfaces
- routes
- transport
- utility version
- test parameters

`record-edge` binds the sample and source budget to that fingerprint.

`reuse-edge` compares a retained bandwidth sample against a corrected budget when the fingerprint still matches, then writes a new private comparison.

Each budget covers one directed host edge at the recorded workload.

Running-engine KV probes and concurrent-capacity tests provide later acceptance evidence.

---

## 18. CLI precedence

`narwhal-serve` applies these CLI overrides after loading the fleet document:

| CLI option           | Config field                 | Behaviour                                              |
| -------------------- | ---------------------------- | ------------------------------------------------------ |
| `--max-concurrent`   | `serving.max_connections`    | Replaces router admission capacity.                    |
| `--graceful-timeout` | `serving.graceful_timeout_s` | Replaces Uvicorn shutdown drain time.                  |
| `--resume`           | `recovery.resume`            | Forces resume on. A configured `true` remains enabled. |

Configure the bind address, port, log level, journal path, and warm standby through CLI flags:

- `--host`
- `--port`
- `--log-level`
- `--journal`
- warm-standby options

The [CLI reference](../CLI-Reference.md) defines their defaults and validation.

---

## 19. Request journal

Narwhal writes request timing records to `journal.jsonl` beside `profiles.path`. Use `narwhal-serve --journal PATH` to select another path.

The [request-journal reference](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) defines the record format.

---

## 20. Configuration provenance and publication

Keep the exact fleet configuration beside every scored run.

Fleet documents contain engine URLs and can reveal site addresses. Replace those values before publishing an artifact.

The repository's annotated example uses placeholder addresses.

Live fleet files belong under:

- ignored `runs/`
- a gitignored `config/fleet.*.json` path

Keep real host allocations, deployment credentials, runtime evidence, and launch records in ignored private paths.

---

## 21. Operational sequence

For a new or materially changed deployment, the configuration flow is:

1. Define the fleet model, SLOs, engine set, controller policy, serving bounds, recovery policy, and profile limits.
2. Load the private workstation `.env`.
3. Run deployment discovery to produce host inventory, SSH trust, fleet endpoints, launch records, source indexes, and deployment environment.
4. Prepare a new deployment output directory from the exact management revision.
5. Install the verified source bundle and role-specific configuration to each physical host.
6. Inspect every engine host against its generated launch record.
7. Verify image, package, and connector identity, then start every engine and capture its live cache layout.
8. Qualify the directed transfer fabric against budgets derived from the matching cache layouts.
9. Start one attestation sidecar per engine and finalise the fleet contract from the live processes.
10. Profile the deployed engine shape using generated `profiling-limits.json`.
11. Run `narwhal-check` against the exact fleet and engine build.
12. Start the router and observability services with the qualified fleet configuration.
13. Measure workload capacity with the final authentication mode, queueing, retry, byte-limit, and timeout settings.
14. Run controller advisory mode against representative traffic before allowing production role movement.
15. Preserve the fleet, generated private inputs, launch evidence, revision, launcher digest, and container identity beside the run artifacts.

Any material change to engine build, serving policy, queueing, concurrency, retry, handoff timeout, or byte limits invalidates the corresponding capacity evidence and requires remeasurement.
