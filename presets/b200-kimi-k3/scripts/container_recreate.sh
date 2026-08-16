#!/usr/bin/env bash
# container_recreate.sh — recreate the node-local sleep-infinity engine
# container on the B200 fleet. CUDA sibling of the MI355X script:
# docker --gpus replaces the /dev/kfd render device plumbing.
# Run on the node. Stop the engine first (engine_stop.sh).
set -euo pipefail
CONTAINER="${1:-narwhal-b200-d1}"
# The manifest pins the validated build and its digest; point IMAGE at it, or
# at a candidate you mean to validate.
IMAGE="${IMAGE:?set IMAGE to the CUDA vLLM build for B200, see manifest.md}"
# Model weights path on the node.
MODELS="${MODELS:?set MODELS to the node-local Kimi-K3 weights directory}"
# The weights directory is self-contained (config, remote code, 96 shards), so
# nothing should need the hub at serve time. Set HF_CACHE to the node's shared
# HF cache anyway if it has one: it is mounted read-write with HF_HOME pointed
# at it, because an unset HF_HOME sends any stray lookup to a container-local
# path that vanishes on recreate. Leave it empty to skip the mount.
HF_CACHE="${HF_CACHE-}"
HF_ARGS=()
if [ -n "$HF_CACHE" ]; then
  HF_ARGS=(-v "$HF_CACHE:$HF_CACHE" -e "HF_HOME=$HF_CACHE")
fi
# The deploy tree rides in read-only so engine_serve.sh runs from inside the
# container without a docker cp that a recreate would throw away. Logs go to
# LOG_DIR on the host, bound read-write, so they outlive the container too.
NARWHAL_DIR="${NARWHAL_DIR:-$HOME/narwhal}"
LOG_DIR="${LOG_DIR:-$HOME/narwhal-logs}"
mkdir -p "$LOG_DIR"

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
# --entrypoint bash is not optional: this image's ENTRYPOINT is ["vllm","serve"],
# so a bare `-c "sleep infinity"` reaches vLLM's parser as --compilation-config
# and the container exits 2 before it ever holds a GPU. The ROCm sibling omits
# the flag because that image entrypoints on a shell.
docker run -d --name "$CONTAINER" \
  --gpus all --ipc host --network host --shm-size 64g \
  --device /dev/infiniband \
  -v "$MODELS:$MODELS:ro" \
  -v "$NARWHAL_DIR:/narwhal:ro" \
  -v "$LOG_DIR:/logs" \
  ${HF_ARGS[@]+"${HF_ARGS[@]}"} \
  --entrypoint bash \
  "$IMAGE" -c "sleep infinity"
echo "recreated $CONTAINER from $IMAGE"
