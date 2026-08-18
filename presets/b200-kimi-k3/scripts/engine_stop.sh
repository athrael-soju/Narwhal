#!/usr/bin/env bash
# engine_stop.sh — stop the engine running inside the node-local container.
# Vendor-neutral; only the default container name is fleet-specific.
# Usage: bash engine_stop.sh [CONTAINER]
set -euo pipefail
CONTAINER="${1:-narwhal-b200-d1}"
docker exec "$CONTAINER" pkill -INT -f "vllm serve"
echo "stop signal sent to engine in $CONTAINER (VRAM frees within ~10-60 s)"
