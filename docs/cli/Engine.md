# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM engines from a `narwhal.engine-launch` record.

| Backend     | Runtime                                          |
| ----------- | ------------------------------------------------ |
| `native`    | The checked Python environment on Linux or WSL2. |
| `container` | Docker.                                          |

| Variable                 | Use                                       |
| ------------------------ | ----------------------------------------- |
| `NARWHAL_MODEL_REVISION` | Required for the native backend.          |
| `NARWHAL_MODEL_PATH`     | Local GGUF file (SHA-256 recorded by `prepare`). |

GGUF models:

- Pin `vllm-gguf-plugin` in `runtime.expected_packages`.
- Keep the model path inside its snapshot directory.
- Pass the base model tokenizer and configuration through `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

## Actions

| Action               | Backend           | Inputs and operation |
| -------------------- | ----------------- | -------------------- |
| `prepare`            | container, native | Write a fresh launch directory (`launch.json`, backend environment) from `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md) after verifying model and hook hashes. |
| `check`              | container, native | Write `checked.json` and the backend check log entry after checking the plan's pinned packages, model, tokenizer, and NIXL connector. |
| `measure-cache`      | container         | Write `cache-layout.json` by sizing the cache in a temporary container from a checked, unused plan before `start`. |
| `model-dimensions`   | container         | Inspect the model through the checked runtime and write `model-dimensions.json`. |
| `handshake-policy`   | container, native | Inspect the installed NIXL worker against the checked connector settings and write `handshake-policy.json`. |
| `start`              | container         | Start one serving container from a checked plan and record `container.id`. |
| `capture-cache`      | container         | Retain the running container's live cache pages in `cache-layout.json` from a checked launch directory. |
| `cache-registration` | container, native | Write `cache-registration.json` with block grouping resolved from the checked runtime and exactly one of `--startup-log` or `--runtime-layout`. |
| `start-shared`       | container, native | Start two to eight checked plans sharing one GPU sequentially and write readiness, identity, and memory readings to each `shared-start.json`. |
| `stop-native`        | native            | Write `native-stop.json` after stopping the recorded process groups (boot ID and process start ticks validated, SIGKILL for survivors). |

`start` returns when Docker starts the container.

Poll HTTP readiness when `start` returns.

Actions write their outputs to the launch directory.

To start again or capture an output again:

1. Run `prepare` with a fresh `--out` directory.
2. Run `check` on it.

| Option             | Default     | Description |
| ------------------ | ----------- | ----------- |
| `--version`        | optional    | Print the installed distribution version. |
| `--format`         | `text`      | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--backend`        | `container` | Launch backend for `prepare` and `start-shared`, either `container` or `native`. |
| `--out`            | required    | Fresh launch directory for `prepare`. |
| `--run`            | required    | Existing launch directory for every action except `prepare`, repeated two to eight times for `start-shared`. |
| `--ready-seconds`  | `180`       | Positive integer seconds allowed per engine for readiness and identity checks during `start-shared`. |
| `--startup-log`    | optional    | Serving log with one resolved KV layout, read by `cache-registration`. |
| `--runtime-layout` | optional    | Captured runtime cache-layout JSON, read by `cache-registration`. |

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

Both backends apply the same memory check:

| Property       | Value |
| -------------- | ----- |
| Baseline       | The first prelaunch GPU reading |
| Check point    | Each engine passing its readiness and identity checks |
| Measured value | Whole-device memory increase over the baseline, counting other processes' allocations and frees during startup |
| Limit          | `shared_device.device_allowance` times total device memory |
| Record         | Each ready engine's `shared-start.json` |

Each `shared-start.json` has these fields, in MiB:

- Baseline
- Before and after readings
- Per-engine increase
- Aggregate increase
- Allowance

Recorded identity per backend:

- `container` records the container ID, Linux PID, image ID, and serving arguments.
- `native` records the Linux PID, boot ID, process start tick, vLLM version, `/metrics` process start, model revision, and arguments.

`container` on failure:

- Removes created containers, newest first.
- Marks each created container's `shared-start.json` failed, with the startup cause and `cleanup_status`.
- Adds a `cleanup_error` entry to the record and the command error when removal fails.

`native` on failure:

- Stops the current process and every ready engine.
- Keeps the startup cause, cleanup errors, and post-cleanup GPU reading in the failing engine's `shared-start.json`.
- Records the stop of each ready engine in `native-stop.json`.

Native port checks cover the HTTP endpoint in `launch.json` and the NIXL address in `engine.env`.

`NARWHAL_NODE_<n>_ATTESTATION_URL` sets the sidecar address checked at preparation and shared startup:

| Context        | Source |
| -------------- | ------ |
| `prepare`      | Environment at preparation |
| `start-shared` | Environment at startup, overriding the preparation value |
| `narwhal dev`  | Instance fleet configuration |

Serve the process-bound attestation that `native-capture` writes from the recorded engine environment:

1. Set `NARWHAL_NODE_<n>_ATTESTATION_URL`.
2. Run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>`.

When starting engines through Windows OpenSSH, keep a WSL terminal open for the fleet's lifetime.
