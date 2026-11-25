---
description: Prepare and run vLLM, SGLang and TensorRT-LLM engines from engine launch records with narwhal-engine.
---

# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM, SGLang and TensorRT-LLM engines from a `narwhal.engine-launch` record. The record's [`runtime.backend`](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification) selects the engine: `vllm` by default, `sglang` or `trtllm`. Each engine has a launch directory that `prepare` creates and the other actions read and write. The `container` launch backend runs engines in Docker. The `native` launch backend runs them on Linux or WSL2 under the Python interpreter that ran `prepare`.

To start a container engine, run three actions and then poll the engine's `/health` route:

```bash
narwhal-engine prepare --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start --run runs/engine-1
```

Each action refuses to overwrite an output it has already written. To start an engine again or repeat a capture, run `prepare` with a fresh `--out` directory and run `check` on it. Actions after `check` refuse a launch directory whose `launch.json` or environment file changed after the check.

## Model inputs

`prepare` reads the model from `NARWHAL_MODEL_DIR` and checks its `config.json` against `NARWHAL_MODEL_CONFIG_SHA256`. A container engine mounts that directory read-only at `/model`.

A vLLM container engine can load a GGUF file in `NARWHAL_MODEL_DIR`. Set `NARWHAL_MODEL_PATH` to the `.gguf` file and `NARWHAL_MODEL_SHA256` to its SHA-256. The file must be a regular file, not a symbolic link. The engine loads the file from its path under `/model`. `prepare` refuses a file that differs from `NARWHAL_MODEL_SHA256` and records its SHA-256 as `model_sha256` in `launch.json`. `check`, `start` and `start-shared` refuse the file if it changes, becomes a symbolic link, or leaves `NARWHAL_MODEL_DIR`.

`prepare` also hashes every file in the model directory except `README.md` and `.cache/huggingface/`, and records the digest as `model_tree_sha256` in `launch.json` and in the attestation `launch` evidence. For a native launch from a model file or checkpoint directory outside `NARWHAL_MODEL_DIR`, `model_tree_sha256` also covers that path.

For a container launch, every file in the model directory must resolve inside it, because the container mounts only that directory. `prepare` refuses a symbolic link that points outside it, and one with an absolute target, which the container would resolve under `/model`. A native launch follows both kinds of link, as a Hugging Face cache snapshot needs.

When `NARWHAL_MODEL_TREE_SHA256` is set, `prepare` refuses a model directory with another digest. The pin covers `NARWHAL_MODEL_DIR` alone, also for a native model path outside it. Engine export sets it from discovery. To compute it for another directory, run:

```bash
echo '{"model_dir": "/path/to/model"}' | .venv/bin/python -m narwhal.deployment.launch_engine.checkpoint
```

The output's `model_tree_sha256` is the pin. For a native launch whose model directory links outside itself, add `"follow_links": true` to the input.

`prepare` records each model file's size, inode, modification time and change time. `check`, `start`, `start-shared`, `start-native`, and the attestation tool's `generate` and `serve` compare them, hashing no model file other than `config.json`, and refuse the launch when a file was added, removed or changed. A `chmod` or a new hard link changes the change time, so they refuse the launch even when the content is unchanged. To serve changed files, run `prepare` with a fresh `--out` directory.

These later stages trust file status. They miss a rewrite that keeps a file's size, inode, modification time and change time, such as one after the system clock was set back, a write to the block device beneath the file system, or a write through a network or FUSE mount that does not update those times.

For the `native` backend, set `NARWHAL_MODEL_REVISION` to a 40-character commit or a `sha256:` digest. To load a model other than `NARWHAL_MODEL_DIR`, set `NARWHAL_MODEL_PATH` to a checkpoint directory, or on vLLM to a GGUF file. For a model file, `prepare` checks it against `NARWHAL_MODEL_SHA256` when you set it and records its SHA-256 as `model_sha256` in `launch.json`. `prepare` refuses `NARWHAL_MODEL_SHA256` for a model directory. A GGUF launch requires `vllm-gguf-plugin` in `runtime.expected_packages`, and the vLLM dev template sets `runtime.tokenizer` and `--hf-config-path` in `runtime.extra_args` to the directory that holds the tokenizer and `config.json`.

## Engine environment

`prepare` writes these values to the engine's environment file:

- the record's `runtime.environment` and `environment`
- `UCX_TLS`
- `UCX_TCP_PORT_RANGE`, from `NARWHAL_UCX_TCP_PORT_RANGE`
- the KV side-channel address, and the side-channel port from `NARWHAL_KV_SIDE_CHANNEL_PORT`
- the engine credential from `NARWHAL_ENGINE_API_KEY`, if you set it

