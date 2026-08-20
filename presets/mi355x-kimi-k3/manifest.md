# mi355x-kimi-k3

The validated pair is Kimi-K3 on AMD Instinct MI355X, verified at L4 on the [Supported Hardware and Models](../../docs/Supported-Hardware-and-Models.md) ladder - gates, profiling, sustained serving, and a full comparative evaluation, all in this configuration.

| field | value |
| --- | --- |
| hardware | AMD Instinct MI355X, eight per node, one vLLM engine per node |
| model | `moonshotai/Kimi-K3` (hybrid mamba/MLA MoE, MXFP4) |
| engine | vLLM, `rocm/vllm-dev@sha256:5aa7e626ff73672f5ca7aae46754570488c23d33ca1ac90756a1d2d1a3fe099b` |
| parallelism | TP8 per node |
| context | 1,048,576 tokens (`--max-model-len 1048576`) |
| connector | nixl (`kv_role: kv_both`) |
| dialect | vllm |
| measured SLOs | TTFT <= 3.0 s, TPOT <= 60 ms, calibrated at twice the light-load p99 |

## Bring-up

1. Recreate the engine container on each node: `scripts/container_recreate.sh` with `engine.env` filled in (image, devices, binds - copy `engine.env.example`).
2. Start each engine inside its container: `NIXL_HOST_IP=<node fabric address> bash scripts/engine_serve.sh 8002`.
3. Fill the placeholder addresses in `fleet.json`, then profile, gate, and serve per [Deploy](../../docs/Deploy.md): `narwhal-profile`, `narwhal-check --preset mi355x-kimi-k3`, `narwhal-serve`.

## Environment requirements

- `VLLM_ROCM_USE_AITER=1` - the MXFP4 MoE path on ROCm.
- `VLLM_SSM_CONV_STATE_LAYOUT=DS` - required by NIXL's mamba transfer for this hybrid model; a CUDA build of the pair carries it too.

## Known caveats on the validated image

Both upstream and version-bound, covered in [Deploy](../../docs/Deploy.md)'s compatibility notes: engines restart in whole waves, and prefix caching ships on (the full-cache-hit assert fires on fully cache-resident prompts on affected builds; set `PREFIX_CACHING=off` for benchmark campaigns there). Speculative decoding is not enabled pending an upstream Triton/AITER pairing; the pair is fully functional without it.

## Speculative decoding: upstream, no date

Speculative decoding under the DS conv layout does not run on any published image today. It stays an option for a standalone engine outside the fleet, and the serving fleet runs without it.

Candidate nightlies from `0814_b160` onward drop `assert speculative_config is None` and route the mamba align pre-copy through the fused kernel instead, so the pair is accepted at init and both models load. On those images the failing component is the attention kernel underneath.

Three images, three different walls:

| image | gfx950 | Triton | AITER MLA kernel where vLLM imports it | DS + spec at init |
| --- | --- | --- | --- | --- |
| deployed `rollback-20260727` | yes | 3.7.0 | yes | asserts, refuses |
| `nightly_cdna4 ..0815_b162` | yes | 3.6.0 | no, moved | clear |
| `nightly_455_wip ..0815_b163` | no | 3.8.0 | no, moved | clear |

The candidate nightly fails twice over. AITER moved the small-head Gluon MLA kernel to `aiter/ops/triton/_gluon_kernels/gfx950/attention/mla.py`, and vLLM still imports `aiter.ops.triton.gluon.mla_gluon`, so warm-up raises once speculative decoding turns on the multi-query path. Shim that and the next wall is the real one: those kernels are written against Triton 3.7's gluon API (`PaddedSharedLayout` takes `cga_layout`, and `buffer_load_to_shared` accepts more layouts) while the image ships Triton 3.6.0. Past renaming the keyword it becomes kernel surgery, and no result from a hand-edited kernel is worth trusting.

Neither bypass works. `VLLM_ROCM_USE_AITER_MLA=0` avoids the gluon kernels and faults the GPU instead (`HSA_STATUS_ERROR_EXCEPTION` on all eight queues). Transplanting Triton 3.7.0 into the nightly loads but registers no backends. Raising heads per rank above the small-head threshold would need a lower TP, which the model does not fit into.

What unblocks it is one image carrying three things that already exist separately: gfx950 code objects, a Triton at 3.7 or newer with AITER built against it, and the fused align routing. Nothing new is needed upstream, only pieces of the same age in one build. No date; this manifest records each candidate as it is probed.

`python3 tools/engine_image_probe.py <image> --require-arch gfx950 --require-spec-decode` settles the gfx950 code objects and the fused align routing and prints the Triton version, all without an accelerator; the Triton/AITER pairing stays open off-silicon. Run it before staging any bump, because the boot check needs the node's GPUs.
