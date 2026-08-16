#!/usr/bin/env bash
# engine_serve.sh — start the node-local Kimi-K3 engine inside the sleep-
# infinity container from container_recreate.sh. CUDA sibling of the
# reference preset's script: no AITER, everything else that is
# a property of the model or the transport rather than the vendor stays.
#
# Provenance: derived 2026-08-14 from presets/mi355x-kimi-k3/scripts/
# engine_serve.sh. The B200 tuning below was read on 2026-08-15 off the
# vendor deployment these nodes already ran (image vllm/vllm-openai:kimi-k3
# @ sha256:e90e2603b278) and is reproduced here in full, so nothing in this
# file depends on a ledger outside the repository.
#
# Carried over unchanged, because each is a B200 performance choice that
# deployment had already tuned: fp8 KV cache, FLASHINFER prefill with query
# quantization, the flashinfer_trtllm MoE path with expert parallelism,
# fastsafetensors loading, and no autotune sweep.
#
# Dropped deliberately:
#   - its data-parallel split across both nodes; narwhal wants one whole
#     engine per node, and the router does the spreading
#   - its DSpark speculative config, incompatible with the DS conv layout
#     NIXL needs (below)
#   - its --max-num-seqs 4, which sized a smoke client; the narwhal engine
#     leaves vLLM's default alone
#
# Usage (run ON the node):
#   NIXL_HOST_IP=<node serving address> bash engine_serve.sh [PORT]
#
# Env knobs:
#   NIXL_HOST_IP        required, the address peers and the router dial
#   PORT (arg $1)       default 8002
#   GPU_MEM_UTIL        default 0.90 (the recipe's value)
#   MODELS              required, node-local weights directory
#   LOG_DIR             default ./runs
set -euo pipefail

PORT="${1:-8002}"
: "${NIXL_HOST_IP:?set the node serving address, see fleet.json}"
: "${MODELS:?set the node-local weights directory, see manifest.md}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
# PREFIX_CACHING=off drops --enable-prefix-caching; the reference fleet's
# deployed build asserts on full-cache-hit prompts, so check the CUDA
# build has the fix before trusting benchmark filler with caching on.
PREFIX_CACHING="${PREFIX_CACHING:-on}"
CACHE_FLAG="--enable-prefix-caching"
[ "$PREFIX_CACHING" = "off" ] && CACHE_FLAG="--no-enable-prefix-caching"
LOG_DIR="${LOG_DIR:-./runs}"
LOG_FILE="$LOG_DIR/engine_${PORT}.prod.log"
# OFFLOAD=on spills a fraction of the MoE expert weights to host RAM, which is
# what lets Kimi-K3 run as one whole engine per node here at all: the weights
# are 1.42 TiB and a node's eight B200s hold 1.393 TiB, so the model overshoots
# HBM before any KV cache exists. See manifest.md, "Why K3 needs offload".
#
# The cost is not subtle. This offloader is layer-granular - an offloaded layer
# moves all 896 of its experts every forward pass, not the handful a token
# actually routes to - so decode pays the full H2D transfer per step. Measured
# on this fleet: 57.6 GB/s per GPU, ~1.95 GiB of experts per layer per GPU.
# At the default 1-in-4 below that is ~45 GiB and ~0.83 s per token. Prefetch
# overlaps some of it, but there are only three resident layers of compute
# between offloaded ones, so most of the transfer stays on the critical path.
# Calibrate SLOs from this only if you mean to describe PCIe.
OFFLOAD="${OFFLOAD:-off}"
# fastsafetensors DMAs a whole shard file into GPU memory and slices it there,
# which costs a contiguous staging buffer the size of the largest shard - 15.82
# GiB for this checkpoint. The vendor could afford that on 16 GPUs. An offloaded
# engine cannot: construction leaves ~9 GiB free and the load dies in
# fastsafetensors/copier/nogds.py before it reaches the first expert. So offload
# implies the streaming loader, which stages tensor by tensor instead.
if [ "$OFFLOAD" = "on" ]; then
  LOAD_FORMAT="${LOAD_FORMAT:-auto}"
