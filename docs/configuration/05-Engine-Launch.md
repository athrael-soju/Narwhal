---
description: Engine endpoints, engine launch records and runtime image verification for a Narwhal fleet.
---

# Engine endpoints and launch records

## Engine endpoints generated from node environments

An engine URL points to the running vLLM HTTP service, for example:

```text
http://10.0.0.11:8000
```

The engine URL can use the engine's IP address and port directly when the router can reach them.

The attestation sidecar has a separate URL, for example:

```text
http://10.0.0.11:8010/v1/attestation
```

Use the ports your engine deployment configures.

For generated deployment inputs, provide these values in `.env`:

```text
NARWHAL_FABRIC_INTERFACE
NARWHAL_ENGINE_PORT
NARWHAL_ATTEST_PORT
```

For each engine `<n>`, discovery writes `NARWHAL_NODE_<n>_IP` to `config/deployment.env` with the unique global address of the chosen interface. It writes `NARWHAL_NODE_<n>_URL` and `NARWHAL_NODE_<n>_ATTESTATION_URL` as URLs on that address, with brackets around IPv6 hosts.

Set a per-node override for each of these conditions:

| Condition                                              | Set                                                                                                   |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| The interface has multiple global addresses            | `NARWHAL_NODE_<n>_IP` to a global address on the selected fabric interface                            |
| The engine service uses another reachable address      | `NARWHAL_NODE_<n>_URL` to `http://<host>:<port>`                                                      |
| The attestation service uses another reachable address | `NARWHAL_NODE_<n>_ATTESTATION_URL` to `http://<host>:<port>/v1/attestation`                           |
| A service port changes                                 | The matching per-node port override, `NARWHAL_NODE_<n>_ENGINE_PORT` or `NARWHAL_NODE_<n>_ATTEST_PORT` |

The generated fleet references the derived variables:

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

Discovery needs at least two engines with the same accelerator product, GPU count, and tensor parallel (TP) size.

Discovery adds one fleet record per engine. The first engine starts in the prefill role, and each remaining engine starts in decode.

