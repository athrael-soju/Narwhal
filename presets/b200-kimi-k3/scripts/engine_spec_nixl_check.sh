#!/usr/bin/env bash
# engine_spec_nixl_check.sh — image boot-check for candidate CUDA builds:
# DS conv layout + speculative decoding + NixlConnector TOGETHER, the trio the
# reference fleet's deployed image hard-asserts against (mamba_hybrid.py:141;
# the image-bump watch on that fleet). Standalone, invisible to narwhal. Run inside the
# candidate-image container. UNVALIDATED on B200.
set -euo pipefail
PORT="${1:-9002}"
: "${MODELS:?set the node-local weights directory, see manifest.md}"
: "${NIXL_HOST_IP:?set the node fabric IPv6 address, see fleet.json}"
LOG_DIR="${LOG_DIR:-./runs}"
export VLLM_SSM_CONV_STATE_LAYOUT=DS
export VLLM_NIXL_SIDE_CHANNEL_HOST="$NIXL_HOST_IP"
export VLLM_NIXL_SIDE_CHANNEL_PORT="${VLLM_NIXL_SIDE_CHANNEL_PORT:-7481}"
: "${UCX_NET_DEVICES:?name the fabric egress interface, see engine_serve.sh}"
export UCX_TCP_PORT_RANGE=6300-6500
exec /usr/local/bin/vllm serve "$MODELS/Kimi-K3" \
  --served-model-name moonshotai/Kimi-K3 \
  --tensor-parallel-size 8 \
  --max-model-len 1048576 \
  --gpu-memory-utilization 0.90 \
  --block-size 128 \
  --reasoning-parser kimi_k3 \
  --trust-remote-code \
  --no-disable-hybrid-kv-cache-manager \
  --enable-auto-tool-choice \
  --tool-call-parser kimi_k3 \
  --enable-prefix-caching \
  --max-num-batched-tokens 16384 \
  --max-num-seqs 4 \
  --language-model-only \
  --speculative-config "{\"model\":\"$MODELS/Kimi-K3-DSpark\",\"method\":\"dspark\",\"num_speculative_tokens\":7,\"draft_sample_method\":\"probabilistic\",\"rejection_sample_method\":\"block\"}" \
  --kv-transfer-config "{\"kv_connector\":\"NixlConnector\",\"kv_role\":\"kv_both\"}" \
  --host :: --port "$PORT" >> "$LOG_DIR/engine_${PORT}_spec_nixl.log" 2>&1
