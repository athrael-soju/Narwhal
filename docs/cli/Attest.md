---
description: narwhal-attest serves the attestation document of one vLLM engine over HTTP.
---

# `narwhal-attest`

`narwhal-attest` is an HTTP sidecar that serves the attestation document of one vLLM engine.

Start the sidecar while vLLM runs. For a container engine, run it as root. For other engines, run it as the engine's user.

At startup, the sidecar exits with status 1 when the engine's vLLM version differs from the attestation document. While the sidecar runs, every route returns HTTP 503 when the engine's vLLM version or process start time changes or becomes unreadable.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--document PATH` | required | Attestation document holding contract values and a source per field. |
| `--engine-base URL` | required | vLLM base URL that serves `/version` and `/metrics`. |
| `--host HOST` | `127.0.0.1` | Address to bind. |
| `--port PORT` | `8010` | Port to listen on. |
| `--timeout-s SECONDS` | `5.0` | Timeout for reading the engine's identity. |
| `--kv-events DIR` | optional | Engine cache-event socket directory that turns on the residency routes. |
| `--model NAME` | optional | Served model name for block identities, required with `--kv-events`. |

The contract tool's `serve` action passes `--kv-events` and `--model` when both hold:

- the checked launch has prefix caching on
- the checked launch publishes cache events

In a checkout, the contract tool command is `tools/deployment/attestation_contract.py serve`. In an installed package, it is `python -m narwhal.deployment.attestation_contract serve`.

## Residency

With `--kv-events` set, the sidecar serves a bounded index of prefix blocks on the engine's GPU.

### Routes

`GET /v1/residency` returns a snapshot. `GET /v1/residency/events?after=N` returns the sidecar's `epoch`, the last applied `sequence`, `block_size`, `reason`, and the `changes` applied after sequence `N`.

A snapshot carries `known`, `reason`, `sequence`, `block_size`, `groups`, `epoch`, and `process_start_time_seconds`. Each `groups` entry carries `group`, `kind`, `sliding_window`, block `identities`, and the count of `unnamed` blocks.

`changes` holds one entry per event batch, in sequence order:

| Field | Meaning |
| --- | --- |
| `sequence` | The batch's sequence number |
| `cleared` | `true` when the batch reset the prefix cache |
| `groups` | The changed cache groups, keyed by group, each with `kind`, `sliding_window`, `stored`, and `removed` |
| `stored` | Identities the batch made resident in the group |
| `removed` | Identities the batch evicted from the group |

The events route returns HTTP 410 when any of these holds:

- residency is unknown
- `N` is ahead of the last applied sequence
- the oldest change in the bounded change log is later than `N + 1`

On HTTP 410, resume from a new snapshot.

Both routes return HTTP 404 when residency is off (`--kv-events` unset). They return HTTP 503 when the engine identity changed or became unreadable.

### When residency is known

Residency is known when the sidecar has applied every event batch since one of these starting points:

- the engine's first batch, at sequence 0, live or through replay
- an empty replay buffer at subscription
- a cache reset

Residency becomes unknown when any of these happens:

- a sequence gap remains after replay
- the first batch arrives after sequence 0
- a batch fails to decode
- cache groups report different block sizes
- the index grows past 1,000,000 blocks
- replay or subscription fails

A snapshot with unknown residency has an empty `groups` list.

The sidecar replays buffered history from sequence 0 at sidecar start, and when a live batch arrives before the sidecar applies any batch. A gap in the live event stream starts a replay at the first missing sequence. During a replay, a snapshot reports `"known": false` with the reason `replaying buffered history`.

On vLLM in development mode (`VLLM_SERVER_DEV_MODE=1`), a prefix-cache reset through `POST /reset_prefix_cache` or an engine restart restores residency. On every other vLLM engine, an engine restart restores it.

### Block identities

The first block's parent value is the block size and the cache namespace. Every later block takes the previous block's identity as its parent value.

The cache namespace holds:

- the served model name
- the engine contract fingerprint
- the LoRA adapter name
- the request cache salt

A boundary group holds only boundary state, such as Mamba state in vLLM's `align` mode. Its `identities` are names taken from complete groups stored in the same run. The `unnamed` count covers blocks keyed by multimodal or prompt-embedding hashes.

### Reusable prefixes

A prefix is reusable when every KV cache group holds the blocks its kind requires:

| Group kind | Required blocks |
| --- | --- |
| Full attention | Every leading block |
| Sliding window | The blocks covering the window before the prefix end |
| Boundary | The block at the prefix end |
| Other kinds, such as chunked local attention | Zero reusable prefixes |

### Residency limits

A sidecar that starts or restarts after the engine publishes more than 10,000 batches reports residency unknown.

After a KV handoff over NIXL for a hybrid attention and Mamba model, the receiving engine holds the prompt's attention blocks, zero Mamba state, and zero reusable blocks for that prefix.

Residency tracking requires:

- a sidecar that runs for the life of its engine process
- every request reaching the engine through the Narwhal router