A single-engine fleet is valid in the fleet schema, with both [role floors](02-Serving-and-Role-Control.md#role-floors) at `1`.

Store per-site fleet files in a [Git-ignored path](06-Fabric-and-Operations.md#configuration-provenance-and-publication).

For the profiling, preflight, and serving commands:

1. Load `.env` and `config/deployment.env`.
2. Pass `--fleet "$NARWHAL_FLEET"`.

The monitoring stack's scrape-target generator, `tools/observability/make_targets.py`, resolves endpoint URLs with the fleet's [whole-value substitution rules](01-Fleet-Schema.md#environment-loading).


## Engine launch records

`NARWHAL_LAUNCH_CONFIG` selects the discovery-generated launch-record file on the management workstation (default `config/engine-launch.local.json`).

Discovery builds one launch record per assigned `engine-<n>` role from these inputs:

- the detected GPUs
- their device paths
- the image package versions
- the launch policy chosen in `.env`

`config/engine-launch.sources.json` maps each role to its inspection file and launch-policy source.

The [launch-record example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) documents allocation, transport, and runtime fields.

To change GPU allocation or runtime policy:

1. Change the corresponding `.env` policy input.
2. Rerun discovery into a fresh output set.

### Allocation and transport fields

Each launch record holds these allocation and transport fields:

| Field                  | Deployment meaning                                                                                             |
| ---------------------- | -------------------------------------------------------------------------------------------------------------- |
| `accelerator`          | Product identity to compare against host inspection and fleet hardware fields.                                 |
| `gpu_ids`              | Selected GPU indices or UUIDs for the replica.                                                                 |
| `tensor_parallel_size` | TP size for the replica.                                                                                       |
| `gpu_visibility_env`   | GPU visibility variable, `ROCR_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES`.                                     |
| `accelerator_devices`  | Host device paths mapped into the container, including `/dev/kfd` and DRI mappings for allocated GPUs on ROCm. |
| `network_mode`         | `host`, for the recorded network and port allocation.                                                          |
| `transfer.transport`   | `ucx_tcp` or `ucx_rdma`.                                                                                       |
| `transfer.net_devices` | Ethernet interface names for TCP, or HCA:port names for RDMA.                                                  |
| `transfer.devices`     | Transport device paths mapped into the container, required for RDMA.                                           |
| `transfer.gpu_tls`     | UCX GPU transport, `cuda` (default) or `cuda_copy` on CUDA, and `rocm` on ROCm.                                |
| `sources`              | Allocation, device, transfer, and runtime definitions that produced the record.                                |

`deploy_hosts.py prepare` derives GPU visibility and `UCX_NET_DEVICES` in the record's `environment`, and `--tensor-parallel-size` with the record's TP size in `vllm_args`.

A CUDA engine with dedicated GPUs, colocated with other such engines on its host, exports its `gpu_ids` followed by the GPUs of those engines as its GPU visibility. A shared-device engine exports its single GPU, and every other engine exports its `gpu_ids`.

Colocated CUDA engines with dedicated GPUs run on their allocated GPUs. With `transfer.gpu_tls` set to `cuda`, they transfer KV to each other through CUDA IPC. Under the container backend, their containers share the host PID namespace.

For [CUDA IPC engines](../concepts/03-Failure-and-State.md#peer-memory-release), colocated with dedicated GPUs or sharing a device, with `transfer.gpu_tls` set to `cuda`, `UCX_CUDA_IPC_CACHE` takes the `runtime.environment` value, otherwise the UCX default. The vLLM NIXL `engine_ttl` for these engines is 60 seconds when `UCX_CUDA_IPC_CACHE` is `n`.

`install` copies the selected launch record into the engine checkout's `config/` directory.

Launch records and their supporting extracts are Git-ignored files matching `config/engine-launch.*.json`, at mode `0600`.

When preparation reports an error in an `.env` input or a remote prerequisite:

1. Fix the input or prerequisite.
2. Prepare a new run.


## Runtime launch records and image verification

Every generated engine record contains a `runtime` object that `narwhal-engine` reads.

Discovery fills the `runtime` object with package pins and supported runtime-environment fields from the selected image, the model dtype from the model config, and the launch policy from the [environment launch policy](../deploy/01-Discover.md#confirming-the-launch-policy).

Operator input for each `runtime` field:

| Runtime field       | Operator input                                                                                                                                                       |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend`           | Optional registered engine backend that builds and checks the launch, default `vllm`. The fields below are the `vllm` backend's.                                    |
| `expected_packages` | Exact installed versions for `vllm`, `nixl` or `nixl-rocm`, and each image package whose identity `narwhal-engine check` must verify.                                |
| `model_dtype`       | `bfloat16` or `float16`.                                                                                                                                             |
| `kv_cache_dtype`    | `auto`.                                                                                                                                                              |
| `block_size`        | Positive runtime block size.                                                                                                                                         |
| `kv_lease_s`        | Optional NIXL producer lease in whole seconds, at least 6, default `30`. The launcher passes it as `kv_lease_duration`, and the router derives the [KV handoff bound](02-Serving-and-Role-Control.md#kv-handoff-bound) from it. |
| `environment`       | Image-local `LD_LIBRARY_PATH`, `PYTHONPATH`, and variables with a `VLLM_`, `UCX_`, `NIXL_`, `ROCM_`, `HIP_`, `HSA_`, `AITER_`, `PYTORCH_`, or `SAFETENSORS_` prefix. |
| `extra_args`        | vLLM options from the `extra_args` allowlist.                                                                                                                        |

The `extra_args` allowlist accepts these options with a value: `--max-model-len`, `--gpu-memory-utilization`, `--max-num-batched-tokens`, `--max-num-seqs`, `--reasoning-parser`, `--attention-backend`, `--tokenizer`, `--hf-config-path`, `--load-format` and `--kv-events-config`.

It accepts these flags: `--trust-remote-code`, `--language-model-only`, `--enforce-eager`, `--async-scheduling`, `--disable-hybrid-kv-cache-manager`, `--no-disable-hybrid-kv-cache-manager`, `--enable-prefix-caching` and `--no-enable-prefix-caching`.

The launcher rejects every other `extra_args` option.

The launcher sets these values itself:

| Setting                                                                                    | Source           |
| ------------------------------------------------------------------------------------------ | ---------------- |
| Model mount, served model name, bind family, HTTP port                                     | Role environment |
| TP size                                                                                    | Launch record    |
| `NixlConnector` with `kv_both`, UCX, failure propagation, and `kv_lease_duration`          | Launcher         |
| GPU visibility, advertised addresses and ports, transport selection, engine authentication | Launcher         |

`runtime.environment` rejects the launcher-managed variables `ROCR_VISIBLE_DEVICES`, `CUDA_VISIBLE_DEVICES`, `UCX_NET_DEVICES`, `UCX_TLS`, `UCX_TCP_PORT_RANGE`, `NIXL_HOST_IP`, `VLLM_NIXL_SIDE_CHANNEL_HOST`, `VLLM_NIXL_SIDE_CHANNEL_PORT` and `VLLM_API_KEY`. It also rejects names containing `PASSWORD`, `TOKEN`, `SECRET`, `API_KEY`, `SSH` or `SKIP_COMPAT`.

When the checkpoint model or tokenizer metadata contains an `auto_map`, discovery adds `--trust-remote-code` to the arguments configured in `NARWHAL_ENGINE_ARGS`. When model metadata identifies convolutional state-space model (SSM) transfer state, discovery sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`, and any other layout value in `NARWHAL_ENGINE_ENV` fails discovery.

Checks that `narwhal-engine check` runs before model startup:

- `--trust-remote-code` against the mounted checkpoint
- image identity, for the container backend
- exact distribution versions
- connector configuration and import
- checkpoint tokenizer construction
- convolutional-state layout of the pinned image, for SSM models
- resolution of the serving arguments into vLLM's engine configuration

`checked.json` records these values:

| Field               | Value                                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------------- |
| `plan_sha256`       | SHA-256 of `launch.json`                                                                           |
| `vllm_api_version`  | Value of `vllm.version.__version__`                                                                |
| `prefix_caching`    | `true` when the resolved engine configuration keeps prefix caching on                              |
| `kv_events`         | The resolved event and replay endpoints, or `null` when cache-event publishing is off              |
| `ucx_version`       | Version of the `libucp` bundled with the NIXL package, otherwise of the system `libucp`, or `null` |
| `peer_release`      | `true` when this engine releases a stopped peer's KV memory                                        |
| `image_id`          | Local image ID, for the container backend                                                          |
| `backend`           | `native`, for the native backend                                                                   |
| `python_executable` | The launch plan's Python interpreter, for the native backend                                       |
| `expected_packages` | The launch plan's pinned package versions, for the native backend                                  |

The check fails when the resolved endpoints differ from `launch.json`.

With `peer_release: false`, the check prints a warning that this engine keeps a stopped peer's GPU memory mapped.

The [live HTTP process check](../deploy/03-Validate-Engines.md#proving-the-live-http-process) compares the listening engine's `/version` response with `vllm_api_version`.

The image's NIXL connector must implement the fleet's required `kv_both` behavior.

### SGLang runtime fields

With `NARWHAL_ENGINE_BACKEND=sglang`, [discovery](../deploy/01-Discover.md#confirming-the-launch-policy) writes these `runtime` fields:

| Runtime field                 | Value                                                                                                                                                                                                                                      |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `backend`                     | `sglang`.                                                                                                                                                                                                                                  |
| `connector`                   | `mooncake`, the default, or `nixl`, from `NARWHAL_ENGINE_CONNECTOR`.                                                                                                                                                                       |
| `expected_packages`           | Exact installed versions for `sglang`, the connector's transfer package (`mooncake-transfer-engine` or `nixl`, or a CUDA build such as `mooncake-transfer-engine-cuda13`), and each image package that `narwhal-engine check` must verify. |
| `model_dtype`                 | `bfloat16` or `float16`.                                                                                                                                                                                                                   |
| `kv_cache_dtype`              | `auto`.                                                                                                                                                                                                                                    |
| `role`                        | The engine's initial pool, `prefill` or `decode`, required with `nixl` and rejected with `mooncake`.                                                                                                                                       |
| `decode_cuda_graph_memory_gb` | Positive GB of decode CUDA graph memory that the engine reserves when it switches to decode, required with `mooncake`, from `NARWHAL_DECODE_CUDA_GRAPH_MEMORY_GB`.                                                                         |
| `environment`                 | Image-local `LD_LIBRARY_PATH`, `PYTHONPATH`, and variables with a `SGLANG_`, `MOONCAKE_`, `MC_`, `UCX_`, `NIXL_`, `NCCL_`, `PYTORCH_`, or `SAFETENSORS_` prefix.                                                                           |
| `extra_args`                  | SGLang options from the `extra_args` allowlist.                                                                                                                                                                                            |

The model path names a checkpoint directory. The launcher rejects a GGUF file.

A `mooncake` engine launches as prefill with `--enable-pd-role-switch`, and the router switches each decode engine through `POST /pd_role_switch` and swaps roles at runtime. A `nixl` engine serves the role in `runtime.role` for its lifetime, so the fleet sets `pin` on every engine.

The `extra_args` allowlist accepts these options with a value: `--context-length`, `--mem-fraction-static`, `--chunked-prefill-size`, `--max-total-tokens`, `--max-prefill-tokens`, `--tokenizer-path`, `--reasoning-parser`, `--load-format`, `--disaggregation-ib-device`, `--mamba-ssm-dtype` and `--max-running-requests`, set once from 1 to 256. It accepts these flags: `--trust-remote-code`, `--disable-radix-cache`, `--disable-cuda-graph` and `--enable-mixed-chunk`.

The launcher sets `--page-size 1`, `--attention-backend triton`, `--max-running-requests 256` unless `extra_args` sets it, `--stream-interval 1`, `--enable-metrics`, `--enable-cache-report`, the disaggregation mode and transfer backend, and `--kv-events-config` unless `--disable-radix-cache` is set. It passes `--disaggregation-bootstrap-port` from `NARWHAL_SGLANG_BOOTSTRAP_PORT` in the role environment, and the engine credential as `--api-key`.

`runtime.environment` rejects the launcher-managed variables `CUDA_VISIBLE_DEVICES`, `UCX_NET_DEVICES`, `UCX_TLS`, `UCX_TCP_PORT_RANGE`, `SGLANG_HOST_IP`, `SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT`, `NARWHAL_SGLANG_BOOTSTRAP_HOST`, `NARWHAL_SGLANG_BOOTSTRAP_PORT` and `NARWHAL_SGLANG_API_KEY`. It also rejects names containing `PASSWORD`, `TOKEN`, `SECRET`, `API_KEY` or `SSH`.

For an SGLang engine, `checked.json` records `sglang_version` in place of `vllm_api_version`, and has no `ucx_version` or `peer_release`.

Launch directories, environment files, and runtime captures live under the Git-ignored `runs/`.

Record the application revision, launcher digest, and container ID with each deployment.

### Prefix caching and cache events

vLLM turns prefix caching on by default. Prefix caching is off when `extra_args` contains vLLM's `--no-enable-prefix-caching`, when vLLM's resolved engine configuration turns it off for the model, or when vLLM turns it off during model load for some attention configurations.

An engine with prefix caching off publishes zero block events.

With prefix caching on, vLLM publishes KV cache events over two ZeroMQ IPC sockets in `/tmp/narwhal-<uid>/<plan name>/`. `events.sock` carries published event batches, each with a sequence number. `replay.sock` takes replay requests for batches held in vLLM's replay buffer.

In the path, `<uid>` is the effective user ID of the launching user and `<plan name>` is `narwhal-engine-<n>-<12 hex>`.

Each socket directory has these properties:

| Property        | Value                                                                              |
| --------------- | ---------------------------------------------------------------------------------- |
| Mode            | `0700`, for the launching user                                                     |
| Created by      | Preparation, the check, and each engine start                                      |
| Container mount | Plan directory at `/narwhal-kv-events`                                             |
| Record          | `kv_events` in `launch.json`, with the host directory and the endpoints vLLM binds |

Stopping a native engine removes its plan directory. For a container engine, remove the directory after removing the container.

Preparation, the check, and engine start fail when either directory belongs to another user or grants group or other access.

To keep prefix caching on and turn event publishing off, add vLLM's event setting to `extra_args`:

```json
["--kv-events-config", "{\"enable_kv_cache_events\": false}"]
```

The launcher rejects every other `--kv-events-config` value.

### Resetting prefix caches

vLLM serves `POST /reset_prefix_cache` on an engine started with `VLLM_SERVER_DEV_MODE=1`. The route empties the engine's prefix cache.

1. Add the variable to `runtime.environment` in the engine's launch record:

    ```json
    "environment": {"VLLM_SERVER_DEV_MODE": "1"}
    ```

2. Prepare and start the engine from the updated launch record.
3. [Profile the engine](../measure/01-Profile.md#reusing-or-creating-an-idle-fleet-latency-profile) for its new [`launch_digest`](01-Fleet-Schema.md#attestation).
4. If the fleet configures a first-token calibration artifact, [calibrate the first-token deadline](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).

While the router is idle, reset the engine's prefix cache, with `<engine-url>` replaced by the engine's URL:

```bash
curl -fsS -X POST '<engine-url>/reset_prefix_cache'
```

Expected output:

```text
{"success":true}
```
