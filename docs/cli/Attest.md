# `narwhal-attest`

Start `narwhal-attest` after vLLM. The sidecar reads one engine's identity, serves its attestation over HTTP, and stops when the engine version or process start time changes.

| Option                | Default     | Contract                                                |
| --------------------- | ----------- | ------------------------------------------------------- |
| `--version`           |             | Print the installed distribution version.               |
| `--document PATH`     | required    | Contract values plus a source for every populated field |
| `--engine-base URL`   | required    | vLLM base URL queried at `/version` and `/metrics`      |
| `--host HOST`         | `127.0.0.1` | Sidecar bind address                                    |
| `--port PORT`         | `8010`      | Sidecar port                                            |
| `--timeout-s SECONDS` | `5.0`       | Time budget for reading engine identity                 |
| `--kv-events DIR`     | omitted     | Directory holding the engine's cache-event sockets; serves the residency routes |
| `--model NAME`        | omitted     | Served model name for block identities; required with `--kv-events` |

`tools/deployment/attestation_contract.py serve` passes `--kv-events` and `--model` when the checked launch keeps prefix caching on and publishes cache events.

## Residency routes

With `--kv-events`, the sidecar subscribes to the engine's cache events. It keeps a bounded index of the prefix blocks that the engine process holds on its GPU. Without `--kv-events`, both routes answer HTTP 404, and the router prices the engine cold.

| Route | Response |
| --- | --- |
| `GET /v1/residency` | Snapshot: `known`, `reason`, `sequence`, `block_size`, `epoch`, `process_start_time_seconds`, and each KV cache group's `kind` and named block `identities` |
| `GET /v1/residency/events?after=N` | Ordered changes after sequence `N`, with the sidecar `epoch`; HTTP 410 when the sidecar no longer holds them |

Both routes answer HTTP 503 after the engine process changes.

The sidecar knows the engine's residency only after it has applied every event batch since a known state. It reaches a known state in three ways:

- replaying the engine's history from sequence 0;
- observing that the engine has published no batch;
- applying a cache reset.

Residency is unknown in these cases, and a snapshot then lists no blocks:

- a sequence gap that replay cannot fill;
- a subscription that starts after vLLM's replay buffer dropped earlier batches;
- an unreadable batch;
- an index larger than 1,000,000 blocks.

Unknown residency lasts until the engine resets its prefix cache. While the sidecar replays buffered history, including after its own restart, a snapshot reports `"known": false` with the reason `replaying buffered history`.

A block identity chains the block's token IDs onto the identity of the block before it. The first block chains from the block size and the cache namespace. The namespace holds the served model name, the engine contract fingerprint, the LoRA adapter name, and the request's cache salt. Some groups keep only boundary state, such as Mamba state in vLLM's `align` mode. They take each block identity from a group that reported every block in the same run.

A prefix is reusable when every KV cache group can serve it. Full-attention groups must hold every leading block. Sliding-window groups must hold the blocks that cover the window before the prefix end. Boundary groups must hold the block at the prefix end. The router prices an engine cold when it has a group of any other kind, such as chunked local attention. Blocks carrying multimodal or prompt-embedding hash keys have no identity.

### Residency limits

vLLM keeps no residency snapshot. Its replay buffer holds the latest 10,000 event batches. A sidecar that starts, or restarts, after the engine has published more than that loses the engine's early history and reports residency unknown. Residency returns after a prefix-cache reset or an engine restart. vLLM serves its cache-reset route only in development mode, so a production engine needs a restart. Keep the sidecar running for the life of its engine process.

With a hybrid attention and Mamba model, an engine can receive a prompt's KV cache over NIXL. It then reports the prompt's attention blocks but no Mamba state. The reuse rule counts zero blocks for that prefix, and the engine cannot reuse it.

Container engines run as root, so their event sockets belong to root. Run the sidecar as the same user as the engine.
