# b200-kimi-k3

*Paths below are preset-relative (`scripts/...` means this preset's `scripts/`).*

Kimi-K3 on NVIDIA B200, the CUDA sibling of the reference preset. The router needs nothing new, and the config still has no hardware field. Everything below is the preset's own record.

| field | value |
| --- | --- |
| hardware | 2 × NVIDIA B200 nodes, 8 GPUs each, 178.35 GiB HBM per GPU |
| model | moonshotai/Kimi-K3 (hybrid mamba/MLA MoE), 1.42 TiB of weights on disk |
| engine | `vllm/vllm-openai:kimi-k3` @ `sha256:e90e2603b2781936651ba019804137714367c69e10a7b25a2e57b46995225616`, vLLM `0.1.dev19262+gb6bbf29dd.d20260727`, NIXL 1.3.2 (`nixl_cu13`) |
| parallelism | TP8 + expert parallel per node, one engine per node |
| context | 1,048,576 tokens by the model; the offloaded shape declares 262,144 (eager mode leaves the KV cache too little room for 1M) |
| connector | nixl (`kv_role: kv_both`), UCX over RDMA on eight 400G HCAs |
| dialect | vllm |
| weight offload | **required** - see "Why K3 needs offload" |
| measured SLOs | TTFT <= 3.0 s, TPOT <= 2.1 s, calibrated 2026-08-15 at twice the light-load p99 on the offloaded shape. The TPOT floor (1.050 s and 0.994 s per engine) is PCIe, not silicon - read "What offload costs" before quoting these as B200 serving numbers |
| launch | `scripts/engine_serve.sh` inside the node container (`scripts/container_recreate.sh`) |

## Why K3 needs offload

Kimi-K3's weights are 1,560,998,998,830 bytes, or 1.42 TiB. One node's eight B200s hold 8 × 178.35 GiB = 1.393 TiB. The weights alone exceed a node's entire HBM by about 2%, before any KV cache, activation, or CUDA context. A plain TP8 launch dies in `mxfp4.create_weights` while building the MoE experts, at ~175 GiB of 178 GiB on all eight GPUs.

The constraint is the B200's HBM capacity rather than anything about K3. The reference MI355X fleet carries 288 GB per GPU, 2.3 TB per node, and holds the model resident with room to spare. So the CUDA sibling of that preset cannot be shape-identical to it on this hardware.

The vendor deployment already on these nodes solved this by spanning both nodes with `--data-parallel-size 2`, giving one engine on 16 GPUs. Narwhal cannot use that shape: it routes *between* engines, and `narwhal-check`'s produce/consume is a KV transfer from one engine to another. One engine has no peer. Two K3 engines need 2.84 TiB of weights against the fleet's 2.786 TiB of total HBM, so two resident copies do not fit either.

What does fit: one engine per node with part of each layer's experts spilled to host RAM, which each node has 2,826 GB of. `OFFLOAD=on` in `scripts/engine_serve.sh` turns this on.

### What offload costs

The offloader (`vllm/model_executor/offloader/prefetch.py`) is layer-granular. An offloaded layer moves **all 896 of its experts** on every forward pass, not the handful a token routes to. Decode pays the full host-to-device transfer per step.

Measured on this fleet, 2026-08-15:

| quantity | value |
| --- | --- |
| H2D bandwidth, pinned | 57.6 GB/s per GPU (PCIe Gen5 x16) |
| experts per GPU | ~179 GiB across 92 MoE layers, ~1.95 GiB per layer |
| at 1-in-4 offload | ~45 GiB moved per forward pass → **~0.83 s per token** |
| at 1-in-8 offload | ~22 GiB moved per forward pass → **~0.42 s per token** |

Offload also rules out the vendor's loader. `fastsafetensors` DMAs a whole shard file into GPU memory and slices it there, which needs a contiguous staging buffer the size of the largest shard - 15.82 GiB for this checkpoint. An offloaded engine finishes construction with about 9 GiB free, so the load dies in `fastsafetensors/copier/nogds.py` before it reaches the first expert. `engine_serve.sh` defaults `--load-format` to `auto` whenever `OFFLOAD=on`, which stages tensor by tensor. It is slower to boot and costs nothing at serve time.

Against the reference fleet's 60 ms TPOT, that is 7-14× over. Prefetch overlaps some of it, but only three resident layers of compute sit between offloaded ones - a few milliseconds against ~35 ms of transfer each - so most of the transfer stays on the critical path. Offload is what makes a two-engine K3 B200 fleet exist at all; it is not a configuration whose latency describes the KV fabric. **Do not calibrate SLOs from an offloaded engine and present them as B200 serving numbers.** Lower offload trades KV cache for speed: `OFFLOAD_GROUP_SIZE` sets the denominator.

## Environment the scripts depend on

- **No** `VLLM_ROCM_USE_AITER` - AITER is the ROCm backend path.
- `VLLM_SSM_CONV_STATE_LAYOUT=DS` - **verified on CUDA** 2026-08-15: `scripts/engine_serve.sh` exports it unconditionally, so the offloaded boot and the two-direction produce/consume that day exercised it, and passed. DS feeds NIXL's 3-read mamba-conv transfer. The requirement comes from the K3 hybrid rather than from a vendor, and it drops only for MLA-only models like K2. If the CUDA build disagrees, `produce` and `consume` fail, and this is the first knob to question.
- `UCX_NET_DEVICES` - derived at launch, and not by the routing-table lookup the reference preset uses. These nodes run no IPoIB, so the eight 400G HCAs carry no IP and a route lookup returns `enp1s0` instead. The script pins the HCAs found ACTIVE at 400G and lets UCX address them by LID over RDMA. Only the ZMQ side channel needs an IP.
- `UCX_MODULE_DIR` - **required here**, unlike on ROCm. See below.
- The NCCL and vLLM stability knobs carried from the vendor deployment (`NCCL_NVLS_ENABLE=0`, `NCCL_MNNVL_ENABLE=0`, symmetric-memory allreduce off, K3 MoE tail fusion on). NCCL's IB knobs are deliberately absent: TP8 is one node, so NCCL stays on NVLink, and cross-node KV is NIXL's job.

## Constraints of this image

1. **Container entrypoint.** This image is `ENTRYPOINT ["vllm","serve"]`, so the ROCm sibling's `-c "sleep infinity"` reaches vLLM's parser as `--compilation-config` and the container exits 2 before holding a GPU. `container_recreate.sh` passes `--entrypoint bash`.
2. **`UCX_MODULE_DIR` is mandatory.** The nixl wheel ships UCX under an auditwheel-mangled `nixl_cu13.libs/ucx` that UCX's loader does not search. Unset, UCX loads the IB transports but silently drops `libuct_cuda`: it says *"UCX CUDA support was not found! GPU memory is not supported"* once, at warn level, then proceeds. The handshake succeeds, produce and consume pass, and every KV transfer is staged through host memory. `engine_serve.sh` derives the path from whichever nixl variant imports and refuses to serve without it.
3. **The fabric is shared and reachable.** `ibnetdiscover` from node 1 resolves node 2's port GUIDs on the rack's shared IB fabric. Both nodes carry eight ACTIVE 400G HCAs (`mlx5_0-5,10,11`) plus 100G management HCAs that the launch script filters out. Re-check this on any fleet provisioned later; it is a property of the rack, not of the image.
4. **The engine recipe.** Imported in full from the vendor deployment's argv, reproduced in `engine_serve.sh`, so the preset stands alone.
5. **Offload requires the V1 model runner, and the flags lie on V2.** This build defaults Kimi-K3 to the V2 runner (`vllm/v1/worker/gpu/model_runner.py`), which contains no reference to the offloader at all. `set_offloader` is called only from the legacy `vllm/v1/worker/gpu_model_runner.py:939`. On V2 the `--offload-*` flags parse, populate `VllmConfig.offload_config`, appear in the engine's "non-default args" banner, and are then read by nobody: `get_offloader()` returns the default `NoopOffloader`, whose `wrap_modules` is a bare `return list(modules_generator)`. The engine OOMs exactly as if offload had never been requested - verified 2026-08-15, identical failure to the same byte counts with and without the flags. `engine_serve.sh` sets `VLLM_USE_V2_MODEL_RUNNER=0` whenever `OFFLOAD=on`. That is not the runner the vendor validated, and it auto-enables `VLLM_USE_BREAKABLE_CUDAGRAPH`, which disables the torch.compile pipeline. Both are further reasons not to read serving latency off this shape.

## Open items

1. **Prefix caching on CUDA:** the reference build asserts `num_new_tokens > 0` on a full-cache-hit prompt; benchmark filler is exactly that. `tools/engine_image_probe.py` reads the cache-hit caps out of this image without a GPU; run `PREFIX_CACHING=off` campaigns until verified.
2. **Speculative decoding** stays a standalone engine (`scripts/engine_serve_dspark.sh`, unvalidated) until a build passes `scripts/engine_spec_nixl_check.sh`: DS layout + spec decode + NixlConnector in one process.
3. **A shape whose latency means something.** Offload makes the two-engine K3 fleet exist but not represent B200 serving. Either four nodes (two engines of 16 GPUs each, fully resident) or a model that fits eight GPUs. `deepseek-ai/DeepSeek-V4-Flash-DSpark` (156G) and `Qwen/Qwen3.6-27B` (52G) are already cached on both nodes.

## Validation record

| step | status |
| --- | --- |
| container holds GPUs | **pass** - `--entrypoint bash` fix, 2026-08-15 |
| NIXL agent + IB transport | **pass** - `UCX_TLS=rc` initializes on all 8 HCAs |
| UCX CUDA support | **pass** - with `UCX_MODULE_DIR` derived; fails silently without |
| K3 boots TP8, no offload | **fail** - OOM in `mxfp4.create_weights`, does not fit |
| K3 boots TP8 + 1-in-4 offload | **pass** - streaming loader, eager mode, 262,144 context, 2026-08-15 |
| `narwhal-profile` | not run |
| `narwhal-check` (all gates) | **pass** - including `produce` and `consume` in both directions, 2026-08-15 |
| calibrated SLOs | **pass, offloaded shape** - TPOT floor 1.050 s / 0.994 s, targets set at 2×; not B200 serving numbers |
| one request end to end | **pass** - prefill on one node, decode on the other, KV across the fabric |
| `narwhal-bench`, `narwhal-report` | not run |

When the fleet runs a resident shape, re-measure, replace the SLOs here and in `fleet.json`, and say which shape the new numbers describe.
