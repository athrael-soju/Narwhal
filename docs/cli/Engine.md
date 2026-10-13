---
description: Prepare and run vLLM and SGLang engines from engine launch records with narwhal-engine.
---

# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM and SGLang engines from a `narwhal.engine-launch` record. The record's [`runtime.backend`](../configuration/05-Engine-Launch.md#sglang-runtime-fields) selects the engine, `vllm` by default or `sglang`. Each engine has a launch directory that `prepare` creates and the other actions read and write. The `container` launch backend runs engines in Docker. The `native` launch backend runs them on Linux or WSL2 under the Python interpreter that ran `prepare`.

To start a container engine, run three actions and then poll the engine's `/health` route:

```bash
narwhal-engine prepare --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start --run runs/engine-1
```

Each action refuses to overwrite an output it has already written. To start an engine again or repeat a capture, run `prepare` with a fresh `--out` directory and run `check` on it. Actions after `check` refuse a launch directory whose `launch.json` or environment file changed after the check.

## Model inputs

`prepare` reads the model from `NARWHAL_MODEL_DIR` and checks its `config.json` against `NARWHAL_MODEL_CONFIG_SHA256`. A container engine mounts that directory read-only at `/model`.

For the `native` backend, set `NARWHAL_MODEL_REVISION` to a 40-character commit or a `sha256:` digest. To load a model other than `NARWHAL_MODEL_DIR`, set `NARWHAL_MODEL_PATH` to a checkpoint directory or, on vLLM, a GGUF file; `prepare` records a file's SHA-256, and `start-shared` refuses the file once it changes. SGLang takes only a checkpoint directory. A GGUF launch requires `vllm-gguf-plugin` in `runtime.expected_packages`. The vLLM dev template loads a GGUF file with `runtime.tokenizer` and `--hf-config-path` in `runtime.extra_args`, both set to the directory that holds the tokenizer and `config.json`.

## Engine environment

`prepare` writes the engine's environment file from the record's `runtime.environment` and `environment`, `UCX_TLS`, `UCX_TCP_PORT_RANGE` from `NARWHAL_UCX_TCP_PORT_RANGE`, and the KV side channel. It reads the engine credential, when set, from `NARWHAL_ENGINE_API_KEY` and the side-channel port from `NARWHAL_KV_SIDE_CHANNEL_PORT`.

A vLLM engine gets `NIXL_HOST_IP` and `VLLM_NIXL_SIDE_CHANNEL_HOST`, both set to `NARWHAL_NODE_<n>_IP`, `VLLM_NIXL_SIDE_CHANNEL_PORT`, and the credential as `VLLM_API_KEY`.

An SGLang engine gets `SGLANG_HOST_IP`, set to `NARWHAL_NODE_<n>_IP` for SGLang to advertise to decode peers, `NARWHAL_SGLANG_BOOTSTRAP_PORT`, `SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=30`, and the credential as `NARWHAL_SGLANG_API_KEY`. `NARWHAL_SGLANG_BOOTSTRAP_HOST` is the engine's HTTP bind address, or `NARWHAL_NODE_<n>_IP` when the engine binds every interface, as a container engine does. The launcher passes the port to SGLang as `--disaggregation-bootstrap-port` and the credential as `--api-key`.

## Actions

