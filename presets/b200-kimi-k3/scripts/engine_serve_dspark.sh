#!/usr/bin/env bash
# engine_serve_dspark.sh — standalone engine with DSpark speculative decoding
# (7-token block draft) + prefix caching, the CUDA sibling. UNVALIDATED on
# B200: the reference build was validated 2026-08-12 on the MI355X
# fleet and stays OUT of narwhal PD there because the DS conv layout -
# required by NIXL - is unset. Same standing here: do not join the fleet.
#
# Keep it invisible to narwhal: run on a port NOT in the fleet config
# (e.g. 9002). Requires the container from container_recreate.sh with the
# draft weights at $MODELS/Kimi-K3-DSpark.
#
# Usage (run ON the node):
#   MODELS=<weights dir> bash engine_serve_dspark.sh [PORT]
set -euo pipefail

# --max-num-seqs caps the draft attention-logits workspace (on the reference
# fleet it scaled with max_num_seqs at 1M ctx into triple-digit GiB). The CUDA
# build's attention backend choice for the draft is NOT verified:
# FLASHMLA/TRITON_MLA selection belongs to the vendor recipe - see manifest.
MAX_NUM_SEQS="${MAX_NUM_SEQS:-8}"
PORT="${1:-9002}"
: "${MODELS:?set the node-local weights directory, see manifest.md}"
LOG_DIR="${LOG_DIR:-./runs}"

# Deliberately NOT setting VLLM_SSM_CONV_STATE_LAYOUT=DS: DS cannot express
# the >0 spec-decode conv-state shift as a contiguous copy
# (v1/worker/gpu/model_states/mamba_hybrid.py:141). Default SD layout used.
# No --kv-transfer-config: spec decode is NIXL-incompatible in this build.

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
  --max-num-seqs "$MAX_NUM_SEQS" \
  --language-model-only \
  --speculative-config '{"model":"'"$MODELS"'/Kimi-K3-DSpark","method":"dspark","num_speculative_tokens":7,"draft_sample_method":"probabilistic","rejection_sample_method":"block"}' \
  --host :: --port "$PORT" >> "$LOG_DIR/engine_${PORT}_dspark.log" 2>&1