else
  LOAD_FORMAT="${LOAD_FORMAT:-fastsafetensors}"
fi
# The prefetch offloader forks a copy stream for async H2D and, on this build,
# leaves work unjoined when CUDA graph capture closes: every worker dies with
# cudaErrorStreamCaptureUnjoined ("capturing stream has unjoined work"), then
# `markCaptureEnd called with no captures in progress` on the way down. It gets
# all the way through weight load and KV allocation first, so this costs a full
# boot to discover. Eager mode skips capture entirely and sidesteps it.
#
# The usual objection to --enforce-eager is latency, and it does not apply here:
# decode on an offloaded engine is PCIe-bound by roughly an order of magnitude
# more than graph capture would ever save. This shape exists to validate the
# transport, not to time it.
if [ "$OFFLOAD" = "on" ]; then
  EAGER="${EAGER:-on}"
else
  EAGER="${EAGER:-off}"
fi
EAGER_ARGS=()
[ "$EAGER" = "on" ] && EAGER_ARGS=(--enforce-eager)
# Context length is the last thing offload takes. Eager mode gives up the CUDA
# graph memory pool, so activations claim more and KV cache lands at ~4.3 GiB
# per GPU; the model's 1,048,576-token max needs 13.63 GiB to admit a single
# request, and vLLM refuses to start. It reports the honest ceiling itself -
# 324,096 tokens at this memory - so the offloaded engine declares 262,144,
# comfortably under it. That is a property of this shape, not of Kimi-K3: the
# resident preset keeps the full 1M.
if [ "$OFFLOAD" = "on" ]; then
  MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}"
else
  MAX_MODEL_LEN="${MAX_MODEL_LEN:-1048576}"
fi
OFFLOAD_GROUP_SIZE="${OFFLOAD_GROUP_SIZE:-4}"
OFFLOAD_NUM_IN_GROUP="${OFFLOAD_NUM_IN_GROUP:-1}"
OFFLOAD_PREFETCH_STEP="${OFFLOAD_PREFETCH_STEP:-2}"
OFFLOAD_PARAMS="${OFFLOAD_PARAMS:-experts}"
OFFLOAD_ARGS=()
if [ "$OFFLOAD" = "on" ]; then
  # Offload only exists on the legacy (V1) model runner. This build defaults
  # Kimi-K3 to the V2 runner (vllm/v1/worker/gpu/model_runner.py), which never
  # calls set_offloader - so on V2 the --offload-* flags parse, populate
  # VllmConfig.offload_config, and are then read by nobody. get_offloader()
  # returns the default NoopOffloader, whose wrap_modules is a bare
  # `return list(modules_generator)`, and the engine OOMs exactly as if no
  # offload had been asked for. Verified 2026-08-15: identical OOM, same byte
  # counts, with and without the flags.
  #
  # V1 accepts this config (checked via create_engine_config) and does install
  # the PrefetchOffloader. It is not the runner the vendor validated, and it
  # auto-enables VLLM_USE_BREAKABLE_CUDAGRAPH, which disables the torch.compile
  # pipeline. Both are reasons to distrust latency from this shape.
  export VLLM_USE_V2_MODEL_RUNNER="${VLLM_USE_V2_MODEL_RUNNER:-0}"
  OFFLOAD_ARGS=(
    --offload-group-size "$OFFLOAD_GROUP_SIZE"
    --offload-num-in-group "$OFFLOAD_NUM_IN_GROUP"
    --offload-prefetch-step "$OFFLOAD_PREFETCH_STEP"
    --offload-params "$OFFLOAD_PARAMS"
  )
fi
# Send this script's own diagnostics to the engine log rather than to stderr.
# Under `docker exec -d` stderr goes nowhere, and the transport pins below are
# exactly what an operator needs to audit after the fact.
say() { echo "engine_serve.sh: $*" | tee -a "$LOG_FILE" >&2; }

