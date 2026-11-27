---
description: Prepare and run vLLM, SGLang and TensorRT-LLM engines from engine launch records with narwhal-engine.
---

# `narwhal-engine`

`narwhal-engine` prepares and runs vLLM, SGLang and TensorRT-LLM engines from a `narwhal.engine-launch` record. The record's [`runtime.backend`](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification) selects the engine: `vllm` by default, `sglang` or `trtllm`. Each engine has a launch directory that `prepare` creates and the other actions read and write. The `container` launch backend runs engines in Docker. The `kubernetes` launch backend renders each engine into a [Kubernetes engine pod](#kubernetes-plans) that the kubelet starts. The `native` launch backend runs engines on Linux or WSL2 under the Python interpreter that ran `prepare`.

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

For a container launch, every file in the model directory must resolve inside it, because the container mounts only that directory. `prepare` refuses a symbolic link that points outside it, one with an absolute target, which the container would resolve under `/model`, and one whose relative target climbs above the directory, such as `../m2/weights` in a directory named `m2`, which names nothing under `/model`. A native launch follows all three kinds of link, as a Hugging Face cache snapshot needs.

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
- `UCX_TLS`, and for `ucx_rdma` also `UCX_SHM_DEVICES=none`
- `UCX_TCP_PORT_RANGE`, from `NARWHAL_UCX_TCP_PORT_RANGE`
- the KV side-channel address, and the side-channel port from `NARWHAL_KV_SIDE_CHANNEL_PORT`
- the engine credential from `NARWHAL_ENGINE_API_KEY`, if you set it

A vLLM engine gets `NIXL_HOST_IP` and `VLLM_NIXL_SIDE_CHANNEL_HOST`, both set to `NARWHAL_NODE_<n>_IP`, and `VLLM_NIXL_SIDE_CHANNEL_PORT`, set to `NARWHAL_KV_SIDE_CHANNEL_PORT`. When a credential is set, the engine gets it as `VLLM_API_KEY`.

An SGLang engine gets `SGLANG_HOST_IP`, set to `NARWHAL_NODE_<n>_IP` for SGLang to advertise to decode peers, `NARWHAL_SGLANG_BOOTSTRAP_PORT`, `SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=30`, and the credential as `NARWHAL_SGLANG_API_KEY`. `NARWHAL_SGLANG_BOOTSTRAP_HOST` is the engine's HTTP bind address, or `NARWHAL_NODE_<n>_IP` when the engine binds every interface, as a container engine does. The launcher passes the port to SGLang as `--disaggregation-bootstrap-port` and the credential as `--api-key`.

A TensorRT-LLM engine gets `NARWHAL_TRTLLM_TRANSFER_HOST`, set to `NARWHAL_NODE_<n>_IP`, and `NARWHAL_TRTLLM_TRANSFER_PORT`, set to `NARWHAL_KV_SIDE_CHANNEL_PORT`. The cache transceiver opens its own listener ports. When a credential is set, the engine also gets it as `NARWHAL_TRTLLM_API_KEY`, which `trtllm-serve` does not check.

## Actions

