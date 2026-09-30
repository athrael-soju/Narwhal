# `narwhal-attest`

`narwhal-attest` is a sidecar for one vLLM engine. It reads the engine's identity and serves the attestation document over HTTP. Start it after vLLM is up, as the engine's user (root for container engines).

Identity checks:

| Condition                                                                       | Result                       |
| ------------------------------------------------------------------------------- | ---------------------------- |
| At startup, the engine's vLLM version differs from the attestation document     | Exit status 1                |
| While the sidecar runs, the engine's vLLM version or process start time changes | Every route returns HTTP 503 |

## Options

| Option                | Default     | Description                                                              |
| --------------------- | ----------- | ------------------------------------------------------------------------ |
| `--version`           |             | Print the installed distribution version.                                |
| `--document PATH`     | required    | The attestation document: contract values, plus a source for each field. |
| `--engine-base URL`   | required    | vLLM base URL. The sidecar queries `/version` and `/metrics`.            |
| `--host HOST`         | `127.0.0.1` | Address to bind.                                                         |
| `--port PORT`         | `8010`      | Port to listen on.                                                       |
| `--timeout-s SECONDS` | `5.0`       | How long to wait when reading the engine's identity.                     |
| `--kv-events DIR`     |             | Directory with the engine's cache-event sockets. Turns on residency.     |
| `--model NAME`        |             | Served model name, used in block identities.                             |

`--kv-events` requires `--model`.

The contract tool's `serve` action passes `--kv-events` and `--model` when the checked launch has prefix caching on and publishes cache events.

| Install           | Contract tool command                                     |
| ----------------- | --------------------------------------------------------- |
| Checkout          | `tools/deployment/attestation_contract.py serve`          |
| Installed package | `python -m narwhal.deployment.attestation_contract serve` |

## Residency

With `--kv-events` set, the sidecar builds a bounded index of the prefix blocks on the engine's GPU from the engine's cache events. A router reads the index to find which prompts an engine can serve from cache.

### Routes

`GET /v1/residency` returns a snapshot:

| Level               | Fields                                                                             |
| ------------------- | ---------------------------------------------------------------------------------- |
| Top level           | `known`, `reason`, `sequence`, `block_size`, `epoch`, `process_start_time_seconds` |
| Each KV cache group | `kind`, `sliding_window`, block `identities`, and the count of `unnamed` blocks    |

`GET /v1/residency/events?after=N` returns the changes applied after sequence `N` and the sidecar's `epoch`. It returns HTTP 410 when any of these holds:

- residency is unknown;
- `N` is ahead of the last applied sequence;
- the oldest change in the bounded change log is later than `N + 1`.

On HTTP 410, fetch a new snapshot and resume from it.

| Sidecar state                       | Both routes return |
| ----------------------------------- | ------------------ |
| Residency off (`--kv-events` unset) | HTTP 404           |
| Engine process changed              | HTTP 503           |

Pricing an engine cold means treating its prefix cache as empty. A router prices the engine cold on HTTP 404.

### When residency is known

Residency is known when the sidecar has applied every event batch since a starting point. The starting points are:

- the engine's first batch, at sequence 0, live or through replay
- an empty replay buffer at subscription, which starts the index empty
- a cache reset

Residency becomes unknown, and every group's block list is cleared, when any of these happens:

- a sequence gap remains after replay
- the first batch arrives after sequence 0
- a batch fails to decode
- the index grows past 1,000,000 blocks
- replay or subscription fails

Residency stays unknown until the engine resets its prefix cache.

### Block identities

A block's identity chains its token IDs onto the identity of the previous block. The first block chains from the block size and the cache namespace. The cache namespace is the served model name, the engine contract fingerprint, the LoRA adapter name, and the request cache salt.

Boundary groups hold only boundary state, such as Mamba state in vLLM's `align` mode. A boundary group takes its identities from a group that reported every block in the same run. The group's `unnamed` count holds blocks keyed by multimodal or prompt-embedding hashes.

### Reusable prefixes

A prefix is reusable when every KV cache group can serve it. Each group kind needs these blocks:

| Group kind                                   | Required blocks                                      |
| -------------------------------------------- | ---------------------------------------------------- |
| Full attention                               | Every leading block                                  |
| Sliding window                               | The blocks covering the window before the prefix end |
| Boundary                                     | The block at the prefix end                          |
| Other kinds, such as chunked local attention | The router prices the engine cold                    |
