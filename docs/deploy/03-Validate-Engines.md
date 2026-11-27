---
description: Validate and start every vLLM, SGLang or TensorRT-LLM engine in a Narwhal fleet.
---

# Validate and start engines

## Inspecting every engine host

In the installed engine-role shell on every engine host, inspect the host before you prepare its launch plan:

```bash
umask 077
export INSPECTION_RUN="runs/host-inspection-$(date -u +%Y%m%dT%H%M%SZ)-$$"
narwhal-engine inspect-host --out "$INSPECTION_RUN"
```

For a native engine, add `--backend native`. The inspection reads the launch record that `NARWHAL_ENGINE_LAUNCH_CONFIG` selects and the role environment, and runs every check:

| Check | Passes when |
| --- | --- |
| `accelerators` | The host's GPUs run the runtime that `gpu_visibility_env` selects: ROCm for `ROCR_VISIBLE_DEVICES`, CUDA for `CUDA_VISIBLE_DEVICES`. Each `gpu_ids` entry names a distinct GPU on the host by index or UUID, and each selected GPU's product equals `accelerator`. On ROCm, `accelerator_devices` maps `/dev/kfd` and the render device of each selected GPU. A ROCm GPU's index is its `rocminfo` agent number and its product the agent's `Marketing Name`. A CUDA GPU's index, product and UUID come from `nvidia-smi`, and its device node `/dev/nvidia<minor>` takes the minor number that the driver reports for its PCI bus ID, which differs from the index on a host that exposes only some of a machine's GPUs |
| `image` | For a container engine, `NARWHAL_ENGINE_IMAGE` is the local image ID, or a registry digest among the local image's `RepoDigests` |
| `engine_user` | The inspection reads the user and groups the engine runs as. A container engine runs as the image's user with the image's groups, because its launch sets neither `--user` nor `--group-add`, so the inspection reads them from a process it starts from the image with no network. A native engine runs as the user that runs `narwhal-engine`. The engine user must run in the inspection's user namespace, so a container under rootless Docker or userns-remap fails: its IDs are not the IDs that the device owners name |
| `model` | The model files pass the checks that `prepare` applies, reading them the same way: `NARWHAL_MODEL_DIR/config.json` matches `NARWHAL_MODEL_CONFIG_SHA256`, the directory's tree digest matches `NARWHAL_MODEL_TREE_SHA256` when it is set, and with `NARWHAL_MODEL_SHA256` set, `NARWHAL_MODEL_PATH` has that SHA-256 |
| `run_directory` | `NARWHAL_RUN_DIR` is a directory |
| `fabric_interface` | `NARWHAL_FABRIC_INTERFACE` is up and holds `NARWHAL_NODE_<n>_IP` |
| `ports` | These ports are free to bind: the engine port at the address the engine binds, every address of the endpoint's family for a container engine and the endpoint's host for a native one; the vLLM NIXL side channel, SGLang bootstrap server or TensorRT-LLM transfer port; the attestation port of `NARWHAL_NODE_<n>_ATTESTATION_URL`; and for `ucx_tcp`, one free port of `NARWHAL_UCX_TCP_PORT_RANGE` at `NARWHAL_NODE_<n>_IP` for each UCX listener, which is one per TP rank on each device of `transfer.net_devices` |
| `devices` | Each path in `accelerator_devices` and `transfer.devices`, and each selected GPU's device node, is a character device without an access control list that the engine's user can read and write |

A device passes the permission check when the engine's user is root, or the device's mode grants read and write to that user: the owner bits when the user owns the device, otherwise the group bits when the user's group or one of its groups owns it, otherwise the other bits. The check reads the mode bits. It refuses a device that carries an access control list, because the list's mask replaces the group bits. On ROCm, this covers `/dev/kfd` and each selected `/dev/dri/renderD<minor>`. A ROCm container that runs as a non-root user passes only when its image gives that user the devices' group, such as `render` or `video` with the host's group ID, or the devices grant access to every user.

The inspection writes `host-inspection.json` at mode 0600 with the launch record's allocation fields (`role`, `accelerator`, `gpu_ids`, `tensor_parallel_size`, `accelerator_devices`, `network_mode`, `transfer` and `environment`), each check's observations, the failures and `passed`. Commands it runs on the host log to `inspection.log`. On success it prints `engine-<n>: host inspection passed; evidence in <path>`. When a check fails, it still writes `host-inspection.json`, then exits 1 naming every failed check. A missing or malformed input, such as an unset `NARWHAL_FABRIC_INTERFACE` or an attestation URL without an HTTP port, exits 2 before any check runs. A failed check reads:

```text
narwhal-engine: inspect-host: ports: the engine port 8000 at 0.0.0.0 is unavailable: [Errno 98] Address already in use; inspect runs/host-inspection-<id>/host-inspection.json
```

Free an unavailable port, restore a missing device or its permissions, or correct the launch record, then inspect again into a new `--out` directory.

When `NARWHAL_UCX_TCP_PORT_RANGE` lies inside the host's `net.ipv4.ip_local_port_range`, outgoing connections take ports of the range as their source ports. After an engine stops, those ports stay in TIME-WAIT for about a minute and cannot be bound. Reserve the range for listeners with `net.ipv4.ip_local_reserved_ports`, which needs root, or choose a range outside `ip_local_port_range`. When the provisioned checkpoint or a GPU changed, rerun discovery. `narwhal deploy package` has already checked the fleet's `hardware` fields against the launch records, as [Building the deployment package](02-Install.md#building-the-deployment-package) describes, so an inspection that passes on every host ties them to the hosts.

## Preparing, checking, and starting each engine

The launcher builds one launch plan per engine with these properties:

| Property                      | Value                                                                                                                                                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Serving command               | vLLM: `python3 -m vllm.entrypoints.openai.api_server` inside `NARWHAL_ENGINE_IMAGE`. SGLang: `python3 -m sglang.launch_server`, and TensorRT-LLM: `trtllm-serve serve`, each started by the Narwhal launch wrapper inside `NARWHAL_ENGINE_IMAGE`                                         |
| Model mount                   | `NARWHAL_MODEL_DIR`, read-only at `/model`. vLLM loads a GGUF file from its path under `/model`                                                                                                                                             |
| Devices                       | The configured devices with the selected TP allocation                                                                                                                                                                                      |
| KV connector                  | vLLM: `NixlConnector` with `kv_role=kv_both`, the UCX backend, `enforce_handshake_compat=true`, and `kv_load_failure_policy=fail`. SGLang: the `mooncake` transfer backend; every engine launches as prefill with `--enable-pd-role-switch`. TensorRT-LLM: the C++ cache transceiver with the `UCX` or `NIXL` backend, set in `engine.json` |
| Side-channel address and port | The role environment, for the vLLM NIXL side channel, the SGLang bootstrap server or the TensorRT-LLM transfer port                                                                                                                                                         |
| Transport                     | `UCX_TLS` of `tcp,sm,self,<gpu>` for `ucx_tcp`, or `rc,sm,self,<gpu>` with `UCX_SHM_DEVICES=none` for `ucx_rdma`                                                                                                                                                       |
| Prefix caching                | On by default, turned off through `runtime.prefix_caching`                                                                                                                                                                                  |
| Cache events                  | Published by the engine over private IPC sockets under `/tmp/narwhal-<uid>/` while prefix caching and `runtime.kv_events` are on                                                                                                            |
| Cache event opt-out           | [Prefix caching and cache events documentation](../configuration/05-Engine-Launch.md#prefix-caching-and-cache-events)                                                                                                                       |

For the [capacity trial](07-Serve-and-Measure.md#running-the-initial-capacity-trial), turn prefix caching off by setting `NARWHAL_PREFIX_CACHING=false` in `.env` before discovery, or `runtime.prefix_caching` to `false` in the launch records before preparing the launch plans. `narwhal deploy run --trial` refuses to run the trial otherwise. To turn it off after launch:

1. Prepare a fresh launch directory.
2. Restart each engine.
3. Capture the live cache layout, or measure the handoffs on SGLang, and derive the cache-equivalence groups again.
4. Repeat the stages that [Rerunning after a change](../Deploy.md#rerunning-after-a-change) lists for an engine process restart and for a changed attested binding digest.

For a separate tokenizer at `PATH`, set `NARWHAL_TOKENIZER=PATH` in `.env` before discovery, or set `runtime.tokenizer` to `PATH` before preparing the launch plan.

When the image, model, dtype, cache policy, TP allocation or model arguments change, capture the live cache layout on vLLM and TensorRT-LLM, or measure the handoffs on SGLang, and derive the groups again. When the workload assumptions change, recalculate the fabric budget.

In every engine-role shell, prepare the launch plan:

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
narwhal-engine prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

The launcher prints `Prepared <role>; review launch.json and run the image check.`

Inspect `launch.json` for:

- the immutable image
- the complete serving command
- the mounts and device mappings
- the endpoint
- the application revision
- the source hashes

Keep `container.env` private.

For a GGUF model, `prepare` checks the file against `NARWHAL_MODEL_SHA256` and records its SHA-256 as `model_sha256` in `launch.json`. `check` and `start` refuse the file if it changes, becomes a symbolic link, or leaves `NARWHAL_MODEL_DIR`.

Validate the image and plan:

```bash
narwhal-engine check --run "$ENGINE_RUN"
```

A passing check confirms, for every backend:

- the immutable image identity and runtime identity
- the pinned package versions
- the custom-code requirements
- the plan hash
- the resolved prefix-caching setting and cache-event endpoints, matched against the plan

For vLLM, the check also confirms:

- the connector
- the tokenizer and its custom-code metadata
- the DS convolutional-state layout, when required
- the GGUF plugin's Python source and CUDA extension digests, when the launch record pins them

For SGLang, the check also parses the serving arguments and confirms that the image's SGLang has each part that the launch wrapper replaces to [keep Mooncake registrations across role switches](../configuration/05-Engine-Launch.md#sglang-runtime-fields). For TensorRT-LLM, it also checks for `trtllm-serve` and reads block reuse from the `--config` file.

The check writes [`checked.json`](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification).

A failed check names the failing package, tokenizer, identity check, or missing SGLang part.

The check uses the tokenizer at `runtime.tokenizer`, or the model directory when that field is unset.

For a container engine, the check logs to `image-check.log` and resolves the tokenizer path in the image and its mounts, including `/model` for `NARWHAL_MODEL_DIR`. For a native engine, the check logs to `runtime-check.log` and resolves the tokenizer path on the host. Each check appends its attempt identifier, the launch-plan hash, and the subprocess output to the check log.

When the launch-plan hash changes, prepare a fresh launch directory. When the launcher changes, build a new deployment package in [Install Narwhal on the hosts](02-Install.md).

Start the engines:

1. Recheck the planned ports with `ss -ltnp`.
2. Start each engine and follow its log:

    ```bash
    narwhal-engine start --run "$ENGINE_RUN"
    export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
    docker logs --follow "$ENGINE_CONTAINER"
    ```

3. Watch the log until the HTTP endpoints come up.
4. Stop the log follower with Ctrl+C.

When startup fails, inspect the container named by `container.id`:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Find the cause of the failure before creating another plan.

When vLLM exits requesting `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, recover in this order:

1. Retain the failed attempt's evidence.
2. Rerun discovery with the corrected inputs into fresh outputs.
3. Build a new deployment package.
4. Install engine 1 into its new deployment directory.
5. Validate engine 1 before updating the rest.
6. Compare the old and new image IDs, model-config hashes, accelerator and TP allocations, runtime settings, and transport.

### Docker command deadlines

Docker commands run within the stage budgets in [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery). Cleanup after a timeout or cancellation follows [Docker command reconciliation](../dev/Recovery-and-Qualification.md#docker-command-reconciliation).

Before you reuse a failed deployment:

1. Inspect the retained partial output.
2. Follow the [stage recovery procedure](../Dev-Runtime.md#stage-deadlines-and-recovery).

## Proving the live HTTP process

Once the engine answers `/health`, probe it from the same engine-role shell into a new directory:

```bash
narwhal-engine probe --run "$ENGINE_RUN" --out "$ENGINE_RUN/probe"
```

The probe reads the engine's credential and served model name from the launch directory. It checks, in order, and stops at the first failure:

1. The plan hash in `checked.json` matches `launch.json`.
2. The engine that `start` recorded is still serving: the container in `container.id` runs the checked image and has not started again since `container-start.json`, or the native process in `native-process.json` is the same process. An endpoint answers for whichever process holds its port, so this keeps an engine left from another launch from passing.
3. `/health` answers with a 2xx status.
4. The engine version equals the checked `engine_version`. vLLM and TensorRT-LLM report it at `/version`, and SGLang at `/server_info`.
5. `/v1/models` lists the plan's `served_model_name`.
6. On vLLM, `/metrics` publishes a `process_start_time_seconds` within 2 seconds of the recorded container start, or equal to the native launch record's.
7. On vLLM and TensorRT-LLM, one completion of `The sea is` with `max_tokens` 32 and `temperature` 0 returns text.

SGLang publishes no process start over HTTP, and serves a completion only with a peer engine for the other leg. [Attestation](05-Attest.md) reads the SGLang process start on the host, and [preflight](06-Profile-and-Preflight.md) sends paired completions.

Each response lands in `--out` at mode 0600 as it arrives: `health.txt`, `version.json`, `models.json`, `metrics.txt` on vLLM and `completion.json` on vLLM and TensorRT-LLM. `version.json` holds only `{"version": ...}`, because `/server_info` also returns the engine's API key. Once every check passes, the probe writes `probe.json` with the role, endpoint, plan hash, checked version, container ID or native process, model, the vLLM process start, and the SHA-256 of each response, then prints a line such as `engine-1: engine health, version, model, process start and completion passed.`

A failed check exits 1 and keeps the responses that led to it, such as `version.json` with the version the engine reported. If the probe reports another version, compare the container that serves the endpoint with the recorded image and container ID. The probe refuses an `--out` directory that already holds its files, so probe again into a new directory.

- Leave each serving container running through the workload trial.
- Keep the engines idle during fabric qualification and profiling.

## Capturing the live cache layout

For a vLLM or TensorRT-LLM engine, capture the cache layout in each engine-role shell:

```bash
narwhal-engine capture-cache --run "$ENGINE_RUN"
```

`capture-cache` writes `cache-layout.json` with one record per TP rank.

`narwhal-engine` rejects `capture-cache` for an SGLang plan. An SGLang engine's cache geometry is its measured KV handoff instead, which [Measuring KV handoffs](04-Qualify-Fabric.md#measuring-kv-handoffs) measures before the groups are derived.

## Deriving cache-equivalence groups

Engines in one cache-equivalence group share one fabric budget. Two engine roles share a group when they match in:

- the image ID and the model configuration SHA-256 that discovery observed
- `accelerator`, `tensor_parallel_size`, `gpu_visibility_env`, `runtime` and `transfer.transport` in their launch records
- their live cache geometry: every TP rank's captured layers, page sizes, block sizes and `kv_cache_layout`, or on SGLang the measured series, prompt length and payload bytes per handoff

The group's representative is its lowest-numbered role.

Copy each engine's `cache-layout.json` to the management workstation. On SGLang, `narwhal deploy transfer-measure` has already written each engine's `transfer.json` there. Then, from the management shell with `config/deployment.env` loaded, pass one file for every engine role:

```bash
narwhal deploy cache-groups --run "$PACKAGE_RUN" --out runs/cache-groups/first-deploy \
  --cache-layout engine-1=runs/cache-layouts/engine-1.json \
  --cache-layout engine-2=runs/cache-layouts/engine-2.json
```

On SGLang, pass `--transfer-record engine-<n>=<path>` instead, with the `transfer.json` that [`narwhal deploy transfer-measure`](04-Qualify-Fabric.md#measuring-kv-handoffs) wrote for the role, such as `runs/transfer-measure/first-deploy/engine-1/transfer.json`. `PACKAGE_RUN` is the directory that [`narwhal deploy package`](02-Install.md) wrote, such as `runs/deployment-env/first-deploy`. The command reads the launch records from `NARWHAL_LAUNCH_CONFIG`, each role's discovery record from its `sources.runtime`, and each role's packaged launch record from `--run`. Every cache layout and transfer record names the SHA-256 of the packaged launch record its engine was prepared from, and the command refuses one that names another. It also refuses a role without the file its engine needs, a file for a role that has none, a packaged record that differs from the launch records, a cache layout that lacks a TP rank or was captured from another image or model configuration, and an `--out` directory that already holds `cache-groups.json`.

It prints one line per group, such as:

```text
3f0c2a9d81b4e675: representative=engine-1; roles=engine-1,engine-2
```

and writes `cache-groups.json` at mode 0600 with each group's ID, its static-input `signature`, representative, roles and geometry, and each role's inputs and the SHA-256 of the files it read.

[Qualify the transfer fabric](04-Qualify-Fabric.md)