| Action | Backend | Input | Output |
| --- | --- | --- | --- |
| `prepare` | container, Kubernetes, native | `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md) | New launch directory with `launch.json`, `container.env` or `engine.env`, on vLLM and TensorRT-LLM the cache-capture hook, and for Kubernetes `kubernetes.json` |
| `check` | container, Kubernetes, native | Prepared launch directory, and for Kubernetes the engine pod's records | `checked.json`, and `image-check.log`, `runtime-check.log` or the pod's records under `pods/` |
| `measure-cache` | container, vLLM | Checked plan with no started container | `cache-layout.json` from a sizing container, removed once it succeeds |
| `model-dimensions` | container | Checked plan | `model-dimensions.json` |
| `handshake-policy` | container, native, vLLM | Checked plan | `handshake-policy.json` from the installed NIXL worker |
| `start` | container | Checked plan | One serving container and `container.id` |
| `capture-cache` | container on vLLM; container and native on TensorRT-LLM | Running container of a checked plan, or the launch directory of a started native TensorRT-LLM engine | `cache-layout.json` with the live cache pages |
| `cache-registration` | container, native, vLLM | Checked plan and one of `--startup-log` or `--runtime-layout` | `cache-registration.json` with the resolved block grouping |
| `start-shared` | container, native | Two to eight checked plans on one GPU | Running engines and one `shared-start.json` per engine |
| `start-native` | native | One to eight checked plans, each on its own GPU | Running engines and one `native-start.json` per engine |
| `stop-native` | native | Native launch directory with `native-process.json` | `native-stop.json` |

SGLang plans reject `measure-cache`, `handshake-policy`, `capture-cache` and `cache-registration`. TensorRT-LLM plans reject all of them except `capture-cache`. Kubernetes plans take only `prepare` and `check`.

These actions check their inputs:

| Action | Checks |
| --- | --- |
| `prepare` | Model configuration hash and, on vLLM and TensorRT-LLM, cache-capture hook hash. For an engine on a shared GPU, it also checks that the engine uses a single NVIDIA GPU with `tensor_parallel_size` 1, and that the engine's memory flag (`--gpu-memory-utilization`, `--mem-fraction-static` or `--free_gpu_memory_fraction`) is set to `shared_device.gpu_memory_utilization`. |
| `check` | For `container`, image identity. For `native`, the model path. For `kubernetes`, the engine pod's records, as [Kubernetes plans](#kubernetes-plans) describes. For `container` and `native`, the model file status that `prepare` recorded. For every backend, package versions against `runtime.expected_packages`, `--trust-remote-code` when the model or tokenizer has custom code, which a Kubernetes plan checks at `prepare`, and the cache-event socket paths in the serving arguments. On vLLM, also the KV connector class, the tokenizer, and `VLLM_SSM_CONV_STATE_LAYOUT=DS` for a model with convolutional SSM state. On SGLang, also that SGLang's server argument parser accepts the serving arguments. On TensorRT-LLM, also that `trtllm-serve` is on the engine's `PATH` and the `--config` file is readable. |
| `start-shared` | Free memory, readiness, identity, and the device allowance for each engine. |
| `start-native` | One GPU for each plan, free memory, readiness and identity for each engine. |
| `stop-native` | Boot ID, process group and start tick of the recorded process, before it sends SIGTERM to the group and SIGKILL to members still running after 10 seconds. |

With text output, `prepare` exits 2 when it fails and 1 when its `--out` directory exists. The other actions exit 1 when they fail, except that an action given one `--run` directory exits 2 when it cannot load that directory. With `--format json`, the exit status follows the [command result](../Command-Results.md) status.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | `text` or `json` for [versioned command results](../Command-Results.md). |
| `--backend` | `container` | Launch backend: `container`, `kubernetes` or `native` for `prepare`, and `container` or `native` for `start-shared`. |
| `--pod-evidence` | | Directory with an engine pod's `checkpoint.json` and `runtime-check.json`, read by `check`. Required for a Kubernetes plan and refused for the others. |
| `--out` | required | New launch directory for `prepare`. |
| `--run` | required | Launch directory for every action except `prepare`, given two to eight times for `start-shared` and one to eight times for `start-native`. |
| `--ready-seconds` | 180 | Positive integer seconds each engine has to answer `/health` during `start-shared` or `start-native`. |
| `--startup-log` | optional | Serving log with one resolved KV layout, read by `cache-registration`. |
| `--runtime-layout` | optional | Captured runtime cache-layout JSON, read by `cache-registration`. |

## Kubernetes plans

A Kubernetes plan runs its engine in a pod. `prepare` renders the pod, the pod's init containers run the checks, and `check` reads what they recorded:

```bash
narwhal-engine prepare --backend kubernetes --out runs/engine-1
narwhal-engine check --run runs/engine-1 --pod-evidence runs/engine-1-pod
```

`prepare` takes the [Kubernetes inputs](../configuration/04-Deployment-Inputs.md#kubernetes-launch-inputs) besides the container inputs. `NARWHAL_ENGINE_IMAGE` must be a registry digest, `<repository>@sha256:<hex>`, and `NARWHAL_NODE_<n>_URL` names the engine pod, such as its DNS name under a headless Service. The record must transfer KV over `ucx_rdma` and must not set `shared_device`.

The device plugins choose the pod's GPUs and HCA, so the plan differs from a container plan:

- It requests as many GPUs as the record's `gpu_ids` lists, under `NARWHAL_KUBERNETES_GPU_RESOURCE`, and one HCA under `NARWHAL_KUBERNETES_RDMA_RESOURCE`. It ignores the record's device paths. `transfer.net_devices` must name one `<HCA>:<port>`: the plan keeps the port as `kubernetes.hca_port` and ignores the HCA name.
- It sets neither the GPU visibility variable nor `UCX_NET_DEVICES`. The pod sets `UCX_NET_DEVICES` from the HCA it receives.
- Each variable that carries `NARWHAL_NODE_<n>_IP` on a host, such as `NIXL_HOST_IP` on vLLM, holds `$(NARWHAL_POD_IP)` instead. Kubernetes fills it with the pod's address, which is the node's address under host networking. Kubernetes passes every other value unchanged, because `kubernetes.json` writes each `$` as `$$`.
- It records no model file status. The pod hashes the model files it serves.
- `prepare` refuses a model with custom code when the record lacks the backend's remote-code flag, because it renders the runtime probe.

### The engine pod

`kubernetes.json` holds `plan_sha256`, two immutable ConfigMaps under `configMaps`, and the engine pod's specification under `podSpec`. The ConfigMap `<plan name>-hooks` holds the hook directory: `launch.json`, `launch_engine.py`, the cache-capture hook and the engine configuration files. The ConfigMap `narwhal-pod-<digest>` holds the pod steps: `pod.py`, `checkpoint.py`, `verdict.py`, and the engine backend's probe reader as `probe_reader.py`. Its name follows its content, so plans of one engine backend prepared by one Narwhal release share it.

Every container runs in the unmodified engine image:

| Container | Kind | Step |
| --- | --- | --- |
| `checkpoint` | Init | Hashes the files under `/model` and fails when their tree digest differs from `model_tree_sha256` in `launch.json` |
| `runtime-check` | Init | Runs the runtime probe that `check` runs in Docker, with the engine's environment, devices and mounts, and judges its output as `check` does. It fails on a probe that exits nonzero or an output that fails the check |
| `engine` | Main | Starts the engine with `python3` and the plan's arguments, once both init containers have recorded a pass for this plan, this pod and its HCA. Each start, including a restart without the init containers, judges the recorded probe output again and requires the recorded verdict |

Each container's command first runs a bootstrap under `python3 -I -B`. The bootstrap checks every file in `/narwhal-hooks` and `/narwhal-pod` against the SHA-256 the pod specification pins, and fails on a missing, extra or changed file. It skips the entries whose names start with `..`, which the kubelet adds to a ConfigMap volume. Isolated mode keeps the hook directory, which the engine's `PYTHONPATH` names, off the bootstrap's import path, and `-B` keeps the pod steps from writing bytecode. The `engine` container's step replaces itself with the engine, which keeps the container's first process ID.

The specification sets:

| Setting | Value |
| --- | --- |
| Network | `hostNetwork: true`, `dnsPolicy: ClusterFirstWithHostNet` and `enableServiceLinks: false`, so Kubernetes adds no variables to the engine's environment |
| Devices | The GPU and HCA requests in `runtime-check` and `engine` |
| Volumes | The model volume read-only at `/model`; the ConfigMaps read-only at `/narwhal-hooks` and `/narwhal-pod`; the state claim at `/narwhal-state`; empty directories at `/root/.cache`, in memory at `/dev/shm`, and at `/narwhal-kv-events` while the engine publishes cache events |
| Ports | The engine port as `http` and the KV side-channel port as `kv-side-channel` |
| Credential | The engine credential from the Secret that `NARWHAL_KUBERNETES_API_KEY_SECRET` names |
| Security context | `IPC_LOCK` added to `runtime-check` and `engine`, so they can lock the memory that RDMA registers, as the container backend's unlimited `memlock` allows. On ROCm, also seccomp `Unconfined`, as the container backend runs ROCm engines |

The state claim mounts at the subdirectory `engines/<role>/<pod UID>`, writable for the init containers and read-only for `engine`.

### Allocated HCA

`runtime-check` and `engine` each read the one `/dev/infiniband/uverbs*` device the RDMA device plugin allocated, take its HCA from `/sys/class/infiniband_verbs/<device>/ibdev`, and set `UCX_NET_DEVICES` to `<hca>:<port>` with the plan's `kubernetes.hca_port`. Either container fails when it holds no verbs device or more than one. `engine` also fails when its HCA differs from the one `runtime-check` recorded.

### Pod records

The init containers write `checkpoint.json` and `runtime-check.json` to `/narwhal-state`. Both hold `plan_sha256`, `pod_uid`, `node`, and under `files` the SHA-256 of every delivered file. `checkpoint.json` adds `model_tree_sha256`, `file_count` and `bytes_verified`. `runtime-check.json` adds the allocated HCA as `transfer_device`, the probe's `returncode`, `stdout` and `stderr`, and for a passing probe the verdict as `verdict`: the evidence that `check` records and the notes it prints. A step that fails records `error`. An init container that runs again in the same pod replaces its record.

The runtime probe has `NARWHAL_STAGE_POD_RUNTIME_CHECK_TIMEOUT_SECONDS` to finish, or `NARWHAL_STAGE_TIMEOUT_SECONDS` when that is unset, 300 seconds by default. `prepare` reads them and pins the budget in the plan.

### Checking the records

Copy the pod's directory `engines/<role>/<pod UID>/` from the state volume, and pass the copy to `check` as `--pod-evidence`. `check` refuses records that:

- name no pod UID or another plan, or come from two pods or two nodes
- list delivered files other than the plan's
- record an error, a model tree digest other than the plan's, or a probe that exited nonzero
- name no allocated HCA, or an HCA port other than the plan's

It then checks the probe output as the Docker check does, and fails when the pod's `verdict` differs from its own. It writes `checked.json` with `backend` set to `kubernetes`, `planned_image`, `pod_uid`, `node` and `transfer_device`. `planned_image` is `NARWHAL_ENGINE_IMAGE` as the plan names it; `check` does not observe the image the kubelet ran. `check` keeps each record of the plan it reads under `pods/<pod UID>/`, named by the record and its SHA-256.

`check` reads records, not the pod. It accepts the records of any pod that ran the plan, including one that has since stopped or been replaced.

A launch directory holds the check of one pod. To check a replacement pod of the same plan, copy `launch.json`, `container.env`, `hook/` and `kubernetes.json` into a new directory and check the new pod there.

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
