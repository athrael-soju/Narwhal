# Gate E: Attest the live engine processes

## Confirm router inventory

On the router, confirm that `runs/deployment/fleet.json` names the running engines, their URLs and attestation URLs, the model, initial roles, SLO values, and profile path. `.env.router` resolves the endpoint references.

For each engine, use its Gate C `ENGINE_RUN` directory and captured `cache-layout.json` as inputs to attestation. Retain the container ID and logs in that directory.

## Capture attestation inputs

For each live engine:

```bash
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
export ENGINE_STARTUP_LOG="$ENGINE_RUN/startup.log"
(set -o noclobber; docker logs "$ENGINE_CONTAINER" > "$ENGINE_STARTUP_LOG" 2>&1)
```

Capture NIXL connector protocol version:

```bash
.venv/bin/python tools/deployment/attestation_contract.py capture-nixl --run "$ENGINE_RUN"
```

`contract.nixl_connector_version` comes from the installed connector's `NIXL_CONNECTOR_VERSION`; it is distinct from the pinned `nixl_version` package and participates in the peer compatibility hash. Image ID and serving container ID bind the capture to the deployed build.

Capture compatibility-hash model dimensions from the installed runtime:

```bash
umask 077
.venv/bin/python tools/deployment/attestation_contract.py capture-model-dimensions --run "$ENGINE_RUN"
cat "$ENGINE_RUN/model-dimensions.live.json"
```

The fields are `head_size`, `kv_heads`, `hidden_layers`, and `model_architecture`, derived from `ModelConfig.get_head_size()`, `get_total_num_kv_heads()`, and `get_total_num_hidden_layers()` plus resolved architecture. For DeepSeek-style MLA with MLA enabled, the head-size resolver derives size from `kv_lora_rank + qk_rope_head_dim`.

Retain these values with `use_mla`, model-config hash, image identity, application revision, and serving plan hash. The command reads plan and launcher from the live container, checks their hashes against `launch.json`, and records private logs. Run it on every engine.

Choose one source to capture physical cache grouping. Run `cache-registration` once per `ENGINE_RUN`; it refuses to overwrite an existing capture.

To use the serving startup log:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$ENGINE_RUN/cache-registration.json"
```

Alternatively, use this engine's resolved layout from `cache-layout.json`:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --runtime-layout "$ENGINE_RUN/cache-layout.json"
```

For vLLM v0.29.0 layouts, `BLHNC`, `BLNHC`, and `BHLNC` have `is_block_outermost=true`; `LBHNC`, `LBNHC`, and `LHBNC` have `is_block_outermost=false`. The generated record retains `cross_layers_blocks`, layout name, enum source hash, input hash, checked plan hash, and image identity. When using the startup log, it must contain exactly one resolved layout line of the form `Using <layout> KV cache layout.`

Compare live layout with the representative:

```bash
export REPRESENTATIVE_LAYOUT='<layout name from representative cache-layout.json>'
python3 - <<'PY_CACHE_MATCH'
import json
import os
from pathlib import Path

record = json.loads((Path(os.environ["ENGINE_RUN"]) / "cache-registration.json").read_text())
actual = record["kv_cache_layout"]
expected = os.environ["REPRESENTATIVE_LAYOUT"]
if actual != expected:
    raise SystemExit(f"Resolved layout {actual} differs from representative {expected}.")
print(f"Resolved layout {actual} matches the cache representative.")
PY_CACHE_MATCH
```

When the group signature, resolved layout, and page geometry match, the group can continue using the representative's [Gate D fabric budget](04-Qualify-Fabric.md#build-the-source-budget). A differing layout or page geometry requires its own serving capture, budget, and edge comparisons.

Capture transfer direction from the checked connector resolution. In the pinned API, `NixlConnector` aliases `NixlPullConnector`; `kv_both` describes ability to produce and consume KV.

```bash
python3 - <<'PY_TRANSFER_MODE'
import hashlib
import json
import os
from pathlib import Path
os.umask(0o077)
run = Path(os.environ["ENGINE_RUN"])
plan_data = (run / "launch.json").read_bytes()
plan = json.loads(plan_data)
checked = json.loads((run / "checked.json").read_text())
plan_hash = hashlib.sha256(plan_data).hexdigest()
if checked["plan_sha256"] != plan_hash:
    raise SystemExit("Use the image check belonging to this launch plan")
log = run / "image-check.log"
log_data = log.read_bytes()
resolved = set()
for line in log_data.decode().splitlines():
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(item, dict) and isinstance(item.get("connector"), str):
        resolved.add(item["connector"])
if len(resolved) != 1:
    raise SystemExit("Retain one factory-resolved connector class from this image check")
connector = resolved.pop()
modes = {"NixlPullConnector": "pull", "NixlPushConnector": "push"}
mode = modes.get(connector.rsplit(".", 1)[-1])
if mode is None:
    raise SystemExit("Inspect the pinned connector implementation for its transfer protocol")
record = {
    "transfer_mode": mode,
    "configured_connector": plan["connector"]["kv_connector"],
    "kv_role": plan["connector"]["kv_role"],
    "resolved_connector": connector,
    "image_id": checked["image_id"],
    "plan_sha256": plan_hash,
    "source": str(log),
    "source_sha256": hashlib.sha256(log_data).hexdigest(),
}
with (run / "transfer-mode.json").open("x") as output:
    json.dump(record, output, indent=2)
    output.write("\n")
print(f"Captured transfer_mode={mode} from {connector}")
PY_TRANSFER_MODE
```