A vLLM engine gets `NIXL_HOST_IP` and `VLLM_NIXL_SIDE_CHANNEL_HOST`, both set to `NARWHAL_NODE_<n>_IP`, `VLLM_NIXL_SIDE_CHANNEL_PORT`, and the credential as `VLLM_API_KEY`.

An SGLang engine gets `SGLANG_HOST_IP`, set to `NARWHAL_NODE_<n>_IP` for SGLang to advertise to decode peers, `NARWHAL_SGLANG_BOOTSTRAP_PORT`, `SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=30`, and the credential as `NARWHAL_SGLANG_API_KEY`. `NARWHAL_SGLANG_BOOTSTRAP_HOST` is the engine's HTTP bind address, or `NARWHAL_NODE_<n>_IP` when the engine binds every interface, as a container engine does. The launcher passes the port to SGLang as `--disaggregation-bootstrap-port` and the credential as `--api-key`.

A TensorRT-LLM engine gets `NARWHAL_TRTLLM_TRANSFER_HOST`, set to `NARWHAL_NODE_<n>_IP`, and `NARWHAL_TRTLLM_TRANSFER_PORT`, set to `NARWHAL_KV_SIDE_CHANNEL_PORT`. The cache transceiver opens its own listener ports. When a credential is set, the engine also gets it as `NARWHAL_TRTLLM_API_KEY`, which `trtllm-serve` does not check.

## Actions

| Action | Backend | Input | Output |
| --- | --- | --- | --- |
| `prepare` | container, native | `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md) | New launch directory with `launch.json`, `container.env` or `engine.env`, and on vLLM and TensorRT-LLM the cache-capture hook |
| `check` | container, native | Prepared launch directory | `checked.json`, and `image-check.log` or `runtime-check.log` |
| `measure-cache` | container, vLLM | Checked plan with no started container | `cache-layout.json` from a sizing container, removed once it succeeds |
| `model-dimensions` | container | Checked plan | `model-dimensions.json` |
| `handshake-policy` | container, native, vLLM | Checked plan | `handshake-policy.json` from the installed NIXL worker |
| `start` | container | Checked plan | One serving container and `container.id` |
| `capture-cache` | container on vLLM; container and native on TensorRT-LLM | Running container of a checked plan, or the launch directory of a started native TensorRT-LLM engine | `cache-layout.json` with the live cache pages |
| `cache-registration` | container, native, vLLM | Checked plan and one of `--startup-log` or `--runtime-layout` | `cache-registration.json` with the resolved block grouping |
| `start-shared` | container, native | Two to eight checked plans on one GPU | Running engines and one `shared-start.json` per engine |
| `start-native` | native | One to eight checked plans, each on its own GPU | Running engines and one `native-start.json` per engine |
| `stop-native` | native | Native launch directory with `native-process.json` | `native-stop.json` |

SGLang plans reject `measure-cache`, `handshake-policy`, `capture-cache` and `cache-registration`. TensorRT-LLM plans reject all of them except `capture-cache`.

These actions check their inputs:

| Action | Checks |
| --- | --- |
| `prepare` | Model configuration hash and, on vLLM and TensorRT-LLM, cache-capture hook hash. For a shared GPU, one CUDA GPU, tensor parallel size 1, and a memory argument equal to `gpu_memory_utilization`. |
| `check` | For `container`, image identity. For `native`, the model path. For both, the model file status that `prepare` recorded, package versions against `runtime.expected_packages`, `--trust-remote-code` when the model or tokenizer has custom code, and the cache-event socket paths in the serving arguments. On vLLM, also the KV connector class, the tokenizer, and `VLLM_SSM_CONV_STATE_LAYOUT=DS` for a model with convolutional SSM state. On SGLang, also that SGLang's server argument parser accepts the serving arguments. On TensorRT-LLM, also that `trtllm-serve` is on the engine's `PATH` and the `--config` file is readable. |
| `start-shared` | Free memory, readiness, identity, and the device allowance for each engine. |
| `start-native` | One GPU for each plan, free memory, readiness and identity for each engine. |
| `stop-native` | Boot ID, process group and start tick of the recorded process, before it sends SIGTERM to the group and SIGKILL to members still running after 10 seconds. |

With text output, `prepare` exits 2 when it fails and 1 when its `--out` directory exists. The other actions exit 1 when they fail, except that an action given one `--run` directory exits 2 when it cannot load that directory. With `--format json`, the exit status follows the [command result](../Command-Results.md) status.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--backend` | `container` | Launch backend for `prepare` and `start-shared`, either `container` or `native`. |
| `--out` | required | New launch directory for `prepare`. |
| `--run` | required | Launch directory for every action except `prepare`, given two to eight times for `start-shared` and one to eight times for `start-native`. |
| `--ready-seconds` | 180 | Positive integer seconds each engine has to answer `/health` during `start-shared` or `start-native`. |
| `--startup-log` | optional | Serving log with one resolved KV layout, read by `cache-registration`. |
| `--runtime-layout` | optional | Captured runtime cache-layout JSON, read by `cache-registration`. |

