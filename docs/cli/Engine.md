# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM engines from a `narwhal.engine-launch` record. Both backends run the same model, GPU allocation, port, NIXL connector, and runtime argument checks.

| Backend     | Runtime                                          |
| ----------- | ------------------------------------------------ |
| `native`    | The checked Python environment on Linux or WSL2. |
| `container` | Docker.                                          |

| Variable                 | Use                                                                                       |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| `NARWHAL_MODEL_REVISION` | Required for the native backend.                                                          |
| `NARWHAL_MODEL_PATH`     | Local GGUF file. `prepare` records its SHA-256.                                           |

Run directories are immutable. Each fresh start uses a new directory.

GGUF models:

- Pin `vllm-gguf-plugin` in `runtime.expected_packages`.
- Keep the model path inside its snapshot directory.
- Pass the base model tokenizer and configuration through `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

## Actions

| Action               | Backend           | Inputs and operation                                                                                                                                                                                                                              |
| -------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prepare`            | container, native | Read `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md), verify model and hook hashes, and write a fresh launch directory with `launch.json` and the backend environment.                                  |
| `check`              | container, native | Check the plan's pinned packages, model, tokenizer, and NIXL connector, resolve the prefix-caching and cache-event settings, and bind `checked.json` to the plan. Repeated checks append to the backend check log.                                |
| `measure-cache`      | container         | Size the cache in a temporary container started from a checked, unused plan, and write `cache-layout.json`.                                                                                                                                       |
| `model-dimensions`   | container         | Inspect the model through the checked runtime and write `model-dimensions.json`.                                                                                                                                                                  |
| `handshake-policy`   | container, native | Inspect the installed NIXL worker against the checked connector settings and write `handshake-policy.json`.                                                                                                                                       |
| `start`              | container         | Start one serving container from a checked plan and record `container.id`. Returns when Docker starts the container. Poll HTTP readiness yourself.                                                                                                |
| `capture-cache`      | container         | Read the running container recorded in a checked launch directory and retain its live cache pages in `cache-layout.json`.                                                                                                                         |
| `cache-registration` | container, native | Resolve block grouping from the checked runtime and one of a serving startup log or a captured runtime layout, and write `cache-registration.json`.                                                                                                |
| `start-shared`       | container, native | Validate two to eight checked plans sharing one GPU, start them sequentially, and retain readiness, identity, and memory readings in each `shared-start.json`.                                                                                    |
| `stop-native`        | native            | Validate the recorded boot ID and process start ticks, stop the recorded process groups, wait for their workers, send SIGKILL to survivors, and write `native-stop.json`.                                                                        |

Outputs stay in the launch directory. To capture an output again, prepare and check a fresh directory.

| Option             | Default     | Description                                                                                                |
| ------------------ | ----------- | ---------------------------------------------------------------------------------------------------------- |
| `--version`        | optional    | Print the installed distribution version.                                                                  |
| `--format`         | `text`      | `text` or `json`. JSON output follows [versioned command results](../Command-Results.md).                  |
| `--backend`        | `container` | Launch backend for `prepare` and `start-shared`, either `container` or `native`.                           |
| `--out`            | required    | Fresh launch directory for `prepare`.                                                                      |
| `--run`            | required    | Existing launch directory for every action except `prepare`. Repeat two to eight times for `start-shared`. |
| `--ready-seconds`  | `180`       | Positive integer seconds allowed per engine for readiness and identity checks during `start-shared`.       |
| `--startup-log`    | optional    | Serving log with one resolved KV layout, read by `cache-registration`.                                     |
| `--runtime-layout` | optional    | Captured runtime cache-layout JSON, read by `cache-registration`.                                          |

- `start-shared` requires every selected plan to use the chosen `--backend`. Other actions read the backend from the plan itself.
- `cache-registration` takes one of `--startup-log` or `--runtime-layout`.

## Shared-GPU startup

Native shared-GPU example:

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

`start-shared` launches the engines sequentially. Every selected plan must meet these conditions:

- The plans share one group, GPU UUID, and device allowance.
- Each plan has a unique role and port.
- The per-engine `gpu_memory_utilization` fractions sum to at most `shared_device.device_allowance`.
- Each plan's CUDA device resolves to `shared_device.gpu_uuid`. The checked runtime evaluates ordinals and UUID prefixes.

Both backends apply the same memory check:

| Property      | Value                                                                                                                                  |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Baseline      | The first prelaunch GPU reading                                                                                                        |
| Check point   | Each engine passing its readiness and identity checks                                                                                  |
| Measured value | Whole-device memory increase over the baseline, including allocations and frees by other processes during startup                     |
| Limit         | `shared_device.device_allowance` times total device memory                                                                             |
| Record        | Each ready engine's `shared-start.json` keeps the baseline, the before and after readings, the per-engine and aggregate increases, and the allowance in MiB |

Backend records and failure handling:

| Property           | `container`                                                                                                  | `native`                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| Recorded identity  | Container ID, Linux PID, image ID, and serving arguments                                                     | Linux PID, boot ID, process start tick, vLLM version, `/metrics` process start, model revision, and arguments                    |
| Cleanup on failure | Removes the containers it created, newest first                                                              | Stops the current process and every engine that was already ready                                                                |
| Failure record     | Each created container's `shared-start.json` is marked failed, with the startup cause and a `cleanup_status` | The failing engine's `shared-start.json` keeps the startup cause, cleanup errors, and post-cleanup GPU reading                   |
| Failed removal     | Adds a `cleanup_error` entry to the record and to the command error                                          |                                                                                                                                  |
| Stop record        |                                                                                                              | `native-stop.json` records the stops of the engines that were ready                                                              |

Native port checks bind the HTTP endpoint from `launch.json` and the NIXL address from `engine.env`.

`NARWHAL_NODE_<n>_ATTESTATION_URL` sets the sidecar address checked at preparation and shared startup:

| Context        | Source                                             |
| -------------- | -------------------------------------------------- |
| `prepare`      | Environment at preparation                         |
| `start-shared` | Environment at startup, overriding the preparation value |
| `narwhal dev`  | Instance fleet configuration                       |

`native-capture` runs Narwhal's model, cache, NIXL, and handshake checks in the recorded engine environment and writes a process-bound attestation. Serve it:

1. Set `NARWHAL_NODE_<n>_ATTESTATION_URL`.
2. Run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>`.

When starting engines through Windows OpenSSH, keep a WSL terminal open for the fleet's lifetime.
