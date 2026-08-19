# Supported Hardware and Models

The table lists hardware and model pairs that have been run and verified on this project. A pair absent from it is untested.

| Hardware                                                       | Model                | Verification  | Preset                                                                                               |
| -------------------------------------------------------------- | -------------------- | ------------- | ---------------------------------------------------------------------------------------------------- |
| AMD Instinct MI355X (TP8, one engine per node)                 | `moonshotai/Kimi-K3` | L4: Evaluated | [`presets/mi355x-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/mi355x-kimi-k3) |
| NVIDIA B200 (TP8 + EP, one engine per node, experts offloaded) | `moonshotai/Kimi-K3` | L1: Gated     | [`presets/b200-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/b200-kimi-k3)     |

Verification levels, from strongest to weakest. Each level includes everything the lower ones require.

- **L4: Evaluated.** A comparative evaluation has run on the pair: architectures and controllers raced on identical traces, and scored deterministically.
- **L3: Served.** Sustained serving with the full operator surface (admission, health, failover, observability).
- **L2: Profiled.** Per-engine curves fitted and SLOs calibrated from the pair's own measurements.
- **L1: Gated.** All eight preflight gates pass, with `produce` and `consume` moving real KV between every ordered engine pair.
- **L0: Runway.** The engine contract is satisfied on paper only, and nothing has run on the pair.

Support is bounded by the engine contract: stateless vLLM engines with NIXL `kv_both`, on a fabric the KV cache can cross. Any accelerator and model such a build can serve is a candidate. For a stack the table does not list, the `produce` and `consume` gates are the decision procedure. Rows appear at L1 and above. An L0 row is added only when the hardware is already planned.

## The MI355X / Kimi-K3 pair

The validated deployment. Its fleet configuration with measured SLOs, engine environment, and launch scripts is at [`presets/mi355x-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/mi355x-kimi-k3):

|               |                                                                                               |
| ------------- | --------------------------------------------------------------------------------------------- |
| Engine build  | vLLM, `rocm/vllm-dev@sha256:5aa7e626ff73672f5ca7aae46754570488c23d33ca1ac90756a1d2d1a3fe099b` |
| Parallelism   | TP8, one engine per node                                                                      |
| Context       | 1,048,576 tokens (`--max-model-len 1048576`)                                                  |
| Connector     | NIXL (`kv_role: kv_both`)                                                                     |
| Measured SLOs | TTFT <= 3.0 s, TPOT <= 60 ms, calibrated at twice the light-load p99                          |

Two environment variables are required: `VLLM_ROCM_USE_AITER=1` (the MXFP4 MoE path on ROCm) and `VLLM_SSM_CONV_STATE_LAYOUT=DS` (needed by NIXL's mamba-conv transfer). The second is a K3-hybrid requirement, so a CUDA build of this pair needs it as well.

Two engine-version caveats apply, both upstream and both covered in the compatibility notes in [Deploy](Deploy.md). Engines restart in whole waves, and prefix caching ships on; on affected builds the full-cache-hit assert fires on fully cache-resident prompts, so benchmark campaigns there run with `PREFIX_CACHING=off`. Speculative decoding is disabled pending an upstream Triton/AITER pairing. The pair is fully functional without it, and the L4 grade above was verified in exactly this configuration, run at the evaluation's 15 s TTFT and 60 ms TPOT budgets. The preset ships a tighter 3 s TTFT budget for demonstration; a run against it scores with `--ttft-slo 3.0`.

## The B200 / Kimi-K3 pair

The CUDA counterpart, gated but not yet profiled. Its fleet configuration and launch scripts are at [`presets/b200-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/b200-kimi-k3), along with a manifest that records the full bring-up:

|               |                                                                                                              |
| ------------- | ------------------------------------------------------------------------------------------------------------ |
| Engine build  | vLLM, `vllm/vllm-openai:kimi-k3` @ `sha256:e90e2603b2781936651ba019804137714367c69e10a7b25a2e57b46995225616` |
| Parallelism   | TP8 + expert parallel, one engine per node                                                                   |
| Context       | 262,144 tokens on the offloaded shape (the model's 1,048,576 needs residency)                                |
| Connector     | NIXL (`kv_role: kv_both`), UCX over RDMA on eight 400G HCAs                                                  |
| Measured SLOs | TTFT <= 3.0 s, TPOT <= 2.1 s, calibrated on the offloaded shape                                              |

Kimi-K3's 1.42 TiB of weights do not fit in a B200 node's 1.393 TiB of HBM, so the pair runs with one layer in four keeping all 896 of its experts in host RAM and decode transfers them over PCIe on every step. All eight gates pass, including `produce` and `consume` moving real KV across the fabric. The SLOs above measure this offloaded shape's PCIe floor, so they understate what a resident B200 deployment can do. The preset's manifest explains why the offloaded shape exists, what the offload costs on every decode step, and what a resident shape would need.

For hardware the table does not list, start from [presets/README](https://github.com/athrael-soju/Narwhal/blob/main/presets/README.md): copy `presets/_template/` and adapt its launch scripts, then work through the profiling, gating, and calibration steps before serving. Each table row links the specific preset configuration used in its verification.
