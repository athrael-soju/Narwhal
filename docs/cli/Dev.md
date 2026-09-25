# `narwhal`

`narwhal dev init` writes a private instance containing its model and runtime
pins, memory budget, unique ports, engine launch records and fleet config.
Four engines open as two prefill and two decode processes on the selected
GPU. Repeating `init` preserves the existing directory.

`up` checks the ports and runtime, starts each engine, captures live
attestations, profiles every split with at least one prefill and one decode
engine, and starts the router. It reports `launched`.
`verify` runs full preflight across every eligible directed KV path, checks
the profiles against current processes, sends an arithmetic request through
the router, and retains router and engine metrics before reporting `ready`.

`status` derives `starting`, `launched`, `ready`, `degraded` or `stopped` from
process ownership, HTTP health and current transfer evidence. `down` checks
the recorded boot ID and start ticks, then waits for the group's workers
through leader exit and escalates surviving owned processes to SIGKILL.
A subsequent `up` creates another run directory with fresh profiles; earlier
logs and measurements stay beside it.

```bash
narwhal dev init --model /path/to/model.gguf --model-dir /path/to/tokenizer
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

| Flag | Default | Operation |
| --- | --- | --- |
| `--instance` | `runs/dev` | Select the private instance for any subcommand. |
| `--template` | Installed reference | Supply versioned model, tokenizer, runtime, profiling and memory settings to `init`. |
| `--model` | Pinned Hugging Face cache file | Select the GGUF file matching the template checksum. |
| `--model-dir` | Pinned tokenizer cache directory | Select tokenizer and configuration files. |
| `--gpu` | Single discovered GPU | Select a physical GPU UUID. |
| `--engine-count` | Template value, four | Allocate independent engine processes. |
| `--port-base` | Template ports | Set the router port; engine HTTP, attestation and NIXL ranges start at offsets 1, 101 and 201. |
| `--gpu-memory-utilization` | Template value, 0.1 | Set each engine's vLLM memory fraction. |
| `--device-allowance` | Template value, 0.5 | Bound the aggregate observed GPU memory increase. |
| `--interface` | `"eth0"` | Select the local NIXL/UCX interface. |

Model and runtime changes belong in a custom template, selected with
`--template`. Runtime and tokenizer checksums bind the default template to
its measured GGUF loader. Model overrides require their matching template
checksums and serving limits.

Export `NARWHAL_ENGINE_API_KEY` before `up` to authenticate engine requests.
The generated fleet references that environment variable for profiling,
verification and routing; keep it set when using those commands.

Each `run-*` directory contains the fleet used by the router, effective
commands, engine logs, cache layouts, attestations, measured profiles,
whole-device VRAM samples and request journal. A `verify-*` directory adds
the preflight log, directed transfer evidence, routed response and metrics.
On startup failure, inspect the named stage's log, repair the configuration
or runtime, and run `up` again after `down` confirms teardown.