Check that connector class against the serving startup log. When the log yields zero or multiple classes, repeat connector resolution with the pinned image check.

Capture handshake enforcement:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" handshake-policy --run "$ENGINE_RUN"
cat "$ENGINE_RUN/handshake-policy.json"
```

The pinned NIXL worker resolves `kv_transfer_config.get_from_extra_config("enforce_handshake_compat", True)` into `self.enforce_compat_hash`. Current launcher plans set `enforce_handshake_compat=true` explicitly; older plans inherit the installed worker default. The accepted effective value is Boolean `true`. A false or non-Boolean result requires corrected launch configuration and engine restart from a newly checked plan.

## Generate and serve the attestation

Generate the role-specific document:

```bash
.venv/bin/python tools/deployment/attestation_contract.py generate \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
```

Before sidecar start, reconfirm engine `/health`, `/version`, and `process_start_time_seconds` to identify the process being attested.

Start the sidecar:

```bash
.venv/bin/python tools/deployment/attestation_contract.py serve --run "$ENGINE_RUN"
```

Discovery has already placed the role's attestation URL in the router fleet. Verify `/health` and `/v1/attestation` from the router over the trusted control network.

In a second shell for the same role, verify sidecar and process together:

```bash
export ENGINE_ROLE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["role"])' "$ENGINE_RUN/launch.json")"
export ATTEST_URL_VAR="NARWHAL_NODE_${ENGINE_ROLE#engine-}_ATTESTATION_URL"
export ATTEST_BASE="${!ATTEST_URL_VAR}"
export ATTEST_BASE="${ATTEST_BASE%/v1/attestation}"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
umask 077
export ATTEST_RUN="$(mktemp -d "$ENGINE_RUN/attestation-check-XXXXXX")"
.venv/bin/python - <<'PY_ATTEST_CHECK'
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

import httpx
from narwhal.engines.attestation import (
    AttestationDocument, fetch_engine_identity, verify_attestation,
)

out = Path(os.environ["ATTEST_RUN"])
base = os.environ["ATTEST_BASE"].rstrip("/")
responses = {}
with httpx.Client(timeout=15) as client:
    for name, route in (("health", "/health"), ("attestation", "/v1/attestation")):
        response = client.get(base + route)
        (out / f"{name}.json").write_text(response.text)
        (out / f"{name}.status").write_text(str(response.status_code) + "\n")
        responses[name] = response
for response in responses.values():
    if response.status_code != 200:
        raise SystemExit(f"Expected HTTP 200; inspect the captures in {out}")
plan = json.loads((Path(os.environ["ENGINE_RUN"]) / "launch.json").read_text())
key = os.environ.get("NARWHAL_ENGINE_API_KEY")
headers = {"authorization": "Bearer " + key} if key else {}
identity = asyncio.run(fetch_engine_identity(plan["endpoint"], headers=headers))
(out / "identity.json").write_text(json.dumps(asdict(identity), indent=2) + "\n")
document = AttestationDocument.load(os.environ["ATTEST_DOCUMENT"])
failures = verify_attestation(responses["attestation"].json(), document.contract, identity)
if failures:
    raise SystemExit("; ".join(failures))
print("Both sidecar endpoints passed; attestation matches the live engine and contract")
PY_ATTEST_CHECK
```

Run this for every engine. After all sidecars pass, finalise once from the router shell:

```bash
.venv/bin/python tools/deployment/attestation_contract.py finalize-fleet --fleet runs/deployment/fleet.json
```

The command reads every live engine and sidecar, verifies process identity and full contract, requires the same contract on every engine, retains the prior fleet document under `runs/`, and writes `engine_contract` into `runs/deployment/fleet.json`.

A contract mismatch identifies the affected engine input. Correct that engine, restart its sidecar against the checked process, and rerun finalisation. Keep all engines and sidecars running through profiling, preflight, and trial.

Continue with [Gate F: Profile once and run the live KV contract](06-Profile-and-Preflight.md).
