#!/usr/bin/env bash
# Engine launcher template. Narwhal's whole contract with this script (§5.2):
# start a vLLM engine that is stateless, serves the preset's model, and can
# move KV to any peer - concretely:
#
#   --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}'
#
# plus a --host/--port matching the preset's fleet.json URL for this node.
# Everything else - image, device plumbing, parallelism, memory tuning - is
# your accelerator's business, not the router's. Adapt freely; the
# produce/consume gates are the acceptance test, not this file.
#
# Engine-version caveats that may apply to YOUR build (not your hardware):
# see "Engine compatibility notes" in docs/Deploy.md before first launch.
set -euo pipefail
echo "template: copy the nearest real preset's launcher and adapt, or write your own against the contract above" >&2
exit 2