## Shared-GPU startup

`start-shared` starts two to eight engines on one GPU, one after another. This example starts two native engines, captures the first engine's attestation and stops it:

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine prepare --backend native --out runs/engine-2
narwhal-engine check --run runs/engine-1
narwhal-engine check --run runs/engine-2
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract.cli native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

Every plan must use the `--backend` you pass and must not have started an engine. The plans must share `shared_device.group`, `gpu_uuid` and `device_allowance`, and their `gpu_memory_utilization` values must sum to at most `device_allowance`. Each plan needs its own role and its own engine, attestation and KV side-channel ports. Its `CUDA_VISIBLE_DEVICES` must be `gpu_uuid` or an ordinal or UUID prefix that resolves to `gpu_uuid` in the checked runtime.

### Memory check

The first GPU reading is the baseline. Before each engine starts, `start-shared` requires the GPU's free memory to cover the engine's `gpu_memory_utilization` share of total memory. Once the engine answers `/health` and its identity matches the plan, `start-shared` reads the GPU again. Startup fails when whole-device memory use has grown past `device_allowance` times total memory since the baseline. The reading counts every process on the GPU.

Each engine's `shared-start.json` records the readings in MiB: `gpu_baseline_used_mib`, `gpu_before`, `gpu_after`, `budget_mib`, `device_allowance_mib`, `observed_delta_mib` for this engine and `aggregate_delta_mib` since the baseline. It also records the engine's identity. A container engine records `container_id`, `process_id`, `image_id` and the serving arguments as `engine_args`, and must be running the checked image with the planned arguments. A native engine records `process` with its PID, boot ID and start tick, the engine version as `engine_version`, `process_start_time_seconds`, `model_revision` and the serving arguments. `start-shared` requires a native engine's version to match the checked runtime.

### Startup failures

When a container start fails, `start-shared` removes every container it created, newest first. It sets each one's `shared-start.json` to `failed` with the cause and a `cleanup_status` of `removed` or `failed`. A failed removal adds `cleanup_error` to the record and to the command error.

When a native start fails, `start-shared` stops the failing engine's process group and then stops each engine already running, which writes their `native-stop.json`. The failing engine's `shared-start.json` holds the cause, any `cleanup_error`, and `gpu_after_cleanup`. The running engines' `shared-start.json` files keep `status` `running`.

## Own-GPU startup

`start-native` starts one to eight native engines, one after another, each on a GPU of its own:

```bash
narwhal-engine start-native --run runs/engine-1 --run runs/engine-2
```

Each plan must be a checked native plan without `shared_device`, and must not have started an engine. Its `CUDA_VISIBLE_DEVICES` must name one GPU by its UUID, and no two plans may name the same GPU. Each plan needs its own role and its own engine, attestation and KV side-channel ports. Its serving arguments must set the engine's memory fraction once: `--gpu-memory-utilization` on vLLM, `--mem-fraction-static` on SGLang or `--free_gpu_memory_fraction` on TensorRT-LLM.

Before each engine starts, `start-native` requires its GPU's free memory to cover that fraction of the GPU's total memory. Each engine's `native-start.json` records `gpu_uuid`, the readings `gpu_before`, `gpu_after`, `budget_mib` and `observed_delta_mib` in MiB, and the native engine identity that `shared-start.json` records. When a start fails, `start-native` stops the engines as a native `start-shared` does, and the failing engine's `native-start.json` holds the cause.

## Native ports and attestation

Before a native start, `start-shared` and `start-native` check that each engine's ports are free to bind: the engine address from `launch.json`, the KV side channel from `engine.env`, and the sidecar address from `NARWHAL_NODE_<n>_ATTESTATION_URL`. The KV side channel is the NIXL side channel on vLLM and the bootstrap server on SGLang. Both actions read `NARWHAL_NODE_<n>_ATTESTATION_URL` from their own environment and fall back to the value `prepare` recorded.

To serve the attestation that `native-capture` writes, set `NARWHAL_NODE_<n>_ATTESTATION_URL` in your shell and run:

```bash
python -m narwhal.deployment.attestation_contract.cli serve --run runs/engine-<n>
```

When you start engines through Windows OpenSSH, keep a WSL terminal open for the fleet's lifetime. Closing the last `wsl.exe` session can stop the WSL instance and its engines.