# No VLLM_ROCM_USE_AITER: the AITER MXFP4 MoE path is a ROCm backend choice.
# DS layout is kept pending verification: VLLM_SSM_CONV_STATE_LAYOUT=DS
# is required by NIXL's 3-read mamba-conv transfer, which is a K3 hybrid-
# architecture requirement rather than a ROCm one - it drops only for
# MLA-only models like K2. NOTE: DS is incompatible with speculative decoding
# in the reference build (vllm/v1/worker/gpu/model_states/mamba_hybrid.py:141
# asserts speculative_config is None under DS), so no --speculative-config
# here; engine_spec_nixl_check.sh is the boot-check for a build that lifts it.
export VLLM_SSM_CONV_STATE_LAYOUT=DS
# Read off the vendor deployment named in the header. These are properties of
# this silicon and this model, not of that experiment, so they carry over: NVLS
# and multi-node NVLink stay off, symmetric-memory allreduce stays off to match
# --disable-custom-all-reduce below, and the K3 MoE tail fusion stays on.
# NCCL's own IB knobs are deliberately absent - TP8 is one node, so NCCL never
# leaves NVLink here, and cross-node KV is NIXL's job (UCX_NET_DEVICES below).
export NCCL_CUMEM_ENABLE="${NCCL_CUMEM_ENABLE:-1}"
export NCCL_NVLS_ENABLE="${NCCL_NVLS_ENABLE:-0}"
export NCCL_MNNVL_ENABLE="${NCCL_MNNVL_ENABLE:-0}"
export VLLM_ALLREDUCE_USE_SYMM_MEM="${VLLM_ALLREDUCE_USE_SYMM_MEM:-0}"
export VLLM_USE_NCCL_SYMM_MEM="${VLLM_USE_NCCL_SYMM_MEM:-0}"
export VLLM_ENABLE_K3_LATENT_MOE_TAIL_FUSION="${VLLM_ENABLE_K3_LATENT_MOE_TAIL_FUSION:-1}"
export VLLM_NIXL_SIDE_CHANNEL_HOST="$NIXL_HOST_IP"
export VLLM_NIXL_SIDE_CHANNEL_PORT="${VLLM_NIXL_SIDE_CHANNEL_PORT:-7480}"
# The fabric egress device is per node, but this fleet cannot use the
# reference preset's routing-table derivation. The reference fleet runs IPoIB,
# so a route lookup lands on the fabric device; here the eight 400G HCAs carry
# no IP at all - `ip -br addr` shows link-local IPv6 and nothing else - so the
# same lookup would return enp1s0 and quietly move every KV transfer onto the
# public NIC. It would handshake, pass a check, and cost an order of magnitude
# of bandwidth with nothing in the logs to say so.
#
# So pin the HCAs directly and let UCX address them by LID over RDMA. Only the
# ZMQ side channel needs an IP, and that is what NIXL_HOST_IP is for. Both
# nodes sit on one rack fabric (ibnetdiscover from node 1 resolves node 2's
# port GUIDs), which is what makes RDMA reachable here; re-check that before
# assuming it on a fleet provisioned later.
if [ -z "${UCX_NET_DEVICES:-}" ]; then
  UCX_DEVS=""
  for _p in /sys/class/infiniband/*/ports/1; do
    [ -r "$_p/state" ] || continue
    grep -q ACTIVE "$_p/state" || continue
    # Skip the 100G management HCAs; the eight 400G ports are the data fabric.
    grep -q 400 "$_p/rate" 2>/dev/null || continue
    _dev=$(basename "$(dirname "$(dirname "$_p")")")
    UCX_DEVS="${UCX_DEVS:+$UCX_DEVS,}${_dev}:1"
  done
  if [ -n "$UCX_DEVS" ]; then
    UCX_NET_DEVICES="$UCX_DEVS"
  else
    # No active 400G HCA. Fall back to the routed interface so the engine still
    # boots, and say so - this is the slow path, not the intended one.
    say "no active 400G HCA found; falling back to the routed interface"
    UCX_DEFAULT_DEV=$( (ip route get "$NIXL_HOST_IP" 2>/dev/null || true) | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)
    UCX_NET_DEVICES="${UCX_DEFAULT_DEV:?no route to NIXL_HOST_IP; set UCX_NET_DEVICES}"
  fi
fi
export UCX_NET_DEVICES
say "UCX_NET_DEVICES=$UCX_NET_DEVICES side_channel=$NIXL_HOST_IP"
export UCX_TCP_PORT_RANGE=6300-6500
# UCX plugins of the CUDA nixl build. This is required here, not optional as it
# is on ROCm: the wheel ships UCX under an auditwheel-mangled <pkg>.libs/ucx
# directory that UCX's loader does not search, so an unset UCX_MODULE_DIR loads
# the IB transports but silently drops libuct_cuda. UCX then says "UCX CUDA
# support was not found! GPU memory is not supported" once, at warn level, and
# proceeds - handshake succeeds, produce and consume pass, and every KV transfer
# is staged through host memory. Measured 2026-08-15 on this image.
#
# Derive it from whichever nixl variant actually imports (the image carries both
# nixl_cu12 and nixl_cu13; CUDA 13 here resolves to cu13) rather than pinning a
# path that moves with the wheel.
if [ -z "${UCX_MODULE_DIR:-}" ]; then
  UCX_MODULE_DIR=$(python3 -c '
import os, nixl._bindings as b
d = os.path.dirname(os.path.abspath(b.__file__))
cand = d + ".libs/ucx"
print(cand if os.path.isdir(cand) else "")
' 2>/dev/null || true)
fi
if [ -z "$UCX_MODULE_DIR" ] || [ ! -f "$UCX_MODULE_DIR/libuct_cuda.so" ]; then
  say "no UCX module dir with libuct_cuda.so (looked at '${UCX_MODULE_DIR:-<none>}')."
  say "  Refusing to serve: UCX would fall back to host-staged KV transfer without saying so."
  say "  Set UCX_MODULE_DIR to the nixl wheel's ucx/ directory to override."
  exit 1
fi
export UCX_MODULE_DIR
say "UCX_MODULE_DIR=$UCX_MODULE_DIR"
if [ "$OFFLOAD" = "on" ]; then
  say "offload=on ${OFFLOAD_NUM_IN_GROUP}-in-${OFFLOAD_GROUP_SIZE} layers, params=$OFFLOAD_PARAMS, prefetch_step=$OFFLOAD_PREFETCH_STEP, gpu_mem_util=$GPU_MEM_UTIL, load_format=$LOAD_FORMAT, eager=$EAGER, max_model_len=$MAX_MODEL_LEN"
else
  say "offload=off"
fi

exec /usr/local/bin/vllm serve "$MODELS/Kimi-K3" \
  --served-model-name moonshotai/Kimi-K3 \
  --tensor-parallel-size 8 \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --block-size 128 \
  --load-format "$LOAD_FORMAT" \
  --kv-cache-dtype fp8 \
  --attention-config '{"use_prefill_query_quantization": true, "mla_prefill_backend": "FLASHINFER"}' \
  --disable-custom-all-reduce \
  --enable-expert-parallel \
  --moe-backend flashinfer_trtllm \
  --all2all-backend allgather_reducescatter \
  --no-enable-flashinfer-autotune \
  --reasoning-parser kimi_k3 \
  --trust-remote-code \
  --no-disable-hybrid-kv-cache-manager \
  --enable-auto-tool-choice \
  --tool-call-parser kimi_k3 \
  "$CACHE_FLAG" \
  --max-num-batched-tokens 16384 \
  --language-model-only \
  ${EAGER_ARGS[@]+"${EAGER_ARGS[@]}"} \
  ${OFFLOAD_ARGS[@]+"${OFFLOAD_ARGS[@]}"} \
  --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}' \
  --host :: --port "$PORT" >> "$LOG_FILE" 2>&1
