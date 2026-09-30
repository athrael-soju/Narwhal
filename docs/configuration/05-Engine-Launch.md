# Engine endpoints and launch records

## 14. Engine endpoints generated from node environments

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

Discovery writes these values to `config/deployment.env` for each engine host:

| Variable                                                      | Value                                                 |
| ------------------------------------------------------------- | ----------------------------------------------------- |
| `NARWHAL_NODE_<n>_IP`                                         | Unique global address of the chosen interface         |
| `NARWHAL_NODE_<n>_URL` and `NARWHAL_NODE_<n>_ATTESTATION_URL` | URLs on that address, with brackets around IPv6 hosts |

Per-node overrides:

| Condition                                              | Set                                                                                                                               |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| The interface has multiple global addresses            | `NARWHAL_NODE_<n>_IP` to an address on the selected fabric interface                                                              |
| The engine service uses another reachable address      | `NARWHAL_NODE_<n>_URL` to the full engine URL, using `http` and an explicit port                                                  |
| The attestation service uses another reachable address | `NARWHAL_NODE_<n>_ATTESTATION_URL` to the full attestation URL, using `http` and an explicit port and ending in `/v1/attestation` |
| A service port changes                                 | The matching per-node port override, `NARWHAL_NODE_<n>_ENGINE_PORT` or `NARWHAL_NODE_<n>_ATTEST_PORT`                             |

The generated fleet uses those derived values:

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

Discovery needs at least two engines with the same GPU and tensor parallel (TP) shape, as in the two engine blocks of `.env.example`.

Discovery adds one fleet record per engine:

| Engine                | Initial role |
| --------------------- | ------------ |
| First engine          | Prefill      |
| Each remaining engine | Decode       |

