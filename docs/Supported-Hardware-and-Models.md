# Supported Hardware and Models

The table lists what has been run, not what can run.

| Hardware | Model | Verification | Preset |
| --- | --- | --- | --- |
| AMD Instinct MI355X (TP8, one engine per node) | `moonshotai/Kimi-K3` | L4 - Evaluated | [`presets/mi355x-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/mi355x-kimi-k3) |
| NVIDIA B200 (TP8 + EP, one engine per node, experts offloaded) | `moonshotai/Kimi-K3` | L1 - Gated | [`presets/b200-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/b200-kimi-k3) |

Verification levels, each including the ones below it:

- **L4 - Evaluated**: the study's full comparison methodology has run
  on the pair - architectures and controllers raced on identical
  traces, journal-scored.
- **L3 - Served**: sustained serving with the full operator surface -
  admission, health, failover, observability.
- **L2 - Profiled**: per-engine curves fitted and SLOs calibrated
  from the pair's own profile.
- **L1 - Gated**: all eight preflight gates pass, including `produce`
  and `consume` moving real KV between every ordered engine pair.
- **L0 - Runway**: the engine contract holds on paper; nothing has
  run.

The contract is the real boundary: stateless vLLM engines with NIXL
`kv_both`, on a fabric the KV cache can cross - any accelerator and
model such a build serves. The `produce` and `consume` gates, not this
table, are the arbiter for your exact stack. A pair earns a row at L1;
runway rows appear only when the hardware is already planned.

## The MI355X / Kimi-K3 pair

The validated recipe; the full setup ships as
[`presets/mi355x-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/mi355x-kimi-k3)
(fleet config with measured SLOs, engine env, and launch scripts):

| | |
| --- | --- |
| Engine build | vLLM, `rocm/vllm-dev@sha256:5aa7e626ff73672f5ca7aae46754570488c23d33ca1ac90756a1d2d1a3fe099b` |
| Parallelism | TP8, one engine per node |
| Context | 1,048,576 tokens (`--max-model-len 1048576`) |
| Connector | NIXL (`kv_role: kv_both`) |
| Measured SLOs | TTFT <= 3.0 s, TPOT <= 60 ms, calibrated at twice the light-load p99 |

Two environment requirements carry the pair: `VLLM_ROCM_USE_AITER=1`
(the MXFP4 MoE path on ROCm) and `VLLM_SSM_CONV_STATE_LAYOUT=DS`
(required by NIXL's mamba-conv transfer - a K3-hybrid requirement, so
a CUDA build of this pair carries it too).

Two engine-version caveats apply, both upstream and both covered in
[Deploy](Deploy.md)'s compatibility notes: engines restart in whole
waves, and prefix caching stays off on this build (the full-cache-hit
assert). Speculative decoding is not enabled pending an upstream
Triton/AITER pairing; the pair is fully functional without it, and
every verification level above was earned in this configuration.

## The B200 / Kimi-K3 pair

The CUDA sibling, gated but not yet profiled; the setup ships as
[`presets/b200-kimi-k3`](https://github.com/athrael-soju/Narwhal/tree/main/presets/b200-kimi-k3)
(fleet config, launch scripts, and a manifest that records the whole
bring-up):

| | |
| --- | --- |
| Engine build | vLLM, `vllm/vllm-openai:kimi-k3` @ `sha256:e90e2603b2781936651ba019804137714367c69e10a7b25a2e57b46995225616` |
| Parallelism | TP8 + expert parallel, one engine per node |
| Context | 262,144 tokens on the offloaded shape (the model's 1,048,576 needs residency) |
| Connector | NIXL (`kv_role: kv_both`), UCX over RDMA on eight 400G HCAs |
| Measured SLOs | TTFT <= 3.0 s, TPOT <= 2.1 s, calibrated on the offloaded shape |

Kimi-K3's 1.42 TiB of weights exceed a B200 node's 1.393 TiB of HBM,
so the pair runs with a quarter of each layer's experts in host RAM,
and decode pays that transfer on every step. All eight gates pass,
including `produce` and `consume` moving real KV across the fabric,
and the SLOs above describe the offloaded shape's PCIe floor rather
than B200 serving. The preset's manifest records why the shape exists,
what it costs, and what a resident one needs.

To bring up a pair the table does not list,
[presets/README](https://github.com/athrael-soju/Narwhal/blob/main/presets/README.md)
is the adoption path: copy `presets/_template/`, adapt the launch
scripts, profile, gate, calibrate, serve. Validated preset
configurations ship under `presets/`, and each table row links its
configuration.
