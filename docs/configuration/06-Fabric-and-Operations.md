---
description: Fabric qualification, CLI precedence, request journal and configuration provenance settings in Narwhal.
---

# Fabric, CLI, and configuration operations

## Fabric workload qualification

[`narwhal deploy fabric`](../cli/Deploy.md#fabric-qualification) sizes the budgets and measures the edges from the workstation. It reads the [fabric inputs](04-Deployment-Inputs.md#fabric-qualification-inputs), the installed package and the hosts, and writes every record on this page into its output directory at mode 0600.

### Budget

Each cache group has one `budget.json`, `fabric-budget/<representative>/budget.json`. It holds:

- the SHA-256 of the representative's model configuration and launch record, which sit beside it as `model-config.json` and `engine-launch.json`
- the TP size, the prompt length and the payload per handoff
- the declared workload: `peak_handoffs_per_s`, `burst_handoffs`, `transfer_budget_s` and `headroom`
- `required_gbps`, the total rate over all TP ranks that one directed host edge must carry for the group
- `cache_group`, the group's roles, representative first

On vLLM and TensorRT-LLM, the payload is the padded-page bound of the captured `cache-layout.json`, copied beside the budget. The budget records its SHA-256, the image identity and the launch-plan hash. The layout must name this model configuration and launch record. On vLLM, every TP rank must resolve one KV cache layout, which the budget records as `kv_cache_layout`.

On SGLang, the payload is the mean measured handoff of the representative's `transfer.json`, schema `narwhal.kv-transfer-measurement` version 1, from [`narwhal deploy transfer-measure`](../deploy/04-Qualify-Fabric.md#measuring-kv-handoffs). The packaged measurement tool, `transfer_measure.py`, runs on each engine's host. It puts the engine's decode peer in the decode role, switching it out and back first when it already serves decode, sends eight handoffs of `NARWHAL_FABRIC_PROMPT_TOKENS` tokens, and returns the peer to the role it found. `transfer.json` holds the metric series, the SHA-256 of the engine's packaged launch record, the prompt length, the payload per handoff, the handoff count, and the SHA-256 of `before.prom` and `after.prom`, which sit beside it. The budget records the SHA-256 of all three, and `fabric-budget/<representative>/` keeps copies of them.

### Link evidence

Each directed sample has a directory, `fabric-edge/<source-host>-to-<destination-host>/<sample>/`, named after the first source and destination role it serves, such as `engine-1-to-engine-2`.

| File | Contents |
| --- | --- |
| `source.json`, `destination.json` | Each host's tool version, its route to the other host, and on RDMA the GID table entry |
| `link.json` | The link record |
| `sample.json` or `sample.txt` | The `iperf3` JSON of a `ucx_tcp` sample, or the `ib_write_bw` report of a `ucx_rdma` sample |
| `<role>.evidence.json` | The sample against the budget of each source role, schema `narwhal.fabric-edge-evidence` version 2 |
| `retained/` | For a reused sample, the retained sample and the evidence it was recorded with |
| `<role>.comparison.json` | For a reused sample, the retained rate against each source role's current budget, schema `narwhal.fabric-edge-comparison` version 2 |

The link record holds both hosts, addresses and interfaces, both routes, the transport, both hosts' tool versions and the test parameters: the port, the stream count or the HCA ports, GID indexes, GIDs and RoCE mode, the message size and the durations. A route keeps only `dst`, `from`, `gateway`, `via`, `dev`, `prefsrc` and `table`, so a route cache timer does not change it. The link fingerprint is the SHA-256 of the record's canonical JSON.

Evidence and comparisons hold the source role, the link fingerprint, the SHA-256 of the sample and the budget, `measured_gbps`, `required_gbps` and `passed`. A comparison also holds the SHA-256 of the evidence it reuses. For `ucx_tcp`, the measured rate is the receiver's aggregate `end.sum_received.bits_per_second`. For `ucx_rdma`, it is the report's `BW average[Gb/sec]` column.

`fabric.json` lists the input digests, the budget of each group, and every sample with its roles, link fingerprint, measured rate, whether it was reused, and each source role's required rate and verdict.

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