| Action | Backend | Input | Output |
| --- | --- | --- | --- |
| `prepare` | container, native | `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md) | New launch directory with `launch.json`, `container.env` or `engine.env`, and on vLLM the cache-capture hook |
| `check` | container, native | Prepared launch directory | `checked.json`, and `image-check.log` or `runtime-check.log` |
| `measure-cache` | container, vLLM | Checked plan with no started container | `cache-layout.json` from a sizing container, removed once it succeeds |
| `model-dimensions` | container | Checked plan | `model-dimensions.json`, read in the image on vLLM and from the model's `config.json` on SGLang |
| `handshake-policy` | container, native, vLLM | Checked plan | `handshake-policy.json` from the installed NIXL worker |
| `start` | container | Checked plan | One serving container and `container.id` |
| `capture-cache` | container, vLLM | Running container of a checked plan | `cache-layout.json` with the live cache pages |
| `cache-registration` | container, native, vLLM | Checked plan and one of `--startup-log` or `--runtime-layout` | `cache-registration.json` with the resolved block grouping |
| `start-shared` | container, native | Two to eight checked plans on one GPU | Running engines and one `shared-start.json` per engine |
| `stop-native` | native | Native launch directory with `native-process.json` | `native-stop.json` |

SGLang plans reject `measure-cache`, `handshake-policy`, `capture-cache` and `cache-registration`, because an SGLang engine reports its KV cache over HTTP and has no NIXL handshake policy.

These actions check their inputs:

| Action | Checks |
| --- | --- |
| `prepare` | Model configuration hash and, on vLLM, cache-capture hook hash. For a shared GPU, one CUDA GPU, tensor parallel size 1, and a memory argument equal to `gpu_memory_utilization`. |
| `check` | Image identity for `container`, the model path and model file hash for `native`, package versions against `runtime.expected_packages`, `--trust-remote-code` when the model or tokenizer needs remote code, and the cache-event endpoints the server arguments resolve to. On vLLM, also the KV connector class, the tokenizer, and `VLLM_SSM_CONV_STATE_LAYOUT=DS` for a model with convolutional SSM state. On SGLang, also that SGLang's server argument parser accepts the serving arguments. |
| `start-shared` | Free memory, readiness, identity, and the device allowance for each engine. |
| `stop-native` | Boot ID, process group and start tick of the recorded process, before it sends SIGTERM to the group and SIGKILL to members still running after 10 seconds. |

With text output, `prepare` exits 2 when it fails and 1 when its `--out` directory exists. The other actions exit 1 when they fail, except that an action given one `--run` directory exits 2 when it cannot load that directory. With `--format json`, the exit status follows the [command result](../Command-Results.md) status.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--backend` | `container` | Launch backend for `prepare` and `start-shared`, either `container` or `native`. |
| `--out` | required | New launch directory for `prepare`. |
| `--run` | required | Launch directory for every action except `prepare`, given two to eight times for `start-shared`. |
| `--ready-seconds` | 180 | Positive integer seconds each engine has to answer `/health` during `start-shared`. |
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

Each engine's `shared-start.json` records the readings in MiB: `gpu_baseline_used_mib`, `gpu_before`, `gpu_after`, `budget_mib`, `device_allowance_mib`, `observed_delta_mib` for this engine and `aggregate_delta_mib` since the baseline. It also records the engine's identity. A container engine records `container_id`, `process_id`, `image_id` and the serving arguments as `engine_args`, and must be running the checked image with the planned arguments. A native engine records `process` with its PID, boot ID and start tick, the engine version as `engine_version`, `process_start_time_seconds`, `model_revision` and the serving arguments. `start-shared` reads a native engine's version from `/version` on vLLM and `/server_info` on SGLang, and requires it to match the checked runtime. It reads the process start from vLLM's `/metrics`, and from the engine process on the host for SGLang.

### Startup failures

When a container start fails, `start-shared` removes every container it created, newest first. It sets each one's `shared-start.json` to `failed` with the cause and a `cleanup_status` of `removed` or `failed`. A failed removal adds `cleanup_error` to the record and to the command error.

When a native start fails, `start-shared` stops the failing engine's process group and then stops each engine already running, which writes their `native-stop.json`. The failing engine's `shared-start.json` holds the cause, any `cleanup_error`, and `gpu_after_cleanup`. The running engines' `shared-start.json` files keep `status` `running`.

### Native ports and attestation

Before a native start, `start-shared` checks that each engine's ports are free to bind: the engine address from `launch.json`, the KV side channel from `engine.env`, and the sidecar address from `NARWHAL_NODE_<n>_ATTESTATION_URL`. The KV side channel is the NIXL side channel on vLLM and the bootstrap server on SGLang. `start-shared` reads `NARWHAL_NODE_<n>_ATTESTATION_URL` from its own environment and falls back to the value `prepare` recorded.

To serve the attestation that `native-capture` writes, set `NARWHAL_NODE_<n>_ATTESTATION_URL` in your shell and run:

```bash
python -m narwhal.deployment.attestation_contract.cli serve --run runs/engine-<n>
```

When you start engines through Windows OpenSSH, keep a WSL terminal open for the fleet's lifetime. Closing the last `wsl.exe` session can stop the WSL instance and its engines.
