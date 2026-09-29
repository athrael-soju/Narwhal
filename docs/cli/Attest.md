# `narwhal-attest`

`narwhal-attest` is a sidecar that runs alongside vLLM. It reads the engine's identity and serves the attestation document over HTTP. Start it after vLLM is up, and run it as the same user as the engine (root, for container engines).

When it starts, it checks the engine's vLLM version against the one in the attestation document and exits with status 1 if they don't match. After that it keeps watching. If the version or the engine's process start time changes, the sidecar stays up but answers every route with HTTP 503.

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

You don't have to assemble these flags yourself. The contract tool's `serve` action passes both whenever the checked launch has prefix caching on and publishes cache events. Run it from a checkout with `tools/deployment/attestation_contract.py serve`, or from an installed package with `python -m narwhal.deployment.attestation_contract serve`.

## Residency

With `--kv-events` set, the sidecar subscribes to the engine's cache events and keeps a bounded index of the prefix blocks currently held on the engine's GPU. A router can read that index to see which prompts an engine can already serve from cache.

### Routes

`GET /v1/residency` returns a snapshot. At the top level you get `known`, `reason`, `sequence`, `block_size`, `epoch`, and `process_start_time_seconds`. Each KV cache group then lists its `kind`, `sliding_window`, block `identities`, and a count of `unnamed` blocks.

`GET /v1/residency/events?after=N` returns the changes applied after sequence `N`, along with the sidecar's `epoch`. It answers HTTP 410 if residency is unknown, if `N` is ahead of the last applied sequence, or if the bounded change log no longer reaches back to `N + 1`. On a 410, fetch a new snapshot and resume from there.

Without `--kv-events`, both routes return 404. A router that sees a 404 assumes the engine's prefix cache is empty, which we call pricing the engine cold. After the engine process changes, both routes return 503.

### When residency is known

Residency counts as known only once the sidecar has applied every event batch since a known starting point. There are three such points:

- the engine's first batch, at sequence 0, whether it arrives live or through replay
- an empty replay buffer at subscription, which starts the index empty
- a cache reset

Residency becomes unknown, and every group's block list is cleared, if any of these happen:

- a sequence gap remains after replay
- the first batch arrives after sequence 0
- a batch fails to decode
- the index grows past 1,000,000 blocks
- replay or subscription fails

It stays unknown until the engine resets its prefix cache.

### Block identities

A block's identity is built by chaining its token IDs onto the identity of the block before it. The first block chains from the block size and the cache namespace, which is made up of the served model name, the engine contract fingerprint, the LoRA adapter name, and the request cache salt.

Boundary groups store only boundary state (Mamba state in vLLM's `align` mode, for example). They borrow their identities from a group that reported every block in the same run. Blocks keyed by multimodal or prompt-embedding hashes can't be named this way, so they're counted in the group's `unnamed` total instead.

### Reusable prefixes

A prefix is reusable only if every KV cache group can serve it. What each group needs depends on its kind:

- **Full attention:** every leading block.
- **Sliding window:** the blocks covering the window before the prefix end.
- **Boundary:** the block at the prefix end.
- **Anything else** (chunked local attention, for example): the router prices the engine cold.
