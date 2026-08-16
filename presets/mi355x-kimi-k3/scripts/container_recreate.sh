#!/usr/bin/env bash
# container_recreate.sh - (re)create the node-local sleep-infinity container
# that holds the engine. Site specifics (image, device pass-through, binds)
# come from the same env file engine_serve.sh reads. Stop the engine first
# (engine_stop.sh); the container is removed and recreated.
#
# Usage: bash container_recreate.sh <container>
set -euo pipefail

CONTAINER="${1:?usage: container_recreate.sh <container>}"
ENGINE_ENV="${ENGINE_ENV:-$(dirname "$0")/engine.env}"
if [ ! -f "$ENGINE_ENV" ]; then
  echo "container_recreate.sh: no env file at $ENGINE_ENV" >&2
  echo "copy config/engine.env.example, fill in your fleet's values, and" >&2
  echo "place it there or point ENGINE_ENV at it" >&2
  exit 1
fi
# shellcheck source=/dev/null
. "$ENGINE_ENV"

: "${CONTAINER_IMAGE:?$ENGINE_ENV must set CONTAINER_IMAGE}"
: "${CONTAINER_DEVICE_ARGS:?$ENGINE_ENV must set CONTAINER_DEVICE_ARGS}"

BIND_ARGS=()
for bind in ${CONTAINER_BINDS:-}; do BIND_ARGS+=(-v "$bind"); done

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
# shellcheck disable=SC2086 # CONTAINER_DEVICE_ARGS is a flag list by contract
docker run -d --name "$CONTAINER" \
  --ipc host --network host --shm-size "${CONTAINER_SHM_SIZE:-64g}" \
  $CONTAINER_DEVICE_ARGS \
  ${BIND_ARGS[@]+"${BIND_ARGS[@]}"} \
  "$CONTAINER_IMAGE" -c "sleep infinity"
echo "recreated $CONTAINER from $CONTAINER_IMAGE"
