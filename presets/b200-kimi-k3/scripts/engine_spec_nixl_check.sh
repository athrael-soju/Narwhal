#!/usr/bin/env bash
# engine_spec_nixl_check.sh — image boot-check for candidate CUDA builds:
# DS conv layout + speculative decoding + NixlConnector TOGETHER, the trio the
# reference fleet's deployed image hard-asserts against (mamba_hybrid.py:141).
# Standalone, invisible to narwhal. Run inside the
# candidate-image container. UNVALIDATED on B200. Without the OFFLOAD
# machinery from engine_serve.sh this script cannot boot Kimi-K3 on B200, and
# it is a scaffold pending validation.
set -euo pipefail
PORT="${1:-9002}"
: "${MODELS:?set the node-local weights directory, see manifest.md}"
: "${NIXL_HOST_IP:?set the node serving address, see fleet.json}"
LOG_DIR="${LOG_DIR:-./runs}"
export VLLM_SSM_CONV_STATE_LAYOUT=DS
export VLLM_NIXL_SIDE_CHANNEL_HOST="$NIXL_HOST_IP"
export VLLM_NIXL_SIDE_CHANNEL_PORT="${VLLM_NIXL_SIDE_CHANNEL_PORT:-7481}"
: "${UCX_NET_DEVICES:?name the 400G HCA list, e.g. mlx5_0:1,mlx5_1:1,...}"
export UCX_TCP_PORT_RANGE=6300-6500
# UCX_MODULE_DIR is mandatory on this image: the nixl wheel ships UCX under a
# path its loader does not search, so unset, KV transfers stage silently
# through host memory. Same derive-and-refuse as engine_serve.sh.
if [ -z "${UCX_MODULE_DIR:-}" ]; then
  UCX_MODULE_DIR=$(python3 -c '
import os, nixl._bindings as b
d = os.path.dirname(os.path.abspath(b.__file__))
cand = d + ".libs/ucx"
print(cand if os.path.isdir(cand) else "")
' 2>/dev/null || true)
fi
if [ -z "$UCX_MODULE_DIR" ] || [ ! -f "$UCX_MODULE_DIR/libuct_cuda.so" ]; then
  echo "engine_spec_nixl_check.sh: no UCX module dir with libuct_cuda.so (looked at '${UCX_MODULE_DIR:-<none>}')." >&2
  echo "engine_spec_nixl_check.sh:   Refusing to serve: UCX would fall back to host-staged KV transfer without saying so." >&2
  echo "engine_spec_nixl_check.sh:   Set UCX_MODULE_DIR to the nixl wheel's ucx/ directory to override." >&2
  exit 1
fi
export UCX_MODULE_DIR
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
