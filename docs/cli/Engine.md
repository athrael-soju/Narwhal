# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM engines from a `narwhal.engine-launch` record. Both backends run the same model, GPU allocation, port, NIXL connector, and runtime argument checks.

| Backend     | Runtime                                          |
| ----------- | ------------------------------------------------ |
| `native`    | The checked Python environment on Linux or WSL2. |
| `container` | Docker.                                          |

| Variable                 | Use                                                                                       |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| `NARWHAL_MODEL_REVISION` | Required for the native backend. |
| `NARWHAL_MODEL_PATH`     | Local GGUF file; `prepare` records the file's SHA-256.                                    |

Run directories are immutable, so each fresh start needs a new directory.

For GGUF models, pin `vllm-gguf-plugin` in `runtime.expected_packages`. Keep the model path inside its snapshot directory so companion files load, and pass the base model tokenizer and configuration through `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

## Actions

| Action               | Backend           | Inputs and operation                                                                                                                                                                                                                              |
| -------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prepare`            | container, native | Read `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md) and verify model and hook hashes. Write a fresh launch directory with `launch.json` and the backend environment.                                    |
| `check`              | container, native | Check the plan's pinned packages, model, tokenizer, and NIXL connector. Resolve the prefix-caching and cache-event settings, then bind `checked.json` to the plan. Repeated checks append to the backend check log. |
| `measure-cache`      | container         | Start a temporary sizing container from a checked, unused plan, write `cache-layout.json`, and then remove the container.                                                                                                                         |
| `model-dimensions`   | container         | Inspect the model through the checked runtime and write `model-dimensions.json`.                                                                                                                                                                  |
| `handshake-policy`   | container, native | Inspect the installed NIXL worker against the checked connector settings and write `handshake-policy.json`.                                                                                                                                       |
| `start`              | container         | Start one serving container from a checked plan and record `container.id`. Returns when Docker starts the container. Poll HTTP readiness yourself.                                                             |
| `capture-cache`      | container         | Read the running container recorded in a checked launch directory and retain its live cache pages in `cache-layout.json`.                                                                                                                         |
| `cache-registration` | container, native | Resolve block grouping from the checked runtime plus either a serving startup log or a captured runtime layout, then write `cache-registration.json`.                                                                                             |
| `start-shared`       | container, native | Validate two to eight checked plans sharing one GPU, start them sequentially, and retain readiness, identity, and memory readings in each `shared-start.json`.                                                                                    |
| `stop-native`        | native            | Validate the recorded boot ID and process start ticks, stop the recorded process groups, and write `native-stop.json`.                                                                                                                            |

Outputs stay in the launch directory. To capture one again, prepare and check a fresh directory.

| Option             | Default     | Description                                                                                                |
| ------------------ | ----------- | ---------------------------------------------------------------------------------------------------------- |
| `--version`        | optional    | Print the installed distribution version.                                                                  |
| `--format`         | `text`      | `text` or `json`. JSON output follows [versioned command results](../Command-Results.md).             |
| `--backend`        | `container` | Launch backend for `prepare` and `start-shared`, either `container` or `native`.                           |
| `--out`            | required    | Fresh launch directory for `prepare`.                                                                      |
| `--run`            | required    | Existing launch directory for every action except `prepare`; repeat two to eight times for `start-shared`. |
| `--ready-seconds`  | `180`       | Positive integer seconds allowed per engine for readiness and identity checks during `start-shared`.       |
| `--startup-log`    | optional    | Serving log with one resolved KV layout, read by `cache-registration`.                                     |
| `--runtime-layout` | optional    | Captured runtime cache-layout JSON, read by `cache-registration`.                                          |

- `start-shared` requires every selected plan to use the chosen `--backend`. Other actions read the backend from the plan itself.
- `cache-registration` takes one of `--startup-log` or `--runtime-layout`.

## Shared-GPU startup

Prepare, check, and start two native engines on one GPU, then capture attestation and stop.

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

`start-shared` checks every selected role, port, and GPU budget before launching the engines sequentially. The selected plans must satisfy these conditions:

- share a common group, GPU UUID, and device allowance while keeping their roles and ports unique;
- the per-engine `gpu_memory_utilization` fractions sum to no more than `shared_device.device_allowance`;
- resolve their CUDA device to `shared_device.gpu_uuid`. Ordinals and UUID prefixes are evaluated in the checked runtime.

Both backends apply the same memory check. The first prelaunch GPU reading serves as the baseline, and after each engine passes its readiness and identity checks, the whole-device memory increase must not exceed `shared_device.device_allowance` times total device memory. Other processes that allocate or free memory during startup change the observed increase. Each ready engine's `shared-start.json` keeps the baseline, the before and after readings, the per-engine and aggregate increases, and the allowance in MiB.

The container backend records each container ID, Linux PID, image ID, and serving arguments. On failure it removes the containers it created, newest first, and marks their `shared-start.json` records failed. Each failed record keeps the startup cause and a `cleanup_status`. A failed removal also adds a `cleanup_error` entry, which appears in the command error too.

The native backend records the Linux PID, boot ID, process start tick, vLLM version, `/metrics` process start, model revision, and arguments. On failure it stops the current process and every engine that was already ready. The failing engine's `shared-start.json` keeps the startup cause, cleanup errors, and post-cleanup GPU reading. `native-stop.json` records the stops of the engines that were ready.

Native port checks bind the HTTP endpoint from `launch.json` and the NIXL address from `engine.env`, the same way vLLM and NIXL bind them.

Set `NARWHAL_NODE_<n>_ATTESTATION_URL` at preparation or shared startup to check the sidecar address through Uvicorn's event loop. A startup value overrides one set at preparation, and `narwhal dev` reads it from the instance fleet configuration.

`native-capture` runs Narwhal's model, cache, NIXL, and handshake checks in the recorded engine environment and writes a process-bound attestation. To serve it, set `NARWHAL_NODE_<n>_ATTESTATION_URL` and run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>`.

`stop-native` checks the recorded process identity, waits for the group's workers, and sends SIGKILL to any that remain.

When starting engines through Windows OpenSSH, keep a WSL terminal open for the fleet's lifetime. Closing the final `wsl.exe` session can stop the WSL instance and its engine processes.
