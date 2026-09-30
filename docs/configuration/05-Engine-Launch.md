# Engine endpoints and launch records

## 14. Engine endpoints generated from node environments

An engine URL is the base URL of the running vLLM HTTP service, such as `http://10.0.0.11:8000`. A plain IP address works as long as the router can reach that address and port. Use whatever ports the engine deployment actually configures. The attestation sidecar has its own URL, such as `http://10.0.0.11:8010/v1/attestation`.

For generated deployment inputs, set `NARWHAL_FABRIC_INTERFACE`, `NARWHAL_ENGINE_PORT`, and `NARWHAL_ATTEST_PORT` in `.env`. Discovery reads the chosen interface on each engine host, writes its unique global address to `config/deployment.env` as `NARWHAL_NODE_<n>_IP`, and builds the engine and attestation URLs from that address, adding IPv6 brackets where needed.

Set values manually in these cases:

- The interface has more than one global address: set `NARWHAL_NODE_<n>_IP`.
- A service must use a different reachable address: set its full URL.
- An engine deployment uses non-default ports: set the matching per-node port override.

Discovery adds a fleet record like this for each engine (shown for n1):

```json
{
  "iid": "n1",
  "url": "${NARWHAL_NODE_1_URL}",
  "attestation_url": "${NARWHAL_NODE_1_ATTESTATION_URL}",
  "role": "prefill"
}
```

`.env.example` shows the two-engine minimum with a shared fabric interface. Keep site-specific fleet files at `config/fleet.json` or `config/fleet.*.json`, both of which Git ignores. After loading `.env` and `config/deployment.env`, pass `--fleet "$NARWHAL_FLEET"` to profiling, preflight, and serving.

Observability resolves endpoint URLs with the same whole-value substitution rules as the fleet loader. See [§1.3](01-Fleet-Schema.md#13-environment-loading).

---

## 15. Engine launch records

`NARWHAL_LAUNCH_CONFIG` points at `config/engine-launch.local.json`, a private file generated on the management workstation. Discovery writes one launch record for each assigned `engine-<n>` role, based on the detected GPUs, device paths, image package versions, and the launch policy selected in the environment. Each record lists the inspection and policy inputs it came from under `sources`, and `config/engine-launch.sources.json` indexes those references. The [launch-record example](https://github.com/athrael-soju/Narwhal/blob/main/config/engine-launch.example.json) documents the allocation, transport, and runtime fields.

To change GPU allocation or runtime policy, change the corresponding `.env` input and rerun discovery into a fresh output set.

### 15.1 Allocation and transport fields

| Field                  | Deployment meaning                                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `accelerator`          | GPU product name. Compare it with the host inspection and the fleet's hardware fields.                                                   |
| `gpu_ids`              | GPU indices or UUIDs selected for the replica.                                                                                        |
| `tensor_parallel_size` | Tensor-parallel size for the replica.                                                                                                 |
| `gpu_visibility_env`   | Either `ROCR_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES`. `prepare` joins `gpu_ids` into the exported value.                         |
| `accelerator_devices`  | Host device paths mapped into the container. ROCm needs `/dev/kfd` plus DRI mappings for the allocated GPUs.                          |
| `network_mode`         | `host`, matching the recorded network and port allocation.                                                                            |
| `transfer.transport`   | `ucx_tcp` or `ucx_rdma`.                                                                                                              |
| `transfer.net_devices` | Ethernet interfaces for TCP, or HCA:port names for RDMA. `${NARWHAL_FABRIC_INTERFACE}` resolves from the selected engine environment. |
| `transfer.devices`     | Transport device paths mapped into the container. RDMA needs its character devices; TCP uses an empty list.                           |
| `sources`              | The allocation, device, and transfer definitions that produced the record.                                                            |

`prepare` validates every assigned engine record before it creates the output directory. From each record it derives the GPU visibility variable and `UCX_NET_DEVICES` under `environment`, and writes `--tensor-parallel-size` under `vllm_args`. `install` copies the selected record into the engine checkout's `config/` directory, and the role environment points at that copy. At launch time, the delivered launcher combines the recorded arguments and device mappings with the runtime fields and records the complete container command.

Launch records and their supporting extracts are written with mode `0600` to Git-ignored files matching `config/engine-launch.*.json`. Real allocation and runtime data belongs in private files like these.

If `prepare` reports an invalid `.env` input or a missing remote prerequisite, fix the input it names, regenerate the affected configuration, and prepare a new run so the manifest records the corrected state.

---

## 16. Runtime launch records and image verification

Every generated launch record has a `runtime` object, which `launch_engine.py` consumes. Discovery fills it from the package pins and supported runtime-environment fields in the selected image, the model dtype in the model config, and the [environment launch policy](../deploy/01-Discover.md#confirm-launch-policy). `prepare` sends both the launch record and a snapshot of the launcher to the engine host.

| Runtime field       | Operator input                                                                                                                                                                                                                         |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `expected_packages` | Exact installed versions of `vllm` and of `nixl` or `nixl-rocm`, plus any other image packages whose identity must be checked.                                                                                                    |
| `model_dtype`       | `bfloat16` or `float16`.                                                                                                                                                                                                          |
| `kv_cache_dtype`    | `auto`. The launcher accepts no other value.                                                                                                                                                                                      |
| `block_size`        | Requested runtime block size. Cache planning records the adjusted token-block size and padded page bytes for fabric sizing.                                                                                                       |
| `environment`       | Image-local settings with a `VLLM_`, `UCX_`, `NIXL_`, `ROCM_`, `HIP_`, `HSA_`, `AITER_`, `PYTORCH_`, or `SAFETENSORS_` prefix, plus `LD_LIBRARY_PATH` and `PYTHONPATH`.                                                           |
| `extra_args`        | Model-specific vLLM arguments from an allow-list (no other vLLM options are accepted) covering context and batching limits, memory utilization, reasoning parser, attention backend, remote model code, language-only loading, eager execution, async scheduling, or hybrid-cache policy. |

From the role environment, the launcher takes the model mount, served model name, bind family, and HTTP port. It applies the recorded tensor-parallel size and configures `NixlConnector` with `kv_both`, UCX, and failure propagation. The launcher sets GPU visibility, advertised addresses and ports, transport selection, and engine authentication.

Discovery also derives two settings from the model. It adds `--trust-remote-code` when the checkpoint's model or tokenizer metadata contains an `auto_map`, and it sets `VLLM_SSM_CONV_STATE_LAYOUT=DS` when the metadata identifies convolutional SSM transfer state. Discovery retains these derived settings when `NARWHAL_ENGINE_ARGS` replaces the default argument list or `NARWHAL_ENGINE_ENV` adds environment values. It rejects a `NARWHAL_ENGINE_ENV` that sets a different layout.

Before the model starts, the image check runs these steps in order:

1. Verify `--trust-remote-code` against the mounted checkpoint.
2. For convolutional SSM models, confirm that the engine environment sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`.
3. Validate the image identity.
4. Check exact distribution versions.
5. Validate the connector configuration and import.
6. For SSM models, check the pinned image's convolutional-state layout.
7. Construct the checkpoint tokenizer.

The check records `vllm.version.__version__` as `vllm_api_version` in `checked.json`, tied to the launch-plan hash and the image ID. The HTTP probe later compares `/version` with that recorded value.

The image's NIXL connector must support `kv_both`. Runtime transfer probes test both the producer and consumer sides.

Keep launch directories, environment files, and runtime captures under the ignored `runs/` directory. Record the application revision, launcher digest, and container ID with the deployment.
