# `narwhal-attest`

`narwhal-attest` is an HTTP sidecar that serves the attestation document of one vLLM engine.

Start conditions:

| Condition | Value |
| --- | --- |
| Engine state | vLLM running |
| Sidecar user for a container engine | root |
| Sidecar user for other engines | The engine's user |

Identity checks:

| Condition | Result |
| --- | --- |
| At startup, the engine's vLLM version differs from the attestation document | Exit status 1 |
| While the sidecar runs, the engine's vLLM version or process start time changes | Every route returns HTTP 503 |

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

| Install | Contract tool command |
| --- | --- |
| Checkout | `tools/deployment/attestation_contract.py serve` |
| Installed package | `python -m narwhal.deployment.attestation_contract serve` |

## Residency

With `--kv-events` set, the sidecar serves a bounded index of prefix blocks on the engine's GPU.

### Routes

| Route | Returns |
| --- | --- |
| `GET /v1/residency` | A snapshot |
| `GET /v1/residency/events?after=N` | The sidecar's `epoch`, the last applied `sequence`, and the `changes` applied after sequence `N` |

Snapshot fields:

| Level | Fields |
| --- | --- |
| Top level | `known`, `reason`, `sequence`, `block_size`, `groups`, `epoch`, `process_start_time_seconds` |
| Each `groups` entry | `group`, `kind`, `sliding_window`, block `identities`, and the count of `unnamed` blocks |

The events route returns HTTP 410 when any of these holds:

- residency is unknown
- `N` is ahead of the last applied sequence
- the oldest change in the bounded change log is later than `N + 1`

On HTTP 410, resume from a new snapshot.

| Sidecar state | Both routes return |
| --- | :---: |
| Residency off (`--kv-events` unset) | HTTP 404 |
| Engine process changed | HTTP 503 |

### When residency is known

Residency is known when the sidecar has applied every event batch since one of these starting points:

- the engine's first batch, at sequence 0, live or through replay
- an empty replay buffer at subscription
- a cache reset

Residency becomes unknown when any of these happens:

- a sequence gap remains after replay
- the first batch arrives after sequence 0
- a batch fails to decode
- the index grows past 1,000,000 blocks
- replay or subscription fails

A snapshot with unknown residency has an empty `groups` list.

To restore residency, reset the engine's prefix cache.

### Block identities

Parent value of each block identity:

| Block | Parent value |
| --- | --- |
| First block | The block size and the cache namespace |
| Every later block | The previous block's identity |

The cache namespace holds:

- the served model name
- the engine contract fingerprint
- the LoRA adapter name
- the request cache salt

| Term | Meaning |
| --- | --- |
| Boundary group | A group holding only boundary state, such as Mamba state in vLLM's `align` mode |
| `identities` in a boundary group | Names taken from complete groups stored in the same run |
| `unnamed` count | Blocks keyed by multimodal or prompt-embedding hashes |

### Reusable prefixes

A prefix is reusable when every KV cache group holds the blocks its kind requires:

| Group kind | Required blocks |
| --- | --- |
| Full attention | Every leading block |
| Sliding window | The blocks covering the window before the prefix end |
| Boundary | The block at the prefix end |
| Other kinds, such as chunked local attention | Zero reusable prefixes |
