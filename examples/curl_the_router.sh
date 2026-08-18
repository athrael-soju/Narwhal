#!/usr/bin/env bash
# The router's HTTP surface, one curl per route, against the stub fleet.
#
#   make stub-fleet                                          # terminal 1
#   .venv/bin/narwhal-profile --fleet config/fleet.stub.json # terminal 2, once
#   .venv/bin/narwhal-serve --fleet config/fleet.stub.json --port 8100
#   examples/curl_the_router.sh                              # terminal 3
#
# docs/Api.md documents every response shape.
set -euo pipefail
BASE="${BASE:-http://localhost:8100}"

echo "== the one model this router fronts"
curl -sf "$BASE/v1/models"; echo

echo "== liveness"
curl -sf "$BASE/health"; echo

echo "== a completion through a real split (prefill and decode legs)"
curl -sf -X POST "$BASE/v1/completions" \
  -H 'content-type: application/json' \
  -d '{"prompt": "the narwhal surfaces", "max_tokens": 8}'; echo

echo "== the same, streamed"
curl -sfN -X POST "$BASE/v1/completions" \
  -H 'content-type: application/json' \
  -d '{"prompt": "the narwhal dives", "max_tokens": 4, "stream": true}'

echo "== the live scheduler picture (docs/Api.md, field by field)"
curl -sf "$BASE/arrow/state" | python3 -m json.tool
