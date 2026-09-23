# Engine endpoints and launch records

## 14. Engine endpoints generated from node environments

An engine URL points to the running vLLM HTTP service, for example:

```text
http://10.0.0.11:8000
```

An engine URL may use an IP address directly when the router can reach that address and port.

The attestation sidecar has a separate URL, for example:

```text
http://10.0.0.11:8010/v1/attestation
```

Use the ports actually configured by engine deployment.

For generated deployment inputs, provide:

```text
NARWHAL_FABRIC_INTERFACE
NARWHAL_ENGINE_PORT
NARWHAL_ATTEST_PORT
```

in `.env`.

Discovery reads the chosen interface on each engine host and writes its unique global address as:

```text
NARWHAL_NODE_<n>_IP
```

into `config/deployment.env`.

It derives engine and attestation URLs from that address and uses IPv6 host brackets where required.

Set `NARWHAL_NODE_<n>_IP` explicitly when the interface has multiple global addresses.

Set a full engine or attestation URL when that service must use another reachable address.

Changing a port also requires the matching per-node port override.

The generated fleet uses those derived values:

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

Discovery adds one fleet record per engine.

`.env.example` shows the two-engine minimum and the shared fabric interface.

Store site-specific fleet files under either ignored path:

```text
config/fleet.json
config/fleet.*.json
```

After loading `.env`, invoke profiling, preflight, and serving with:

```bash
--fleet "$NARWHAL_FLEET"
```

Observability resolves endpoint URLs using the same complete-value substitution rules.

---

## 15. Engine launch records

`NARWHAL_LAUNCH_CONFIG` selects the private generated management-workstation file:

```text
config/engine-launch.local.json
```

Discovery creates one launch record per assigned `engine-<n>` role from:

- detected GPUs
- device paths
- image package versions
- environment-selected launch policy

Each record identifies its retained inspection and policy inputs under `sources`; `config/engine-launch.sources.json` indexes those references.

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

`prepare` validates every assigned engine record before it creates the output directory.

It derives:

- GPU visibility
- `UCX_NET_DEVICES`

under `environment`, and writes:

```text
--tensor-parallel-size
```

under `vllm_args`.

`install` copies the selected launch record into the engine checkout's `config/` directory. The role environment points to that file.

The delivered launcher combines these recorded arguments and device mappings with runtime fields, then records the complete container command.

Launch records and their supporting extracts remain private in ignored mode-0600:

```text
config/engine-launch.*.json
```

Keep real allocation and runtime evidence in private files.

When preparation reports an invalid `.env` input or a missing remote prerequisite, correct the named input, regenerate the affected configuration, and prepare a new run so the manifest records the corrected state.

---

## 16. Runtime launch records and image verification

Every generated engine record contains a `runtime` object consumed by `launch_engine.py`.

Discovery reads:

- package pins from the selected image
- supported runtime-environment fields from that image
- model dtype from model config
- policy from [environment launch policy](../deploy/01-Discover.md#confirm-launch-policy)

Preparation transfers both the launch record and a launcher snapshot to the engine host.

| Runtime field       | Operator input                                                                                                                                                                                                           |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `expected_packages` | Exact installed versions for `vllm` and `nixl` or `nixl-rocm`; add any image packages whose identity must be checked.                                                                                                    |
| `model_dtype`       | `bfloat16` or `float16`.                                                                                                                                                                                                 |
| `kv_cache_dtype`    | `auto` or the requested cache dtype.                                                                                                                                                                                     |
| `block_size`        | Requested runtime block size. Cache planning records adjusted token-block size and padded page bytes for fabric sizing.                                                                                                  |
| `environment`       | Image-local ROCm/CUDA, UCX, NIXL, and library-path settings.                                                                                                                                                             |
| `extra_args`        | Model-specific vLLM arguments for context/batching limits, memory utilisation, reasoning parser, attention backend, remote model code, language-only loading, eager execution, async scheduling, or hybrid-cache policy. |

The launcher takes these values from the role environment:

- model mount
- served model name
- bind family
- HTTP port

It applies the recorded TP size and configures `NixlConnector` with:

- `kv_both`
- UCX
- failure propagation

The launcher itself supplies:

- GPU visibility
- advertised addresses and ports
- transport selection
- engine authentication

Discovery adds `--trust-remote-code` when the checkpoint model or tokenizer metadata contains an `auto_map`.

When model metadata identifies convolutional SSM transfer state, discovery sets:

```text
VLLM_SSM_CONV_STATE_LAYOUT=DS
```

The launcher retains these derived settings when `NARWHAL_ENGINE_ARGS` or `NARWHAL_ENGINE_ENV` supplies additional values.

The launcher rejects `extra_args` that override launcher-managed settings.

Before model startup, the image check:

1. verifies `--trust-remote-code` against the mounted checkpoint,
2. validates image identity,
3. checks exact distribution versions,
4. validates connector configuration and import,
5. constructs the checkpoint tokenizer,
6. checks the pinned image's convolutional-state layout for SSM models.

The check records:

```text
vllm.version.__version__
```

as `vllm_api_version` in `checked.json`, tied to the launch-plan hash and image ID.

The HTTP probe compares `/version` with that captured value.

Use an image whose NIXL connector implements the fleet's required `kv_both` behaviour. Runtime transfer probes exercise both producer and consumer operations.

Keep launch directories, environment files, and runtime captures under ignored `runs/`.

Record the application revision, launcher digest, and container ID with the deployment.
