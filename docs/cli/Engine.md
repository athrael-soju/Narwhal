---
description: Prepare and run vLLM engines from engine launch records with narwhal-engine.
---

# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM engines from a `narwhal.engine-launch` record.

The `native` backend runs in the checked Python environment on Linux or WSL2. The `container` backend runs in Docker.

The `native` backend uses two variables. `NARWHAL_MODEL_REVISION` holds the required model revision, a 40-character commit or a `sha256:` digest. `NARWHAL_MODEL_PATH` names a local GGUF file, and `prepare` records its SHA-256.

GGUF models:

- Pin `vllm-gguf-plugin` in `runtime.expected_packages`.
- Keep the model path inside its snapshot directory.
- Pass the base model tokenizer and configuration through `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

## Actions

Actions write their outputs to the launch directory.

| Action | Backend | Input | Output |
| --- | --- | --- | --- |
| `prepare` | container, native | `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md) | Fresh launch directory with `launch.json` and the backend environment file |
| `check` | container, native | Prepared launch directory | `checked.json` and `image-check.log` or `runtime-check.log` |
| `measure-cache` | container | Checked plan before `start` | `cache-layout.json` from a temporary sizing container |
| `model-dimensions` | container | Checked plan | `model-dimensions.json` |
| `handshake-policy` | container, native | Checked plan and the installed NIXL worker | `handshake-policy.json` |
| `start` | container | Checked plan | One serving container and `container.id` |
| `capture-cache` | container | Running container of a checked plan | `cache-layout.json` with the live cache pages |
| `cache-registration` | container, native | Checked plan and exactly one of `--startup-log` or `--runtime-layout` | `cache-registration.json` with the resolved block grouping |
| `start-shared` | container, native | Two to eight checked plans on one GPU | Running engines and one `shared-start.json` per engine |
| `stop-native` | native | Native launch directory with process records | `native-stop.json` |

Four actions run checks:

| Action | Checks |
| --- | --- |
| `prepare` | Model configuration and cache-capture hook hashes |
| `check` | Pinned packages, model, tokenizer, and NIXL connector |
| `start-shared` | Readiness, identity, and memory allowance of each engine |
| `stop-native` | Boot ID and process start ticks of each recorded process group, with SIGKILL for survivors |

Poll HTTP readiness when `start` returns.

To start again or capture an output again:

1. Run `prepare` with a fresh `--out` directory.
2. Run `check` on it.

`narwhal-engine` options:

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--backend` | `container` | Launch backend for `prepare` and `start-shared`, either `container` or `native`. |
| `--out` | required | Fresh launch directory for `prepare`. |
| `--run` | required | Existing launch directory for every action except `prepare`, repeated two to eight times for `start-shared`. |
| `--ready-seconds` | 180 | Positive integer seconds allowed per engine for readiness and identity checks during `start-shared`. |
| `--startup-log` | optional | Serving log with one resolved KV layout, read by `cache-registration`. |
| `--runtime-layout` | optional | Captured runtime cache-layout JSON, read by `cache-registration`. |

## Shared-GPU startup

Native shared-GPU example:

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

Every plan selected for `start-shared` must meet these conditions:

- The plan uses the chosen `--backend`.
- The plans share one group, GPU UUID, and device allowance.
- Each plan has a unique role and port.
- The per-engine `gpu_memory_utilization` fractions sum to at most `shared_device.device_allowance`.
- Each plan's CUDA device, as an ordinal or UUID prefix, resolves to `shared_device.gpu_uuid` in the checked runtime.

Both backends apply the same memory check. Its baseline is the first prelaunch GPU reading. When each engine passes its readiness and identity checks, the check measures the whole-device memory increase over the baseline, counting other processes' allocations and frees during startup.

The limit is `shared_device.device_allowance` times total device memory. Each ready engine's `shared-start.json` records the check with these fields, in MiB:

- Baseline
- Before and after readings
- Per-engine increase
- Aggregate increase
- Allowance

For each engine's identity, the `container` backend records the container ID, Linux PID, image ID, and serving arguments. The `native` backend records the Linux PID, boot ID, process start tick, vLLM version, `/metrics` process start, model revision, and arguments.

`container` on failure:

- Removes created containers, newest first.
- Marks each created container's `shared-start.json` failed, with the startup cause and `cleanup_status`.
- Adds a `cleanup_error` entry to the record and the command error when removal fails.

`native` on failure:

- Stops the current process and every ready engine.
- Keeps the startup cause, cleanup errors, and post-cleanup GPU reading in the failing engine's `shared-start.json`.
- Records the stop of each ready engine in `native-stop.json`.

Native port checks read the engine HTTP endpoint address from `launch.json`, the NIXL side channel address from `engine.env`, and the attestation sidecar address from `NARWHAL_NODE_<n>_ATTESTATION_URL`.

`prepare` reads `NARWHAL_NODE_<n>_ATTESTATION_URL` from the environment at preparation. `start-shared` reads it from the environment at startup, overriding the preparation value. Under `narwhal dev`, it comes from the instance fleet configuration.

Serve the process-bound attestation that `native-capture` writes from the recorded engine environment:

1. Set `NARWHAL_NODE_<n>_ATTESTATION_URL`.
2. Run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>`.

When starting engines through Windows OpenSSH, keep a WSL terminal open for the fleet's lifetime.
