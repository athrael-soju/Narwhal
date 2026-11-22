---
description: Fabric qualification, CLI precedence, request journal and configuration provenance settings in Narwhal.
---

# Fabric, CLI, and configuration operations

## Fabric workload qualification

`deploy_hosts.py prepare` copies a snapshot of the fabric helper, `tools/deployment/fabric_budget.py`, for every engine host. The prepared manifest and `NARWHAL_FABRIC_BUDGET_SHA256` hold the snapshot's SHA-256.

`deploy_hosts.py install` verifies the installed snapshot at `runs/deployment-tools/fabric_budget.py`, and `NARWHAL_FABRIC_BUDGET_TOOL` holds its path.

If the helper changes, start a new preparation directory.

### Calculating the workload budget

Engines in one [cache-equivalence group](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) with matching captured cache layouts share one budget from a representative engine. [Building the source budget](../deploy/04-Qualify-Fabric.md#building-the-source-budget) gives the commands. Compare each directed host edge with its group budget using the [link evidence](#link-evidence).

`calculate` accepts these options:

| Option                            | Default          | Description                                                                                               |
| --------------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------- |
| `--model-config PATH`             | required         | Model configuration file, whose hash must match a runtime layout.                                         |
| `--launch-config PATH`            | required         | Engine launch record that supplies the tensor parallel (TP) size, whose hash must match a runtime layout. |
| `--runtime-layout PATH`           | one cache source | Captured `cache-layout.json`.                                                                             |
| `--transfer-metrics BEFORE AFTER` | one cache source | SGLang prefill `/metrics` saved by `measure-transfer` before and after its handoffs.                      |
| `--prompt-tokens N`               | required         | Prompt length, in tokens.                                                                                 |
| `--handoffs-per-s RATE`           | required         | Peak remote KV handoff rate, in handoffs per second.                                                      |
| `--burst N`                       | required         | Number of KV handoffs in a burst.                                                                         |
| `--transfer-budget-s SECONDS`     | required         | Time budget for transferring one burst.                                                                   |
| `--headroom FACTOR`               | required         | Multiplier of at least 1 applied to the required rate.                                                    |
| `--out PATH`                      | required         | Fresh private output path for the budget.                                                                 |

Give `calculate` one cache source: `--runtime-layout`, `--transfer-metrics`, `--uniform-cache`, or `--bytes-per-token`.

`measure-transfer` puts the decode engine in the decode role, switching it out and back first when it already serves decode. It then sends handoffs and returns the decode engine to the role it found. `measure-transfer` accepts these options:

| Option                  | Default                  | Description                                                                                                     |
| ----------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `--prefill URL`         | required                 | Base URL of an idle engine serving the prefill role.                                                            |
| `--decode URL`          | required                 | Base URL of another idle engine of the same cache group.                                                        |
| `--bootstrap-host HOST` | required                 | Address of the prefill engine's bootstrap server.                                                               |
| `--bootstrap-port PORT` | required                 | The prefill engine's `NARWHAL_KV_SIDE_CHANNEL_PORT`.                                                            |
| `--model NAME`          | required                 | Served model name.                                                                                              |
| `--launch-config PATH`  | required                 | Launch record whose `runtime.decode_cuda_graph_memory_gb` sizes the decode switch when the engine reports none. |
| `--prompt-tokens N`     | required                 | Prompt length of each handoff, the same as the budget's `--prompt-tokens`.                                      |
| `--handoffs N`          | 8                        | Number of handoffs.                                                                                             |
| `--api-key-env NAME`    | `NARWHAL_ENGINE_API_KEY` | Environment variable whose value, when set, goes to both engines as a bearer token.                             |
| `--out-dir PATH`        | required                 | New directory for `before.prom` and `after.prom`.                                                               |

### Retained budget evidence

Budget file: `runs/fabric-*/budget.json`, mode 0600, on the representative engine host.

The budget file holds:

- the model and launch hashes, and the runtime-layout or transfer-metrics hashes
- the TP size, and from a runtime layout the image identity and launch-plan hash
- the prompt length
- the payload per handoff, a padded-page bound or a measured mean
- the sizing assumptions
- the required decimal Gbit/s

The budget is the total rate over all TP ranks that one directed host edge must carry at the recorded workload.

Each matching source role's private comparison budget holds the representative's budget rate and hash.

The captured layout holds per-layer page bytes for each TP rank, the token block size, and extra blocks for state and boundary allowance. It also holds the image identity, package versions, application revision and launch-plan hash.

### Uniform-cache options

Deployments use `--runtime-layout` on vLLM and TensorRT-LLM, and `--transfer-metrics` on SGLang, for every model.

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

`narwhal-serve` options take precedence over fleet fields. [Serving options](../cli/Serve.md#serving-options) lists each option, its default and the fleet field it falls back to.

## Request journal

`narwhal-serve` writes request timing records to [`journal.jsonl`](../telemetry/01-Journal.md#diagnosing-a-request-from-the-journal) beside [`profiles.path`](03-Recovery-and-Validation.md#profile-validation). `--journal PATH` writes them to `PATH`.


## Configuration provenance and publication

Keep the exact fleet file beside every scored run. The [benchmark evidence collector](../measure/06-Benchmark-Evidence.md) records SHA-256 digests of the fleet file and profiles before each point and reports `input_changed` when either one changes during the point.

Live fleet files name real hosts and engine URLs. Git ignores `runs/`, `config/fleet.json` and `config/fleet.*.json`, so keep live configurations there, together with real host allocations, credentials, runtime evidence and launch records.

Before publishing a result, replace the engine URLs in its fleet file and remove engine IDs and URLs from [journal](../telemetry/01-Journal.md) failure text. Each benchmark point also writes `summary.shareable.json`, a redacted copy without URLs, credentials, private file paths or request failure text, with engine IDs replaced by `engine-1`, `engine-2` and onward.


## Operational sequence

Follow the [deployment sequence](../Deploy.md#deployment-sequence). Measure capacity again after you change the engine build, or the queueing, concurrency, retry, KV handoff timeout or byte-limit settings.
