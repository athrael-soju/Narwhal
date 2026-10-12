---
description: Engine endpoints, engine launch records and runtime image verification for a Narwhal fleet.
---

# Engine endpoints and launch records

## Engine endpoints generated from node environments

An engine URL is the base URL of the engine's HTTP server, such as `http://10.0.0.11:8000`. The attestation sidecar URL ends in `/v1/attestation`, such as `http://10.0.0.11:8010/v1/attestation`. The router must reach both.

Discovery builds both URLs from these `.env` values:

```text
NARWHAL_FABRIC_INTERFACE
NARWHAL_ENGINE_PORT
NARWHAL_ATTEST_PORT
```

For each engine `<n>`, discovery writes `NARWHAL_NODE_<n>_IP` to `config/deployment.env` with the global address of the selected interface, leaving out loopback, link-local and multicast addresses. It writes `NARWHAL_NODE_<n>_URL` and `NARWHAL_NODE_<n>_ATTESTATION_URL` as HTTP URLs on that address, with brackets around an IPv6 host.

Set a per-node override in `.env` for each of these conditions:

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

Discovery needs at least two engines with the same accelerator product, GPU count and tensor parallel (TP) size. It adds one fleet record per engine. The first engine starts in the prefill role and every other engine in decode.

A fleet you write by hand can hold a single engine, with both [role floors](02-Serving-and-Role-Control.md#role-floors) at `1`.

Keep per-site fleet files private, as [Configuration provenance and publication](06-Fabric-and-Operations.md#configuration-provenance-and-publication) describes.

For the profiling, preflight and serving commands:

1. Load `.env` and `config/deployment.env`.
2. Pass `--fleet "$NARWHAL_FLEET"`.

The monitoring stack's scrape-target generator, `tools/observability/make_targets.py`, resolves endpoint URLs with the fleet's [whole-value substitution rules](01-Fleet-Schema.md#environment-loading).


## Engine launch records

`NARWHAL_LAUNCH_CONFIG` selects the launch-record file on the management workstation, `config/engine-launch.local.json` by default. Discovery writes one launch record per `engine-<n>` role from the detected GPUs and their device paths, the image's package versions, the model configuration, and the [launch policy](../deploy/01-Discover.md#confirming-the-launch-policy) in `.env`. It writes `engine-launch.sources.json` beside the file, mapping each role to its inspection file and policy source.

The [launch-record example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) shows a complete record.

To change GPU allocation or runtime policy:

1. Change the launch-policy input in `.env`.
2. Move the previous generated files aside, then rerun discovery with a new `--out` directory.

### Allocation and transport fields

Each launch record holds these allocation and transport fields:

| Field                  | Deployment meaning                                                                                                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `accelerator`          | Product name from host inspection, compared against the fleet's hardware fields.                                                                                                                   |
| `gpu_ids`              | Distinct GPU indices or UUIDs for the replica.                                                                                                                                                     |
| `tensor_parallel_size` | TP size for the replica, from 1 to the number of `gpu_ids`.                                                                                                                                        |
| `gpu_visibility_env`   | GPU visibility variable, `ROCR_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES`.                                                                                                                         |
| `accelerator_devices`  | Host device paths mapped into the container. On ROCm they include `/dev/kfd` and the DRI render device of each allocated GPU.                                                                      |
| `network_mode`         | `host`.                                                                                                                                                                                            |
| `transfer.transport`   | `ucx_tcp` or `ucx_rdma`.                                                                                                                                                                           |
| `transfer.net_devices` | Ethernet interface names for `ucx_tcp`, or `HCA:port` names for `ucx_rdma`.                                                                                                                        |
| `transfer.devices`     | Transport device paths mapped into the container, required for `ucx_rdma`.                                                                                                                         |
| `transfer.gpu_tls`     | UCX GPU transport: `cuda`, the default, or `cuda_copy` on CUDA, and `rocm` on ROCm.                                                                                                                |
| `shared_device`        | Optional allocation that shares one GPU with other engines, with the fields of the fleet's [`shared_device`](01-Fleet-Schema.md#shared-device-allocation). It requires one CUDA GPU and TP size 1. |
| `sources`              | Allocation, device, transfer and runtime definitions that produced the record.                                                                                                                     |

`deploy_hosts.py prepare` adds the record's `environment`, with GPU visibility and `UCX_NET_DEVICES`, and the TP argument: `--tensor-parallel-size` in `vllm_args` on vLLM, `--tp-size` in `sglang_args` on SGLang. `install` copies each engine's record into the engine checkout as `config/engine-launch.engine-<n>.json`.

A CUDA engine with dedicated GPUs exports its own `gpu_ids` first, followed by the GPUs of the other such engines on its host. It runs on its own GPUs. A shared-device engine exports only its own GPU. Under the container backend, an engine that exports another engine's GPUs runs in the host PID namespace.

On vLLM, an engine with CUDA IPC peers is one with `transfer.gpu_tls` set to `cuda` that shares a GPU or exports another engine's GPUs. [Peer memory release](../concepts/03-Failure-and-State.md#peer-memory-release) depends on `UCX_CUDA_IPC_CACHE` in `runtime.environment`. When that value is `n`, `no` or `0`, the launcher sets the NIXL `engine_ttl` to 60 seconds for that engine.

When preparation reports an error in an `.env` input or a remote prerequisite:

1. Fix the input or prerequisite.
2. Prepare a new run.


## Runtime launch records and image verification

Each launch record holds a `runtime` object that [`narwhal-engine`](../cli/Engine.md) reads. Discovery fills it with the image's package pins and supported environment, the model dtype from the model configuration, and the launch policy from `.env`.

These `runtime` fields apply to both backends:

| Runtime field       | Value                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------- |
| `backend`           | `vllm`, the default, or `sglang`.                                                                             |
| `expected_packages` | Exact installed versions that `narwhal-engine check` verifies, including the engine and its transfer package. |
| `model_dtype`       | `bfloat16` or `float16`, passed as `--dtype`.                                                                 |
| `kv_cache_dtype`    | `auto`, passed as `--kv-cache-dtype`.                                                                         |
| `environment`       | Image-local `LD_LIBRARY_PATH`, `PYTHONPATH`, and variables with one of the backend's prefixes.                |
| `extra_args`        | Engine options from the backend's allowlist.                                                                  |

Variable names in `environment` must be uppercase, and values must fit on one line. The launcher sets the model path, served model name, bind address, HTTP port, TP size, KV transfer settings, GPU visibility, advertised addresses and ports, `UCX_TLS`, `UCX_TCP_PORT_RANGE` and the engine credential. [Engine environment](../cli/Engine.md#engine-environment) lists the variables it writes.

When the model's `config.json` or `tokenizer_config.json` contains an `auto_map`, discovery adds `--trust-remote-code` to the engine arguments.

### vLLM runtime fields

A vLLM record adds these fields:

| Runtime field | Value                                                                                                                                                                                                                           |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `block_size`  | Positive cache block size, passed as `--block-size`.                                                                                                                                                                            |
| `kv_lease_s`  | Optional NIXL producer lease in whole seconds, at least 6, default `30`. The launcher passes it as `kv_lease_duration`, and the router derives the [KV handoff bound](02-Serving-and-Role-Control.md#kv-handoff-bound) from it. |

`expected_packages` must pin `vllm` and either `nixl` or `nixl-rocm`. `environment` accepts the prefixes `VLLM_`, `UCX_`, `NIXL_`, `ROCM_`, `HIP_`, `HSA_`, `AITER_`, `PYTORCH_` and `SAFETENSORS_`. It rejects the launcher-managed variables `ROCR_VISIBLE_DEVICES`, `CUDA_VISIBLE_DEVICES`, `UCX_NET_DEVICES`, `UCX_TLS`, `UCX_TCP_PORT_RANGE`, `NIXL_HOST_IP`, `VLLM_NIXL_SIDE_CHANNEL_HOST`, `VLLM_NIXL_SIDE_CHANNEL_PORT` and `VLLM_API_KEY`, and names containing `PASSWORD`, `TOKEN`, `SECRET`, `API_KEY`, `SSH` or `SKIP_COMPAT`.

The `extra_args` allowlist accepts these options with a value: `--max-model-len`, `--gpu-memory-utilization`, `--max-num-batched-tokens`, `--max-num-seqs`, `--reasoning-parser`, `--attention-backend`, `--tokenizer`, `--hf-config-path`, `--load-format` and `--kv-events-config`. It accepts these flags: `--trust-remote-code`, `--language-model-only`, `--enforce-eager`, `--async-scheduling`, `--disable-hybrid-kv-cache-manager`, `--no-disable-hybrid-kv-cache-manager`, `--enable-prefix-caching` and `--no-enable-prefix-caching`. `deploy_hosts.py prepare` requires exactly one positive `--max-num-seqs`.

The launcher configures `NixlConnector` with `kv_role` `kv_both`, the UCX backend, `kv_load_failure_policy` `fail`, `enforce_handshake_compat` `true` and `kv_lease_duration`.

When model metadata identifies convolutional state-space model (SSM) transfer state, discovery sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`, and any other value of that variable in `NARWHAL_ENGINE_ENV` fails discovery.

### SGLang runtime fields

With `NARWHAL_ENGINE_BACKEND=sglang`, [discovery](../deploy/01-Discover.md#confirming-the-launch-policy) sets `runtime.backend` to `sglang` and writes these fields:

| Runtime field                 | Value                                                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `connector`                   | `mooncake`, from `NARWHAL_ENGINE_CONNECTOR`.                                                                                                |
| `decode_cuda_graph_memory_gb` | Positive GB of CUDA graph memory that the engine reserves when it switches to decode, from `NARWHAL_DECODE_CUDA_GRAPH_MEMORY_GB`. Required. |

`expected_packages` must pin `sglang` and the connector's transfer package, `mooncake-transfer-engine`, or one CUDA build of it such as `mooncake-transfer-engine-cuda13`. `environment` accepts the prefixes `SGLANG_`, `MOONCAKE_`, `MC_`, `UCX_`, `NIXL_`, `NCCL_`, `PYTORCH_` and `SAFETENSORS_`. It rejects the launcher-managed variables `CUDA_VISIBLE_DEVICES`, `UCX_NET_DEVICES`, `UCX_TLS`, `UCX_TCP_PORT_RANGE`, `SGLANG_HOST_IP`, `SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT`, `NARWHAL_SGLANG_BOOTSTRAP_HOST`, `NARWHAL_SGLANG_BOOTSTRAP_PORT` and `NARWHAL_SGLANG_API_KEY`, and names containing `PASSWORD`, `TOKEN`, `SECRET`, `API_KEY` or `SSH`.

The `extra_args` allowlist accepts these options with a value: `--context-length`, `--mem-fraction-static`, `--chunked-prefill-size`, `--max-total-tokens`, `--max-prefill-tokens`, `--tokenizer-path`, `--reasoning-parser`, `--load-format`, `--disaggregation-ib-device`, `--mamba-ssm-dtype`, `--max-mamba-cache-size`, `--mamba-radix-cache-strategy`, `--kv-events-config` with the value in [Prefix caching and cache events](#prefix-caching-and-cache-events), and `--max-running-requests`, set once from 1 to 256. It accepts these flags: `--trust-remote-code`, `--disable-radix-cache`, `--disable-cuda-graph`, `--enable-mixed-chunk` and `--disable-overlap-schedule`.

The launcher sets `--page-size 1`, `--attention-backend triton`, `--max-running-requests 256` unless `extra_args` sets it, `--stream-interval 1`, `--enable-metrics`, `--enable-cache-report`, `--disaggregation-mode` with the launch role, and `--disaggregation-transfer-backend` with the connector. It adds `--kv-events-config` unless `--disable-radix-cache` or `extra_args` turns cache events off. At engine start it passes `--disaggregation-bootstrap-port` from `NARWHAL_SGLANG_BOOTSTRAP_PORT` and the engine credential, when one is configured, as `--api-key`.

Every engine launches as prefill with `--enable-pd-role-switch`, and the router switches its role at runtime through `POST /pd_role_switch`. Unless `--disable-radix-cache` is set, the launcher also adds `--disaggregation-decode-enable-radix-cache`, so that a switched decode engine evicts cached KV to admit requests.

For a model with SSM state, discovery adds `--mamba-radix-cache-strategy no_buffer` and `--disable-overlap-schedule` to every SGLang engine, unless the engine arguments already set `--mamba-radix-cache-strategy` or `--disable-radix-cache`. When your `NARWHAL_ENGINE_ARGS` sets `--mamba-radix-cache-strategy`, set it to `no_buffer` and add `--disable-overlap-schedule`. While the overlap scheduler is on, SGLang's default strategy selects `extra_buffer` for a model that supports it, and a switched decode engine exits under sustained load.

`--disaggregation-ib-device` names the InfiniBand devices an engine uses for KV transfer. Without it, SGLang detects the devices itself. Give each engine on a host its own device.

`narwhal-engine prepare` rejects a native SGLang launch whose model path is a GGUF file.

### Runtime check

`narwhal-engine check` runs these checks before model startup:

- `--trust-remote-code` against the mounted checkpoint's metadata
- image identity, for the container backend
- exact distribution versions
- the cache-event endpoints that the serving arguments resolve to
- on vLLM, KV connector configuration and import, checkpoint tokenizer construction, the convolutional-state layout of the pinned image for SSM models, and resolution of the serving arguments into vLLM's engine configuration
- on SGLang, parsing of the serving arguments by SGLang's server argument parser

The check fails when the resolved cache-event endpoints differ from `launch.json`. It writes `checked.json` with these values:

| Field               | Value                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------- |
| `plan_sha256`       | SHA-256 of `launch.json`                                                                                      |
| `vllm_api_version`  | vLLM only: value of `vllm.version.__version__`                                                                |
| `sglang_version`    | SGLang only: value of `sglang.__version__`                                                                    |
| `prefix_caching`    | `true` when the resolved serving arguments keep prefix caching on                                             |
| `kv_events`         | The resolved event and replay endpoints, or `null` when cache-event publishing is off                         |
| `ucx_version`       | vLLM only: version of the `libucp` bundled with the NIXL package, otherwise of the system `libucp`, or `null` |
| `peer_release`      | vLLM only: `false` when this engine keeps a stopped CUDA IPC peer's GPU memory mapped                         |
| `image_id`          | Local image ID, for the container backend                                                                     |
| `backend`           | `native`, for the native backend                                                                              |
| `python_executable` | The launch plan's Python interpreter, for the native backend                                                  |
| `expected_packages` | The launch plan's pinned package versions, for the native backend                                             |

`peer_release` is `true` for an engine without CUDA IPC peers. For an engine with CUDA IPC peers, it is `true` only when UCX is 1.22 or later and `UCX_CUDA_IPC_CACHE` is off. When it is `false`, the check prints a warning to recover a crashed engine with a whole-wave restart.

On vLLM, the [live HTTP process check](../deploy/03-Validate-Engines.md#proving-the-live-http-process) compares the engine's `/version` response with `vllm_api_version`. For a native engine, `narwhal-engine start-shared` compares the version from `/version` on vLLM, or from `/server_info` on SGLang, with the checked version.

Record the application revision, launcher digest and container ID with each deployment.

### Prefix caching and cache events

On vLLM, `--no-enable-prefix-caching` in `extra_args` turns prefix caching off, and `checked.json` records whether vLLM's resolved engine configuration keeps it on. On SGLang, `--disable-radix-cache` in `extra_args` turns prefix caching off.

With prefix caching on, the engine publishes KV cache events over two ZeroMQ IPC sockets in `/tmp/narwhal-<uid>/<plan name>/`. `events.sock` carries published event batches, each with a sequence number. `replay.sock` takes replay requests for batches held in the engine's replay buffer. With prefix caching off, the engine publishes no cache events.

In the path, `<uid>` is the effective user ID of the launching user and `<plan name>` is `narwhal-engine-<n>-<12 hex>`.

Each socket directory has these properties:

| Property        | Value                                                                                    |
| --------------- | ---------------------------------------------------------------------------------------- |
| Mode            | `0700`, for the launching user                                                           |
| Created by      | Preparation, the check, and each engine start                                            |
| Container mount | Plan directory at `/narwhal-kv-events`                                                   |
| Record          | `kv_events` in `launch.json`, with the host directory and the endpoints the engine binds |

Stopping a native engine removes its plan directory. For a container engine, remove the directory after removing the container.

Preparation, the check and engine start fail when either directory belongs to another user or grants group or other access.

To keep prefix caching on and turn event publishing off, add the backend's event setting to `extra_args`. On vLLM:

```json
["--kv-events-config", "{\"enable_kv_cache_events\": false}"]
```

On SGLang:

```json
["--kv-events-config", "{\"publisher\": \"null\"}"]
```

The launcher rejects every other `--kv-events-config` value.

### Resetting prefix caches

An SGLang engine empties its cache on `POST /flush_cache`.

vLLM serves `POST /reset_prefix_cache` only on an engine started with `VLLM_SERVER_DEV_MODE=1`. The route empties the engine's prefix cache. To enable it:

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
