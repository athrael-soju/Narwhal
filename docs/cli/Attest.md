---
description: narwhal-attest serves the attestation document of one engine over HTTP.
---

# `narwhal-attest`

`narwhal-attest` is an HTTP sidecar that serves one engine's attestation document, checked against the engine process it runs beside. With cache events turned on, it also serves the engine's prefix residency to the router.

## Running the sidecar

Run the sidecar while its engine runs, as root for a container engine and as the engine's user otherwise. In a deployment, the contract tool starts it from the engine's launch directory:

```bash
python -m narwhal.deployment.attestation_contract.cli serve --run runs/engine-<n>
```

The contract tool binds the sidecar to the host and port of `NARWHAL_NODE_<n>_ATTESTATION_URL`, which must be an HTTP URL ending in `/v1/attestation`. It passes `--engine-container` or `--engine-pid` for SGLang, and passes `--kv-events` and `--model` when the checked launch has prefix caching on and publishes cache events. It refuses to start the sidecar when the attestation document differs from the launch directory's current evidence.

At startup, the sidecar reads the engine's identity. It exits with status 1 when it cannot read the identity or the engine's version differs from the document, and with status 2 when the document fails to load. It then checks the identity on every request. The identity is the engine's version, from `/version` on vLLM or `/server_info` on SGLang, and its process start time, from `/metrics` on vLLM or from the host process named by `--engine-container` or `--engine-pid` on SGLang. While the engine's version or process start time differs from startup, or cannot be read, every route except `/v1/process` returns HTTP 503. After an engine restart, restart the sidecar.

## Options

| Option                  | Default     | Description                                                                                                        |
| ----------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------ |
| `--version`             |             | Print the installed distribution version.                                                                          |
| `--document PATH`       | required    | Attestation document holding contract values and a source per field.                                               |
| `--engine-base URL`     | required    | Engine base URL that serves `/version` and `/metrics` on vLLM, or `/server_info` on SGLang.                        |
| `--backend NAME`        | `vllm`      | Engine backend, `vllm` or `sglang`.                                                                                |
| `--host HOST`           | `127.0.0.1` | Address to bind.                                                                                                   |
| `--port PORT`           | `8010`      | Port to listen on.                                                                                                 |
| `--timeout-s SECONDS`   | `5.0`       | Timeout for reading the engine's identity.                                                                         |
| `--kv-events DIR`       | optional    | Engine cache-event socket directory that turns on the residency routes.                                            |
| `--model NAME`          | optional    | Served model name for block identities, required with `--kv-events`.                                               |
| `--engine-container ID` |             | Engine container whose start time identifies the engine process. Required for SGLang unless `--engine-pid` is set. |
| `--engine-pid PID`      |             | Native engine process whose start time identifies the engine process.                                              |

## Routes

| Route                              | Serves                                                                                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `GET /health`                      | `{"status": "ok"}` once the identity check passes                                                                         |
| `GET /v1/attestation`              | The [attestation](../configuration/01-Fleet-Schema.md#attestation) for the engine's current identity                      |
| `GET /v1/process`                  | `process_start_time_seconds`, the engine process start time read on the host, with `--engine-container` or `--engine-pid` |
| `GET /v1/residency`                | A residency snapshot, with `--kv-events`                                                                                  |
| `GET /v1/residency/events?after=N` | The residency changes after sequence `N`, with `--kv-events`                                                              |

The router and `narwhal-check` read an SGLang engine's process start from `/v1/process`.

## Residency

The residency routes give the router a bounded index of the prefix blocks held on the engine's GPU, built from the engine's cache events. A vLLM engine publishes events for its KV blocks while prefix caching is on, and the sidecar indexes those blocks. An SGLang engine publishes events for its radix cache unless `--disable-radix-cache` is set. On either backend, the [event setting](../configuration/05-Engine-Launch.md#prefix-caching-and-cache-events) in `runtime.extra_args` turns events off with prefix caching on. Its launcher sets one-token pages, so the sidecar groups pages into blocks, each the shortest run of pages that holds 64 tokens, and reports a block resident while the block's last page is resident.

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

The sidecar replays the engine's buffered history from sequence 0 when it starts, and when a live batch arrives before it has applied any batch. A gap in the live stream starts a replay from the first missing sequence. While a replay runs, snapshots report `"known": false` with the reason `replaying buffered history`.

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
