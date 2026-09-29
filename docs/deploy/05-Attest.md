# Gate E: Attest the live engines

Each running engine needs an attestation document and a sidecar to serve it. You capture the inputs from the live process, generate the document, and start the sidecar. Once every engine has passed, you finalize the fleet contract from the router.

## Check the router inventory

On the router, `.env.router` resolves the endpoint references in `runs/deployment/fleet.json`. Open that file and confirm it lists each running engine with its URL and attestation URL, plus the model, initial roles, SLO values, and profile path.

Each engine's Gate C `ENGINE_RUN` directory should still hold its container ID and logs. Together with `cache-layout.json`, those files are the inputs for everything below.

## Capture the attestation inputs

Run these steps in each engine's role shell.

### 1. Save the startup log

```bash
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
export ENGINE_STARTUP_LOG="$ENGINE_RUN/startup.log"
(set -o noclobber; docker logs "$ENGINE_CONTAINER" > "$ENGINE_STARTUP_LOG" 2>&1)
```

### 2. Capture the NIXL connector version

```bash
.venv/bin/python tools/deployment/attestation_contract.py capture-nixl --run "$ENGINE_RUN"
```

The capture is tied to the deployed build through the image ID and container ID. Note that `contract.nixl_connector_version` is the installed connector's `NIXL_CONNECTOR_VERSION`. It is not the pinned `nixl_version` package. Only the connector version goes into the peer compatibility hash.

### 3. Capture the model dimensions

```bash
umask 077
.venv/bin/python tools/deployment/attestation_contract.py capture-model-dimensions --run "$ENGINE_RUN"
cat "$ENGINE_RUN/model-dimensions.live.json"
```

Before it captures anything, the command confirms that the live container's plan and launcher hashes match `launch.json`. The dimensions it records are `head_size`, `kv_heads`, `hidden_layers`, and `model_architecture`. They come from `ModelConfig.get_head_size()`, `get_total_num_kv_heads()`, and `get_total_num_hidden_layers()`, plus the resolved architecture. The record also carries `use_mla` and the identifying hashes. On a DeepSeek-style model with MLA enabled, the head size is derived from `kv_lora_rank + qk_rope_head_dim`.

### 4. Capture the cache grouping

`cache-registration` writes one capture per `ENGINE_RUN` and will not overwrite it, so pick your source deliberately.

The startup log is the usual choice. It must name exactly one layout across its `Using <layout> KV cache layout.` lines.

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$ENGINE_RUN/cache-registration.json"
```

If you'd rather use the resolved layout in `cache-layout.json`:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --runtime-layout "$ENGINE_RUN/cache-layout.json"
```

In vLLM v0.29.0, `BLHNC`, `BLNHC`, and `BHLNC` have `is_block_outermost=true`. `LBHNC`, `LBNHC`, and `LHBNC` have it set to `false`. The record stores the layout name and `cross_layers_blocks`, along with the hashes that tie it to the plan and the image.

### 5. Compare the layout with the representative's

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

If the group signature, resolved layout, and page geometry all match, the engine inherits the representative's [Gate D fabric budget](04-Qualify-Fabric.md#build-the-source-budget). If the layout or page geometry differs, that engine needs its own serving capture, budget, and edge comparisons.

### 6. Capture the transfer direction

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

Two details from the pinned API help here. `NixlConnector` is an alias for `NixlPullConnector`, and `kv_both` means the engine can both produce and consume KV. If the script finds zero or several connector classes in `image-check.log`, rerun the connector resolution with the pinned image check.

### 7. Confirm the connector in the startup log

Check that the connector class you just recorded appears in the serving startup log.

### 8. Capture handshake enforcement

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" handshake-policy --run "$ENGINE_RUN"
cat "$ENGINE_RUN/handshake-policy.json"
```

The pinned NIXL worker reads `enforce_handshake_compat` from the transfer config, defaults it to `True`, and stores the result as `self.enforce_compat_hash`. Current launcher plans set the value explicitly. Older plans fall back to the worker default. Either way, the capture only passes if the effective value is Boolean `true`. If it isn't, fix the launch configuration, check a new plan, and restart the engine from it.

## Generate and serve the attestation

### 1. Generate the document

```bash
.venv/bin/python tools/deployment/attestation_contract.py generate \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
```

### 2. Confirm which process you're attesting

Recheck the engine's `/health`, `/version`, and `process_start_time_seconds`. You want to be sure the document describes the process that is running right now, not one from before a restart.

### 3. Start the sidecar

Run it as the engine's user, or as root for container engines.

```bash
.venv/bin/python tools/deployment/attestation_contract.py serve --run "$ENGINE_RUN"
```

If `checked.json` shows prefix caching on and cache events published, the sidecar also subscribes to the engine's cache events and serves the [residency routes](../cli/Attest.md#residency). Once it has applied the engine's full event history, `GET /v1/residency` reports `"known": true`.

### 4. Check the sidecar from the router

Discovery already put the role's attestation URL in the fleet file. From the router, hit the sidecar's `/health` and `/v1/attestation` over the trusted control network.

### 5. Verify the sidecar against the engine

In a second shell for the same engine role:

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

### 6. Repeat for every engine

Run the capture, generate, and serve steps for each engine before moving on.

### 7. Finalize the fleet contract

When every sidecar has passed, run this once from the router shell:

```bash
.venv/bin/python tools/deployment/attestation_contract.py finalize-fleet --fleet runs/deployment/fleet.json
```

`finalize-fleet` checks each sidecar's attestation against its engine's live identity and requires every engine to carry the same complete contract. It saves the previous fleet file under `runs/` and writes `engine_contract` into `runs/deployment/fleet.json`.

If a sidecar fails, the error names the engine and the checks that failed. If two engines disagree, the differing field points to the engine with the bad input. Fix that engine, restart its sidecar against the checked process, and run the finalization again.

Leave every engine and sidecar running through profiling, preflight, and the trial.

Next: [Gate F: Profile once and run the live KV contract](06-Profile-and-Preflight.md).
