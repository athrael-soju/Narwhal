# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM engines from an existing `narwhal.engine-launch` record. It has two backends:

- `native` runs vLLM from the checked Python environment on Linux or WSL2.
- `container` runs it in a Docker container.

Both backends use the same model, GPU allocation, ports, and NIXL connector, and both run the same checks on the runtime arguments.

## Launch directories

Every action works on a launch directory. `prepare` creates one, which you name with `--out`. Every later action points at it with `--run`.

A launch directory is used once. To start again or redo an inspection, prepare and check a new directory.

A native run with two engines sharing one GPU:

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine prepare --backend native --out runs/engine-2
narwhal-engine check --run runs/engine-2
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

## Before you start

The native backend needs `NARWHAL_MODEL_REVISION` set, along with the launch environment created during deployment. To use a local GGUF file, set `NARWHAL_MODEL_PATH`. `prepare` records the file's SHA-256.

### Using a GGUF model

- Pin `vllm-gguf-plugin` in `runtime.expected_packages`.
- Keep the model file inside its snapshot directory so the loader finds the files that go with it.
- Pass the base model's tokenizer and config with `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

## Actions

| Action               | Backends  | What it does                                                                                                                                                                                                                 |
| -------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prepare`            | both      | Reads `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md), checks the model and hook hashes, and creates a new launch directory containing `launch.json` and the backend's environment. |
| `check`              | both      | Checks the pinned packages, model, tokenizer, and NIXL connector for the launch directory, then writes `checked.json`. Each run of `check` is added to the backend's check log.                                   |
| `measure-cache`      | container | Starts a temporary container from a checked launch directory that hasn't been used yet, writes `cache-layout.json`, and removes the container when it's done.                                                                            |
| `model-dimensions`   | container | Inspects the model through the checked runtime and writes `model-dimensions.json`.                                                                                                                                           |
| `handshake-policy`   | both      | Compares the installed NIXL worker with the checked connector settings and writes `handshake-policy.json`.                                                                                                                   |
| `start`              | container | Starts one serving container from a checked launch directory and saves its ID in `container.id`. It doesn't wait for readiness; follow the container's logs and check its HTTP endpoints.                                    |
| `capture-cache`      | container | Reads the running container recorded in a checked launch directory and saves its live cache pages to `cache-layout.json`.                                                                                                    |
| `cache-registration` | both      | Works out block grouping from the checked runtime plus either a serving startup log or a captured runtime layout, and writes `cache-registration.json`.                                                                      |
| `start-shared`       | both      | Starts two to eight checked launch directories that share one GPU, one at a time. Each engine gets a `shared-start.json` with its readiness, identity, and memory readings.                                                               |
| `stop-native`        | native    | Confirms each process in the launch directory is the one that was started, stops its process group, and writes `native-stop.json`.                                                                                                                   |

## Options

| Option             | Default                  | Description                                                                                                                                                          |
| ------------------ | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--out`            | required for `prepare`   | New launch directory to create.                                                                                                                                      |
| `--run`            | required after `prepare` | Existing launch directory to use. Give it two to eight times for `start-shared`.                                                                                     |
| `--backend`        | `container`              | `container` or `native`. Used by `prepare` and `start-shared`, and every plan passed to `start-shared` must use it. Other actions use the backend saved in the plan. |
| `--ready-seconds`  | `180`                    | Seconds each engine has to pass its readiness and identity checks during `start-shared`. A positive whole number.                                                    |
| `--startup-log`    | none                     | Serving log containing exactly one resolved KV layout, for `cache-registration`. Can't be combined with `--runtime-layout`.                                          |
| `--runtime-layout` | none                     | Captured runtime cache-layout JSON, for `cache-registration`. Can't be combined with `--startup-log`.                                                                |
| `--format`         | `text`                   | Output format: `text` or `json` ([versioned command results](../Command-Results.md)).                                                                                                    |
| `--version`        | none                     | Print the installed version.                                                                                                                                         |

`cache-registration` needs either `--startup-log` or `--runtime-layout`.

## Running several engines on one GPU

Before it launches anything, `start-shared` checks every plan's role, ports, and GPU budget. All the plans must have the same group, GPU UUID, and device allowance, and no two can have the same role or port.

Each engine's `gpu_memory_utilization` is a fraction of the GPU's total memory. The fractions are added with exact arithmetic, and the total can't exceed `shared_device.device_allowance`.

The selected CUDA device must resolve to `shared_device.gpu_uuid`. Numeric device ordinals and UUID prefixes are resolved inside the checked runtime, using its recorded environment.

### Memory checks during startup

Engines start one at a time. Before the first launch, the command takes a reading of GPU memory as a baseline. Each time an engine passes its readiness and identity checks, the command measures how much whole-device memory use has grown since the baseline and compares that with `device_allowance` times the GPU's total memory. A total equal to the allowance passes.

Memory already in use on the GPU reduces the free space checked before each launch. Other processes that allocate or free memory during startup change the measured growth.

Each ready engine's `shared-start.json` records the baseline, the readings before and after, the growth for that engine and in total, and the allowance, all in MiB.

### Container backend: startup failure

For each container, the command records its ID, Linux PID, image ID, and serving arguments.

If startup fails, the command removes every container it created, newest first. It marks their `shared-start.json` records as `failed` and saves the cause of the failure and the result of each removal in `cleanup_status`. If a container can't be removed, the error is saved in `cleanup_error` and included in the command's error message. Check that container ID before you try to recover. Container IDs and launch evidence stay in the run directories.

### Native backend: startup failure

For each engine, the command records its Linux PID, the machine's boot ID, the process start time (in kernel clock ticks), the vLLM version, the process start time reported by `/metrics`, the model revision, and the arguments.

If startup fails, the command stops the engine that was starting, then stops the engines that were already ready, using their recorded identities. The failed engine's `shared-start.json` records the cause, any cleanup errors, and a GPU memory reading taken after cleanup. Each already-ready engine that stops successfully gets a `native-stop.json`.

### Native backend: port checks

Before launching, the native backend tries binding the HTTP address from `launch.json` and the NIXL address from `engine.env`. It binds the sockets the same way vLLM (IPv6 by default in the kernel) and NIXL (dual-stack) do. Each engine binds again when it starts, so an address taken after the check is still caught.

To check the attestation sidecar's address as well, set `NARWHAL_NODE_<n>_ATTESTATION_URL` when you run `prepare` or `start-shared`. The check runs through Uvicorn's event loop. If the variable is set at both steps, the value at `start-shared` wins. `narwhal dev` sets this URL from the instance's fleet config.

## Native backend: attestation and shutdown

`native-capture` applies the engine's recorded environment, runs Narwhal's model, cache, NIXL, and handshake checks, and writes an attestation tied to the running process. To serve it, set `NARWHAL_NODE_<n>_ATTESTATION_URL` and run:

```bash
python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>
```

`stop-native` checks the recorded boot ID and process start time, so it only stops processes it started. It sends SIGTERM to the process group, waits up to 10 seconds for the leader and workers to exit, and then sends SIGKILL to any that are still running.

## Starting engines over Windows OpenSSH

If you start engines through Windows OpenSSH, keep a WSL terminal open while the fleet runs. Closing the last `wsl.exe` session can shut down the WSL instance and your engines with it.
