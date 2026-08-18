#!/usr/bin/env bash
# engine_serve.sh - start a vLLM engine that can join a Narwhal fleet.
#
# This is a template launcher: everything model-, hardware- or site-specific
# lives in an env file (copy engine.env.example beside this script and fill in your
# fleet's values). The launcher itself holds only what Narwhal requires of
# every engine:
#   - NixlConnector with kv_role kv_both (role flips are inert without it)
#   - the NIXL side channel bound to the address the router and peers dial
#   - the UCX egress device derived from the routing table
#
# Usage (run inside the engine container ON the node):
#   NIXL_HOST_IP=<this node's fabric address> bash engine_serve.sh [PORT]
#
# Env knobs (beyond the env file):
#   ENGINE_ENV      path to the env file; default: engine.env beside this script
#   NIXL_HOST_IP    required - the address other engines reach this one on
#   PORT (arg $1)   default 8000
#   GPU_MEM_UTIL    default 0.90
#   PREFIX_CACHING  on|off, default on (see Deploy's compatibility notes)
#   UCX_NET_DEVICES override the interface that carries NIXL_HOST_IP
#   NIXL_PEER_IP    optional - another engine's address; refuses to launch
#                   when peers route out a different rail than the one
#                   carrying NIXL_HOST_IP
set -euo pipefail

ENGINE_ENV="${ENGINE_ENV:-$(dirname "$0")/engine.env}"
if [ ! -f "$ENGINE_ENV" ]; then
  echo "engine_serve.sh: no env file at $ENGINE_ENV" >&2
  echo "copy engine.env.example beside this script to engine.env, fill in your" >&2
  echo "values, or point ENGINE_ENV at your copy" >&2
  exit 1
fi
# shellcheck source=/dev/null
. "$ENGINE_ENV"

: "${MODEL_PATH:?$ENGINE_ENV must set MODEL_PATH}"
: "${SERVED_MODEL_NAME:?$ENGINE_ENV must set SERVED_MODEL_NAME}"
# NOTE: keep this message apostrophe-free - bash mis-quotes an apostrophe
# inside the ${var:?msg} expansion word.
: "${NIXL_HOST_IP:?set the fabric address of this node, see your fleet config}"

PORT="${1:-8000}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
LOG_DIR="${LOG_DIR:-./runs}"
PREFIX_CACHING="${PREFIX_CACHING:-on}"
CACHE_FLAG="--enable-prefix-caching"
[ "$PREFIX_CACHING" = "off" ] && CACHE_FLAG="--no-enable-prefix-caching"

export VLLM_NIXL_SIDE_CHANNEL_HOST="$NIXL_HOST_IP"
export VLLM_NIXL_SIDE_CHANNEL_PORT="${VLLM_NIXL_SIDE_CHANNEL_PORT:-7480}"
# Peers dial NIXL_HOST_IP, so KV leaves the interface that carries it. A wrong
# pin handshakes fine and fails under load.
if [ -z "${UCX_NET_DEVICES:-}" ]; then
  UCX_NET_DEVICES=$(ip -o addr show to "$NIXL_HOST_IP" 2>/dev/null \
    | awk '{print $2}' | head -1)
  if [ -z "$UCX_NET_DEVICES" ]; then
    echo "engine_serve.sh: no interface on this node carries $NIXL_HOST_IP;" >&2
    echo "check NIXL_HOST_IP against \`ip -br addr\`, or set" >&2
    echo "UCX_NET_DEVICES explicitly" >&2
    exit 1
  fi
fi
export UCX_NET_DEVICES
# The kernel routes peers out that same interface, or KV leaves with a source
# address peers never dialed. NIXL_PEER_IP names any other engine to hold it.
if [ -n "${NIXL_PEER_IP:-}" ]; then
  peer_dev=$( (ip route get "$NIXL_PEER_IP" 2>/dev/null || true) \
    | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)
  if [ -n "$peer_dev" ] && [ "$peer_dev" != "$UCX_NET_DEVICES" ]; then
    echo "engine_serve.sh: this node advertises $NIXL_HOST_IP on" >&2
    echo "$UCX_NET_DEVICES but routes to $NIXL_PEER_IP out $peer_dev." >&2
    echo "Route peers out $UCX_NET_DEVICES, or move the address to" >&2
    echo "$peer_dev, so identity and data path share one rail." >&2
    exit 1
  fi
fi

mkdir -p "$LOG_DIR"
# shellcheck disable=SC2086 # ENGINE_ARGS is a flag list by contract
exec vllm serve "$MODEL_PATH" \
  --served-model-name "$SERVED_MODEL_NAME" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  ${ENGINE_ARGS:-} \
  "$CACHE_FLAG" \
  --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}' \
  --host :: --port "$PORT" >> "$LOG_DIR/engine_${PORT}.log" 2>&1
