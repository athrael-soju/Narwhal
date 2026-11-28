---
description: narwhal-attest serves the attestation document of one engine over HTTP.
---

# `narwhal-attest`

`narwhal-attest` is an HTTP sidecar that serves one engine's attestation document, checked against the engine process it runs beside. With cache events turned on, it also serves the engine's prefix residency to the router.

## Running the sidecar

Run the sidecar while its engine runs. For a container engine, run it as a user that can run `docker inspect` on the container. For a native engine, run it as the user that launched the engine. In a deployment, the contract tool starts it from the engine's launch directory:

```bash
python -m narwhal.deployment.attestation_contract.cli serve --run runs/engine-<n>
```

The contract tool binds the sidecar to the host and port of `NARWHAL_NODE_<n>_ATTESTATION_URL`, which must be an HTTP URL ending in `/v1/attestation`. When the engine publishes no process start (llama.cpp, SGLang and TensorRT-LLM), it passes `--engine-container`, or `--engine-pid` for a native launch. In a Kubernetes engine pod it always passes `--engine-pod-container`, as [Attesting an engine pod](../deploy/05-Attest.md#attesting-an-engine-pod) describes. When the checked launch has prefix caching on and publishes cache events, it passes `--kv-events` and `--model`. When the launch sends KV through its HCA even to engines on its own host, it passes `--hca` with the launch's `UCX_NET_DEVICES`. That holds when the launch's environment file sets `UCX_SHM_DEVICES=none` and a single `DEVICE:PORT` in `UCX_NET_DEVICES`, and its `UCX_TLS` names `rc` and one of `cuda_copy` (CUDA) or `rocm_copy` (ROCm), but none of `cuda`, `cuda_ipc`, `rocm` and `rocm_ipc`. The contract tool refuses an environment file whose digest differs from the plan's `env_sha256`. It refuses to start the sidecar when the attestation document differs from the launch directory's current evidence.

At startup the sidecar reads the engine's identity, which is its version and its process start time:

| Backend | Version from | Process start from |
| --- | --- | --- |
| vLLM | `/version` | `/metrics` |
| SGLang | `/server_info` | The host process named by `--engine-container` or `--engine-pid`, or the pod process named by `--engine-pod-container` |
| TensorRT-LLM | `/version` | The host process named by `--engine-container` or `--engine-pid`, or the pod process named by `--engine-pod-container` |
| llama.cpp | `build_info` in `/props` | The host process named by `--engine-container` or `--engine-pid` |

The sidecar exits with status 1 when it cannot read the identity or the engine's version differs from the document's `engine_version`. It exits with status 2 when the document fails to load or an argument is invalid: `--kv-events` without `--model`, a backend that publishes no process start without `--engine-container`, `--engine-pid` or `--engine-pod-container`, or more than one of those options.

While the engine's version or process start time differs from startup, or cannot be read, every route except `/v1/process` returns HTTP 503. After an engine restart, restart the sidecar. With `--engine-pod-container`, the sidecar checks every second that the container's first process still runs, and stops once it has ended.

## Options

| Option                  | Default     | Description                                                                                                        |
| ----------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------ |
| `--version`             |             | Print the installed distribution version.                                                                          |
| `--document PATH`       | required    | Attestation document holding contract values and a source per field.                                               |
| `--engine-base URL`     | required    | Engine base URL that serves `/version` and `/metrics` on vLLM, `/server_info` on SGLang, `/version` on TensorRT-LLM, or `/props` on llama.cpp. |
| `--backend NAME`        | `vllm`      | Engine backend: `llamacpp`, `sglang`, `trtllm` or `vllm`.                                                          |
| `--host HOST`           | `127.0.0.1` | Address to bind.                                                                                                   |
| `--port PORT`           | `8010`      | Port to listen on.                                                                                                 |
| `--timeout-s SECONDS`   | `5.0`       | Timeout for reading the engine's identity.                                                                         |
| `--kv-events DIR`       | optional    | Engine cache-event socket directory that turns on the residency routes.                                            |
| `--model NAME`          | optional    | Served model name for block identities, required with `--kv-events`.                                               |
| `--engine-container ID` |             | Engine container whose start time identifies the engine process. Required for llama.cpp, SGLang and TensorRT-LLM unless `--engine-pid` is set. |
| `--engine-pid PID`      |             | Native engine process whose start time identifies the engine process.                                              |
| `--hca DEVICE:PORT`     | optional    | HCA port the engine sends KV through, such as `mlx5_0:1`, which turns on `/v1/hca`.                                |
| `--hca-host NAME`       | optional    | The engine's host by name, with `--hca`. An engine pod's sidecar passes its node. `/v1/hca` reports it as `host`.  |
| `--engine-pod-container ID` |         | Engine container of this pod, by runtime container ID. Its first process in the pod's shared process namespace identifies the engine process. |
| `--pod NAMESPACE/NAME`  | optional    | This engine pod, whose containers' kubelet restart counts `/metrics` exports. The contract tool passes it in an engine pod. |

## Routes

| Route                              | Serves                                                                                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `GET /health`                      | `{"status": "ok"}` once the identity check passes                                                                         |
| `GET /v1/attestation`              | The [attestation](../configuration/01-Fleet-Schema.md#attestation) for the engine's current identity                      |
| `GET /v1/process`                  | `process_start_time_seconds`, the engine process start time read on the host, with `--engine-container`, `--engine-pid` or `--engine-pod-container` |
| `GET /v1/hca`                      | `device`, `port`, the host's `boot_id`, `host` with `--hca-host`, and the port's `port_xmit_data` counter, with `--hca`; a null counter with its `counter_error` when the counter is unreadable |
| `GET /metrics`                     | `narwhal_supervisor_restarts{process="sidecar"}` under the systemd unit that `narwhal deploy` writes, and `narwhal_kubelet_restarts{container}` with `--pod`, as in [Deployment and restart metrics](../telemetry/03-Metrics-and-Control.md#deployment-and-restart-metrics) |
| `GET /v1/residency`                | A residency snapshot, with `--kv-events`                                                                                  |
| `GET /v1/residency/events?after=N` | The residency changes after sequence `N`, with `--kv-events`                                                              |

`/v1/hca` reads `/sys/class/infiniband/<device>/ports/<port>/counters/port_xmit_data`, which the kernel counts in words of 4 octets, and `/proc/sys/kernel/random/boot_id` on every request. When the counter is unreadable, it returns `port_xmit_data` as `null` and the reason in `counter_error`. It returns HTTP 503 when the boot ID is unreadable and HTTP 404 while `--hca` is unset. [Preflight](Check.md#preflight) reads it to check that KV between engines of one host crosses the HCA.

The router and `narwhal-check` read the process start of a llama.cpp, SGLang or TensorRT-LLM engine from `/v1/process`. With `--engine-pid`, the sidecar takes the process start from the engine's start ticks and a boot-time record under `/tmp/narwhal-<uid>/`. Run the sidecar as the user that launched the engine, with the same `/tmp`.

## Residency

The residency routes tell the router which prefix blocks are on the engine's GPU. The sidecar reads this from the cache events that the engine publishes while prefix caching is on. vLLM and SGLang publish these events; llama.cpp and TensorRT-LLM publish none. To turn the events off and keep prefix caching on, set [`runtime.kv_events`](../configuration/05-Engine-Launch.md#prefix-caching-and-cache-events) to `false`.
An SGLang engine stores the cache in one-token pages, so the sidecar groups the pages into blocks of 64 tokens.

### Snapshots and changes

A snapshot carries `known`, `reason`, `sequence`, `block_size`, `groups`, `epoch` and `process_start_time_seconds`. Each `groups` entry carries `group`, `kind`, `sliding_window`, the block `identities`, and the count of `unnamed` blocks. `epoch` identifies the sidecar process and changes when it restarts.

The events route returns `epoch`, the last applied `sequence`, `block_size`, `reason`, and `changes`, which holds one entry for each event batch after sequence `N`, in sequence order:

| Field      | Meaning                                                                                               |
| ---------- | ----------------------------------------------------------------------------------------------------- |
| `sequence` | The batch's sequence number                                                                           |
| `cleared`  | `true` when the batch reset the prefix cache                                                          |
| `groups`   | The changed cache groups, keyed by group, each with `kind`, `sliding_window`, `stored`, and `removed` |
| `stored`   | Identities the batch made resident in the group                                                       |
| `removed`  | Identities the batch evicted from the group                                                           |

To follow residency, read a snapshot and then poll the events route from its `sequence`. The events route returns HTTP 410 when residency is unknown or replaying, when `N` is ahead of the last applied sequence, or when the bounded change log starts later than `N + 1`; on HTTP 410, read a new snapshot. Both routes return HTTP 404 while `--kv-events` is unset.

### When residency is known

Residency is known while the sidecar has applied every event batch since one of these starting points: the engine's first batch at sequence 0, live or through replay; an empty replay buffer at subscription; or a cache reset.

Residency becomes unknown, with an empty `groups` list and a `reason` that names the cause, when any of these happens:

- a sequence gap remains after replay
- the first batch arrives after sequence 0
- a batch fails to decode
- cache groups report different block sizes
- the index grows past 1,000,000 blocks
- the index tracks more than 4,000,000 SGLang pages
- replay or subscription fails

The sidecar replays the engine's buffered history from sequence 0 when it starts. While a replay runs, snapshots report `"known": false` with the reason `replaying buffered history`.

vLLM and SGLang each keep their last 10k event batches for replay, so a sidecar that starts or restarts after its engine has published more than 10k batches reports residency unknown. Run the sidecar for the whole life of its engine process. After a loss, an engine restart makes residency known again, and so does a cache reset: `POST /reset_prefix_cache` on a vLLM engine in development mode (`VLLM_SERVER_DEV_MODE=1`), or `POST /flush_cache` on an idle SGLang engine.

### Block identities

Each block identity chains from its parent. The first block's parent value combines the block size with the cache namespace: the served model name, the engine contract fingerprint, the LoRA adapter, which is the LoRA name on vLLM and the LoRA ID on SGLang, and the request's cache salt. Each later block takes the previous block's identity as its parent value.

On vLLM, a group whose store events cover fewer blocks than their tokens fill, such as a Mamba group, takes its `identities` from complete groups stored in the same event batch. The `unnamed` count covers blocks the sidecar cannot name: blocks whose parent is unnamed, and on vLLM, blocks whose cache keys carry inputs beyond the adapter and salt, such as multimodal or prompt-embedding hashes.

### Reusable prefixes

A prefix is reusable when every KV cache group on the GPU holds the blocks its kind requires. vLLM reports each group's kind. SGLang events carry no group or kind, so an SGLang engine has one group, with `group` and `kind` set to `null`, that follows the full-attention rule:

| Group kind                                                                          | Required blocks                                                                              |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Full attention: `full_attention`, `mla_attention`, `sink_full_attention`, or `null` | Every leading block                                                                          |
| Sliding window: `sliding_window`, `sliding_window_mla`                              | The consecutive blocks that cover the last `sliding_window - 1` tokens before the prefix end |
| Boundary: `mamba`                                                                   | The block at the prefix end                                                                  |
| Any other kind, such as chunked local attention                                     | Zero reusable blocks for every prefix on the engine                                          |
