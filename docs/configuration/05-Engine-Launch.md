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

Discovery reads the chosen interface on each engine host. It writes the interface's unique global address to `NARWHAL_NODE_<n>_IP` in `config/deployment.env`. The engine and attestation URLs come from that address, with brackets around IPv6 hosts.

If the interface has multiple global addresses, set `NARWHAL_NODE_<n>_IP` explicitly so the value matches an address on the selected fabric interface.

If an engine or attestation service must use another reachable address, set its full URL in `NARWHAL_NODE_<n>_URL` or `NARWHAL_NODE_<n>_ATTESTATION_URL`. Each URL must use `http` with an explicit port, and an attestation URL must end in `/v1/attestation`.

If you change a service port, set the matching per-node port override, `NARWHAL_NODE_<n>_ENGINE_PORT` or `NARWHAL_NODE_<n>_ATTEST_PORT`, as well.

The generated fleet uses those derived values:

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

Discovery adds one fleet record per engine. The first engine gets the prefill role and the rest get decode. Discovery needs at least two engines with the same GPU and tensor parallel (TP) shape. `.env.example` shows a shared fabric interface and two engine blocks.

The fleet schema accepts a single-engine fleet; see [Role floors](02-Serving-and-Role-Control.md#72-role-floors).

Store site-specific fleet files in a Git-ignored path, as listed in [Configuration provenance and publication](06-Fabric-and-Operations.md#20-configuration-provenance-and-publication).

After loading `.env`, pass `--fleet "$NARWHAL_FLEET"` to the profiling, preflight, and serving commands.

The monitoring stack's scrape-target generator, `tools/observability/make_targets.py`, resolves endpoint URLs with the same [whole-value substitution rules](01-Fleet-Schema.md#13-environment-loading).

---

## 15. Engine launch records

`NARWHAL_LAUNCH_CONFIG` selects the discovery-generated launch-record file on the management workstation, which defaults to `config/engine-launch.local.json`.

Discovery builds one launch record per assigned `engine-<n>` role from the detected GPUs, their device paths, the image package versions, and the launch policy chosen in `.env`.

Each record lists its inspection and policy inputs under `sources`. `config/engine-launch.sources.json` indexes them.

The [launch-record example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) documents allocation, transport, and runtime fields.

To change GPU allocation or runtime policy, change the corresponding `.env` policy input and rerun discovery into a fresh output set.

### 15.1 Allocation and transport fields

| Field                  | Deployment meaning                                                                                                                   |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `accelerator`          | Product identity. Compare against host inspection and fleet hardware fields.                                                         |
| `gpu_ids`              | Selected GPU indices or UUIDs for the replica.                                                                                       |
| `tensor_parallel_size` | TP size for the replica.                                                                                                             |
| `gpu_visibility_env`   | Chooses `ROCR_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES`. Preparation joins `gpu_ids` into the exported value.                       |
| `accelerator_devices`  | Host device paths mapped into the container. ROCm requires `/dev/kfd` plus DRI mappings for allocated GPUs.                          |
| `network_mode`         | Uses `host` for the recorded network and port allocation.                                                                            |
| `transfer.transport`   | `ucx_tcp` or `ucx_rdma`.                                                                                                             |
| `transfer.net_devices` | Ethernet interfaces for TCP or HCA:port names for RDMA. `${NARWHAL_FABRIC_INTERFACE}` resolves from the selected engine environment. |
| `transfer.devices`     | Transport device paths mapped into the container. RDMA requires its character devices. TCP uses an empty list.                       |
| `sources`              | Allocation, device, and transfer definitions that produced the record.                                                               |

`prepare` validates every assigned engine record before it creates the output directory. It derives GPU visibility and `UCX_NET_DEVICES` under `environment` and writes `--tensor-parallel-size` under `vllm_args`.

`install` copies the selected launch record into the engine checkout's `config/` directory, and the role environment points to that file.

The delivered launcher records the complete container command, combining the recorded arguments and device mappings with the runtime fields.

Launch records and their supporting extracts use mode `0600` and live in Git-ignored files matching `config/engine-launch.*.json`.

If preparation reports an invalid `.env` input or a missing remote prerequisite, fix it and prepare a new run. The manifest then records the corrected state.

---

## 16. Runtime launch records and image verification

Every generated engine record contains a `runtime` object consumed by `launch_engine.py`.

Discovery reads package pins and the supported runtime-environment fields from the selected image, the model dtype from the model config, and the policy from [environment launch policy](../deploy/01-Discover.md#confirm-the-launch-policy).

Preparation transfers both the launch record and a launcher snapshot to the engine host.

| Runtime field       | Operator input                                                                                                                                                                                                                                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `expected_packages` | Exact installed versions for `vllm` and `nixl` or `nixl-rocm`. Add any image package whose identity `narwhal-engine check` must verify.                                                                                                                                                                 |
| `model_dtype`       | `bfloat16` or `float16`.                                                                                                                                                                                                                                                                                |
| `kv_cache_dtype`    | `auto` or the requested cache dtype.                                                                                                                                                                                                                                                                    |
| `block_size`        | Requested runtime block size. Cache planning records adjusted token-block size and padded page bytes for fabric sizing.                                                                                                                                                                                 |
| `environment`       | Image-local ROCm or CUDA, UCX, NIXL, and library-path settings.                                                                                                                                                                                                                                         |
| `extra_args`        | Model-specific vLLM arguments such as context and batching limits, memory utilization, the reasoning parser, and the attention backend. Others cover remote model code, language-only loading, eager execution, async scheduling, and hybrid-cache policy. For the cache opt-outs, see [Prefix caching and cache events](#161-prefix-caching-and-cache-events). |

The launcher takes the model mount, served model name, bind family, and HTTP port from the role environment, applies the recorded TP size, and configures `NixlConnector` with `kv_both`, UCX, and failure propagation. It supplies the GPU visibility, advertised addresses and ports, transport selection, and engine authentication itself.

The launcher rejects `extra_args` that override launcher-managed settings.

Discovery adds `--trust-remote-code` when the checkpoint model or tokenizer metadata contains an `auto_map`. When the model metadata identifies convolutional state-space model (SSM) transfer state, discovery sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`. Both derived settings take precedence over any values supplied by `NARWHAL_ENGINE_ARGS` or `NARWHAL_ENGINE_ENV`.

Before model startup, `narwhal-engine check`:

- verifies `--trust-remote-code` against the mounted checkpoint
- validates the image identity
- checks the exact distribution versions
- validates the connector configuration and import
- constructs the checkpoint tokenizer
- checks the pinned image's convolutional-state layout for SSM models
- resolves the serving arguments into vLLM's engine configuration

The check records `vllm.version.__version__` as `vllm_api_version` in `checked.json`, tied to the launch-plan hash and image ID.

It also records the cache settings that vLLM resolves for the model, as described in [prefix caching and cache events](#161-prefix-caching-and-cache-events):

| Field            | Value                                                                                        |
| ---------------- | -------------------------------------------------------------------------------------------- |
| `prefix_caching` | `true` when the resolved engine configuration keeps prefix caching on                        |
| `kv_events`      | The resolved event and replay endpoints, or `null` when the engine publishes no cache events |

The check fails when the resolved endpoints differ from `launch.json`. If vLLM resolves prefix caching off for the model, the engine caches no prefix blocks and publishes no block events. For some attention configurations vLLM turns prefix caching off later, during model load.

The [live HTTP process check](../deploy/03-Validate-Engines.md#prove-the-live-http-process) compares the listening engine's `/version` response with that captured value.

The image's NIXL connector must implement the fleet's required `kv_both` behavior, because runtime transfer probes exercise both producer and consumer operations.

Launch directories, environment files, and runtime captures live under the ignored `runs/`. Record the application revision, launcher digest, and container ID with each deployment.

### 16.1 Prefix caching and cache events

The launcher leaves vLLM's prefix-caching default in place. To turn prefix caching off, add vLLM's `--no-enable-prefix-caching` to `extra_args`.

While prefix caching stays on, the launcher configures vLLM to publish KV cache events over two ZeroMQ IPC sockets in `/tmp/narwhal-<uid>/<plan name>/`. The short path keeps each socket within the 107-byte Unix socket path limit, which a launch directory can exceed:

| Socket        | Use                                                       |
| ------------- | --------------------------------------------------------- |
| `events.sock` | Published event batches, each with a sequence number      |
| `replay.sock` | Replay requests for batches still in vLLM's replay buffer |

Preparation creates both directories with mode `0700` for the launching user. A host restart clears `/tmp`, so the check and each engine start recreate them. They stop if either directory belongs to another user or grants group or other access. Containers bind-mount the plan directory at `/narwhal-kv-events`. The `kv_events` object in `launch.json` records the host directory and the endpoints vLLM binds. Stopping a native engine removes its directory. For a container engine, remove the directory after removing the container.

To keep prefix caching on without publishing events, add vLLM's own event setting to `extra_args`:

```json
["--kv-events-config", "{\"enable_kv_cache_events\": false}"]
```

The launcher rejects any other `--kv-events-config` value because it selects the event endpoints. Either opt-out leaves `kv_events` as `null`.