A single-engine fleet is valid in the fleet schema, with both [role floors](02-Serving-and-Role-Control.md#72-role-floors) at `1`.

Store site-specific fleet files in a [Git-ignored path](06-Fabric-and-Operations.md#20-configuration-provenance-and-publication).

For the profiling, preflight, and serving commands:

1. Load `.env`.
2. Pass `--fleet "$NARWHAL_FLEET"`.

The monitoring stack's scrape-target generator, `tools/observability/make_targets.py`, resolves endpoint URLs with the fleet's [whole-value substitution rules](01-Fleet-Schema.md#13-environment-loading).

---

## 15. Engine launch records

`NARWHAL_LAUNCH_CONFIG` selects the discovery-generated launch-record file on the management workstation, which defaults to `config/engine-launch.local.json`.

Discovery builds one launch record per assigned `engine-<n>` role from the detected GPUs, their device paths, the image package versions, and the launch policy chosen in `.env`.

`config/engine-launch.sources.json` indexes the `sources` of every record.

The [launch-record example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) documents allocation, transport, and runtime fields.

To change GPU allocation or runtime policy:

1. Change the corresponding `.env` policy input.
2. Rerun discovery into a fresh output set.

### 15.1 Allocation and transport fields

| Field                  | Deployment meaning                                                                                                                   |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `accelerator`          | Product identity to compare against host inspection and fleet hardware fields.                                                       |
| `gpu_ids`              | Selected GPU indices or UUIDs for the replica.                                                                                       |
| `tensor_parallel_size` | TP size for the replica.                                                                                                             |
| `gpu_visibility_env`   | GPU visibility variable, `ROCR_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES`.                                                           |
| `accelerator_devices`  | Host device paths mapped into the container, which on ROCm must include `/dev/kfd` and DRI mappings for allocated GPUs.              |
| `network_mode`         | Uses `host` for the recorded network and port allocation.                                                                            |
| `transfer.transport`   | `ucx_tcp` or `ucx_rdma`.                                                                                                             |
| `transfer.net_devices` | Ethernet interfaces for TCP, such as `${NARWHAL_FABRIC_INTERFACE}` from the selected engine environment, or HCA:port names for RDMA. |
| `transfer.devices`     | Transport device paths mapped into the container: the RDMA character devices, or an empty list for TCP.                              |
| `sources`              | Allocation, device, and transfer definitions that produced the record.                                                               |

`prepare` derives these record values:

| Record section | Derived value                        |
| -------------- | ------------------------------------ |
| `environment`  | GPU visibility and `UCX_NET_DEVICES` |
| `vllm_args`    | Value of `--tensor-parallel-size`    |

`install` copies the selected launch record into the engine checkout's `config/` directory.

Launch records and their supporting extracts are Git-ignored files matching `config/engine-launch.*.json`, at mode `0600`.

When preparation reports an error in an `.env` input or a remote prerequisite:

1. Fix the input or prerequisite.
2. Prepare a new run.

---

## 16. Runtime launch records and image verification

Every generated engine record contains a `runtime` object that `launch_engine.py` reads.

Discovery fills the `runtime` object from these sources:

| Value                                                 | Source                                                                          |
| ----------------------------------------------------- | ------------------------------------------------------------------------------- |
| Package pins and supported runtime-environment fields | Selected image                                                                  |
| Model dtype                                           | Model config                                                                    |
| Launch policy                                         | [Environment launch policy](../deploy/01-Discover.md#confirm-the-launch-policy) |

| Runtime field       | Operator input                                                                                                                                                                                                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `expected_packages` | Exact installed versions for `vllm`, `nixl` or `nixl-rocm`, and each image package whose identity `narwhal-engine check` must verify.                                                                                                                                                                  |
| `model_dtype`       | `bfloat16` or `float16`.                                                                                                                                                                                                                                                                               |
| `kv_cache_dtype`    | `auto` or the requested cache dtype.                                                                                                                                                                                                                                                                   |
| `block_size`        | Requested runtime block size.                                                                                                                                                                                                                                                                          |
| `environment`       | Image-local ROCm or CUDA, UCX, NIXL, and library-path settings.                                                                                                                                                                                                                                        |
| `extra_args`        | Model-specific vLLM arguments, such as context and batching limits, memory utilization, the reasoning parser, the attention backend, remote model code, language-only loading, eager execution, async scheduling, hybrid-cache policy, and the [cache opt-outs](#161-prefix-caching-and-cache-events). |

The launcher rejects `extra_args` that override these settings:

| Setting                                                                                    | Source           |
| ------------------------------------------------------------------------------------------ | ---------------- |
| Model mount, served model name, bind family, HTTP port                                     | Role environment |
| TP size                                                                                    | Launch record    |
| `NixlConnector` with `kv_both`, UCX, and failure propagation                               | Launcher         |
| GPU visibility, advertised addresses and ports, transport selection, engine authentication | Launcher         |

Discovery-derived settings take precedence over values in `NARWHAL_ENGINE_ARGS` or `NARWHAL_ENGINE_ENV`:

| Metadata condition                                                             | Derived setting                 |
| ------------------------------------------------------------------------------ | ------------------------------- |
| Checkpoint model or tokenizer metadata contains an `auto_map`                  | `--trust-remote-code`           |
| Model metadata identifies convolutional state-space model (SSM) transfer state | `VLLM_SSM_CONV_STATE_LAYOUT=DS` |

Checks that `narwhal-engine check` runs before model startup:

- `--trust-remote-code` against the mounted checkpoint
- image identity
- exact distribution versions
- connector configuration and import
- checkpoint tokenizer construction
- convolutional-state layout of the pinned image, for SSM models
- resolution of the serving arguments into vLLM's engine configuration

`checked.json` records the launch-plan hash, the image ID, and these values:

| Field              | Value                                                                                 |
| ------------------ | ------------------------------------------------------------------------------------- |
| `vllm_api_version` | Value of `vllm.version.__version__`                                                   |
| `prefix_caching`   | `true` when the resolved engine configuration keeps prefix caching on                 |
| `kv_events`        | The resolved event and replay endpoints, or `null` when cache-event publishing is off |

The check fails when the resolved endpoints differ from `launch.json`.

The [live HTTP process check](../deploy/03-Validate-Engines.md#prove-the-live-http-process) compares the listening engine's `/version` response with `vllm_api_version`.

The image's NIXL connector must implement the fleet's required `kv_both` behavior.

Launch directories, environment files, and runtime captures live under the ignored `runs/`.

Record the application revision, launcher digest, and container ID with each deployment.

### 16.1 Prefix caching and cache events

| Condition                                                              | Prefix caching |
| ---------------------------------------------------------------------- | -------------- |
| vLLM default                                                           | On             |
| `extra_args` contains vLLM's `--no-enable-prefix-caching`              | Off            |
| vLLM's resolved engine configuration turns it off for the model        | Off            |
| vLLM turns it off during model load, for some attention configurations | Off            |

An engine with prefix caching off publishes zero block events.

With prefix caching on, vLLM publishes KV cache events over two ZeroMQ IPC sockets in `/tmp/narwhal-<uid>/<plan name>/`:

| Socket        | Use                                                      |
| ------------- | -------------------------------------------------------- |
| `events.sock` | Published event batches, each with a sequence number     |
| `replay.sock` | Replay requests for batches held in vLLM's replay buffer |

Socket directories:

| Property                  | Value                                                                              |
| ------------------------- | ---------------------------------------------------------------------------------- |
| Mode                      | `0700`, for the launching user                                                     |
| Created by                | Preparation, the check, and each engine start                                      |
| Container mount           | Plan directory at `/narwhal-kv-events`                                             |
| Record                    | `kv_events` in `launch.json`, with the host directory and the endpoints vLLM binds |
| Removal, native engine    | Stopping the engine removes its directory                                          |
| Removal, container engine | Remove the directory after removing the container                                  |

The check and engine start fail when either directory belongs to another user or grants group or other access.

To keep prefix caching on and turn event publishing off, add vLLM's own event setting to `extra_args`:

```json
["--kv-events-config", "{\"enable_kv_cache_events\": false}"]
```

The launcher rejects every other `--kv-events-config` value.
