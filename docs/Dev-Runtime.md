# WSL2 RTX 5090 runtime

On 25 September 2026, two independent vLLM processes loaded Qwen3.5-0.8B
Q4_K_M on one RTX 5090, exported its hybrid cache through NIXL, and consumed
each other's descriptors through Narwhal's preflight. A request through the
router returned `5` for `2 + 3` in 268 ms. The run used the installed native
launch and attestation helpers developed in [PR #57](https://github.com/athrael-soju/Narwhal/pull/57).

## Runtime pins

| Component | Selected version |
| --- | --- |
| GPU | NVIDIA GeForce RTX 5090, 32,607 MiB |
| Windows NVIDIA driver | 616.92 |
| WSL2 kernel | 6.6.87.2-microsoft-standard-WSL2 |
| Python | 3.12.3 |
| PyTorch / CUDA | 2.13.0 / 13.0 |
| vLLM | 0.29.0 |
| NIXL / nixl-cu13 | 1.4.1 / 1.4.1 |
| Transformers | 5.17.0 |
| GGUF plugin | 0.0.5 CUDA wheel, Python sources at `d4c1f0d082fc7cd4350da56689109a01c1f29d6c` |

The model file is `Qwen3.5-0.8B-Q4_K_M.gguf` from
[`unsloth/Qwen3.5-0.8B-GGUF`](https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/tree/e524882462b3f2a9fe83be967c654c4322abb2f6),
revision `e524882462b3f2a9fe83be967c654c4322abb2f6`, SHA-256
`bd258782e35f7f458f8aced1adc053e6e92e89bc735ba3be89d38a06121dc517`.
The tokenizer and config match
[`Qwen/Qwen3.5-0.8B`](https://huggingface.co/Qwen/Qwen3.5-0.8B/tree/2fc06364715b967f1860aea9cf38778875588b17)
revision `2fc06364715b967f1860aea9cf38778875588b17` byte for byte.

The [GGUF plugin source](https://github.com/vllm-project/vllm-gguf-plugin/tree/d4c1f0d082fc7cd4350da56689109a01c1f29d6c)
supplies the Qwen3.5 loader used in this run. Its installed Python tree hashes
to `b08b2ffdf18e4314ed7e4a8cfd49de3520edd5d9bc9be4564d8a71b2265771e0`;
the `_C_gguf.abi3.so` extension hashes to
`64521127698503e4b72053cd06adc9aa0448debb07715b2663b1250a816b2c1e`.
The tree digest concatenates each sorted relative `.py` path, a NUL byte,
its contents, and a NUL byte.

## Engine and transfer settings

Each engine used tensor parallelism 1, BF16 compute, automatic KV dtype,
block size 128, context limit 4,096, four active sequences, eager execution,
and GPU memory utilization 0.1. The loader used `--load-format gguf`,
`--language-model-only`, and the pinned tokenizer directory for both
`--tokenizer` and `--hf-config-path`.

`NixlConnector` ran with `kv_role=kv_both`, pull transfer,
`kv_load_failure_policy=fail`, the UCX backend, and handshake compatibility
enforcement. `VLLM_SSM_CONV_STATE_LAYOUT=DS` selected the convolution-state
layout; `VLLM_USE_V2_MODEL_RUNNER=0` and `VLLM_USE_FLASHINFER_SAMPLER=0`
selected the tested runner and sampler. Prefix caching was disabled.
`UCX_TLS=tcp,sm,self,cuda_copy` and `UCX_NET_DEVICES=eth0` selected the
WSL2 transport. The live cache contained both `MambaSpec` and attention groups.

| Listener | Producer | Consumer |
| --- | ---: | ---: |
| Engine HTTP | 18301 | 18302 |
| Attestation HTTP | 18401 | 18402 |
| NIXL side channel | 5901 | 5902 |

The router used port 18007; UCX used TCP ports 41000 through 41999.
Engine launch plans retained the effective argument arrays, environment,
model hashes, GPU UUID, boot ID, PID and kernel start ticks.

## Pair qualification

Full preflight checked current profiles and process identities, then sent
Narwhal's producer request and passed its returned KV descriptor to the
other process. Each directed transfer incremented the consumer's NIXL
transfer counter and returned three identified output tokens.

| Directed path | NIXL transfers | NIXL transfer time |
| --- | ---: | ---: |
| Engine 1 → engine 2 | 1 | 179.397 ms |
| Engine 2 → engine 1 | 1 | 171.475 ms |

The pair started in 127.9 seconds. Whole-device VRAM was 2,647 MiB before
launch, reached 10,074 MiB during startup sampling at 500 ms intervals,
and measured 10,366 MiB after profiling and inference. Teardown stopped
the recorded router, sidecars and engine process groups and returned
whole-device use to 2,625 MiB.

The per-engine vLLM fraction reserves 3,260.7 MiB for its internal memory
accounting; whole-device measurements include CUDA and process overhead.
Four engines at the observed per-engine increment project roughly 15,438 MiB of
additional use. The four-engine template therefore allows a 0.5 aggregate
device fraction (16,303.5 MiB) and checks another 2,048 MiB of free reserve
before launch. The installed four-engine run in
[#96](https://github.com/athrael-soju/Narwhal/issues/96) measures that projection.

The retained private bundle is `runs/milestone7/pair-evidence.tar.gz`, with
launch logs, package identities, tokenizer hashes, cache layouts,
attestations, profile samples, both directed transfers, router response,
metrics, VRAM samples and teardown. Results cover one execution of the
two-engine topology at the settings above.
