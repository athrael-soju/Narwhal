#!/usr/bin/env bash
# engine_stop.sh — stop the engine running inside the node-local container.
# Usage: bash engine_stop.sh <container>
set -euo pipefail
CONTAINER="${1:?usage: engine_stop.sh <container>}"
docker exec "$CONTAINER" pkill -INT -f "vllm serve"
echo "stop signal sent to engine in $CONTAINER (VRAM frees within ~10-60 s)"
