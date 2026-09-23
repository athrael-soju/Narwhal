# Fabric, CLI, and configuration operations

## 17. Fabric workload qualification

During `prepare`, Narwhal snapshots:

```text
tools/deployment/fabric_budget.py
```

into the files prepared for each engine host.

Its SHA-256 is stored in:

- the deployment manifest
- the engine role environment

`install` verifies the transferred helper before placing it under:

```text
runs/deployment-tools/
```

The approved application bundle retains the selected application revision.

Record that revision together with:

```text
NARWHAL_FABRIC_BUDGET_SHA256
```

Creating a new preparation directory captures a changed helper without modifying older prepared runs.

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

It retains one serving representative per group.

The representative's running process provides the initial trial budget from its cache pages. Each directed host edge is compared with its source budget.

Every running engine later verifies its resolved cache layout against its representative.

### 17.2 Retained budget evidence

The representative stores a mode-0600:

```text
runs/fabric-*/budget.json
```

containing:

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

The serving capture records cache pages after model loading and memory profiling while the representative continues to HTTP startup.

That loaded model and its cache geometry become the basis for later attestation and workload trials.

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

The following are CLI-only and have no fleet-config equivalent:

- `--host`
- `--port`
- `--log-level`
- `--journal`
- warm-standby options

The [CLI reference](../CLI-Reference.md) defines their defaults and validation.

---

## 19. Request journal

Narwhal writes request timing records to:

```text
journal.jsonl
```

beside `profiles.path`.

Use:

```text
narwhal-serve --journal
```

to select another path.

The [request-journal reference](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) defines the record format.

---

## 20. Configuration provenance and publication

Keep the exact fleet configuration beside every scored run.

Fleet documents contain engine URLs and can reveal site addresses. Replace those values before publishing an artifact.

The repository's annotated example uses placeholder addresses.

Live fleet files belong under:

- ignored `runs/`
- a gitignored `config/fleet.*.json` path

Do not publish real host allocations, deployment credentials, runtime evidence, or private launch records.

---

## 21. Operational sequence

For a new or materially changed deployment, the configuration flow is:

1. Define the fleet model, SLOs, engine set, controller policy, serving bounds, recovery policy, and profile limits.
2. Load the private workstation `.env`.
3. Run deployment discovery to produce host inventory, SSH trust, fleet endpoints, launch records, source indexes, and deployment environment.
4. Prepare a new deployment output directory from the exact management revision.
5. Install the verified source bundle and role-specific configuration to each physical host.
6. Inspect every engine host against its generated launch record.
7. Verify image, package, and connector identity, then launch one serving representative per cache class and capture its runtime cache layout.
8. Qualify the directed transfer fabric against budgets derived from the representative runtime cache geometry.
9. Launch remaining engines and one attestation sidecar per contracted engine.
10. Finalise the fleet contract from live sidecars.
11. Profile the deployed engine shape using generated `profiling-limits.json`.
12. Run `narwhal-check` against the exact fleet and engine build.
13. Start the router and observability services with the qualified fleet configuration.
14. Measure workload capacity with the final authentication mode, queueing, retry, byte-limit, and timeout settings.
15. Run controller advisory mode against representative traffic before allowing production role movement.
16. Preserve the fleet, generated private inputs, launch evidence, revision, launcher digest, and container identity beside the run artifacts.

Any material change to engine build, serving policy, queueing, concurrency, retry, handoff timeout, or byte limits invalidates the corresponding capacity evidence and requires remeasurement.
