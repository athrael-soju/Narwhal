# Gate E: Attest the live engines

## Check the router inventory

On the router, confirm that `runs/deployment/fleet.json` lists:

- Each running engine with URL and attestation URL references that resolve in `.env.router`.
- The model.
- The initial roles.
- The SLO values.
- The profile path.

Capture inputs:

- The container ID and logs in each engine's Gate C `ENGINE_RUN` directory.
- `cache-layout.json`.

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

| Version                           | Source                                         | Peer compatibility hash |
| --------------------------------- | ---------------------------------------------- | ----------------------- |
| `contract.nixl_connector_version` | Installed connector's `NIXL_CONNECTOR_VERSION` | Included                |
| `nixl_version`                    | Pinned NIXL package                            |                         |

### 3. Capture the model dimensions

```bash
umask 077
.venv/bin/python tools/deployment/attestation_contract.py capture-model-dimensions --run "$ENGINE_RUN"
cat "$ENGINE_RUN/model-dimensions.live.json"
```

The live container's plan and launcher hashes match `launch.json`.

| Dimension            | Source                                                                                                                         |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `head_size`          | `ModelConfig.get_head_size()`, or `kv_lora_rank + qk_rope_head_dim` on a DeepSeek-style model with MLA enabled                 |
| `kv_heads`           | `get_total_num_kv_heads()`                                                                                                     |
| `hidden_layers`      | `get_total_num_hidden_layers()`                                                                                                |
| `model_architecture` | Resolved architecture                                                                                                          |

### 4. Capture the cache grouping

Sources for `cache-registration`, one final capture per `ENGINE_RUN`:

| Source                          | Requirement                                                                   |
| ------------------------------- | ----------------------------------------------------------------------------- |
| Startup log (the usual choice)  | Names exactly one layout across its `Using <layout> KV cache layout.` lines.  |
| `cache-layout.json`             | Holds the resolved layout.                                                    |

From the startup log:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$ENGINE_RUN/cache-registration.json"
```

From the resolved layout in `cache-layout.json`:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --runtime-layout "$ENGINE_RUN/cache-layout.json"
```

| Layouts in vLLM v0.29.0         | Block grouping                    |
| ------------------------------- | --------------------------------- |
| `BLHNC`, `BLNHC`, and `BHLNC`   | `is_block_outermost=true`         |
| `LBHNC`, `LBNHC`, and `LHBNC`   | `is_block_outermost` is `false`   |

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

| Comparison with the representative                           | Fabric budget                                                                                                   |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Group signature, resolved layout, and page geometry all match | The engine inherits the representative's [Gate D fabric budget](04-Qualify-Fabric.md#build-the-source-budget). |
| Layout or page geometry differs                              | The engine needs a separate serving capture, budget, and edge comparisons.                                        |

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

| Pinned API term  | Meaning                                  |
| ---------------- | ---------------------------------------- |
| `NixlConnector`  | Alias for `NixlPullConnector`.           |
| `kv_both`        | The engine produces and consumes KV.     |

If the script finds zero or several connector classes in `image-check.log`, rerun the connector resolution with the pinned image check.

### 7. Confirm the connector in the startup log

Confirm that the serving startup log names the connector class recorded in step 6.

### 8. Capture handshake enforcement

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" handshake-policy --run "$ENGINE_RUN"
cat "$ENGINE_RUN/handshake-policy.json"
```

The effective `enforce_handshake_compat` value is:

- The plan's transfer config setting, when set.
- The pinned NIXL worker default of `True`, otherwise.

The capture passes when the effective value is Boolean `true`.

When the capture fails:

1. Fix the launch configuration.
2. Check a new plan.
3. Restart the engine from the new plan.

## Generate and serve the attestation

### 1. Generate the document

```bash
.venv/bin/python tools/deployment/attestation_contract.py generate \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
```

### 2. Confirm which process you're attesting

1. Recheck the engine's `/health`, `/version`, and `process_start_time_seconds`.
2. Confirm the document describes the running process.

### 3. Start the sidecar

Run it as the engine's user, or as root for container engines.

```bash
.venv/bin/python tools/deployment/attestation_contract.py serve --run "$ENGINE_RUN"
```

When `checked.json` shows prefix caching on and cache events published, the sidecar serves the [residency routes](../cli/Attest.md#residency).

### 4. Check the sidecar from the router

From the router, request the sidecar's `/health` and `/v1/attestation` over the trusted control network.

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

Run the capture, generate, and serve steps for each engine.

### 7. Finalize the fleet contract

Run once from the router shell after every sidecar passes:

```bash
.venv/bin/python tools/deployment/attestation_contract.py finalize-fleet --fleet runs/deployment/fleet.json
```

`finalize-fleet` requires:

- Each sidecar's attestation matches its engine's live identity.
- Every engine carries the same complete contract.

`finalize-fleet` writes:

- `engine_contract` in `runs/deployment/fleet.json`.
- A copy of the previous fleet file under `runs/`.

| Failure               | Diagnosis                                                               |
| --------------------- | ----------------------------------------------------------------------- |
| A sidecar fails       | Error output lists the engine and the failed checks.                       |
| Two engines disagree  | The differing field identifies the engine with the bad input.            |

Recovery:

1. Fix the failing engine.
2. Restart its sidecar against the checked process.
3. Run `finalize-fleet` again.

Leave every engine and sidecar running through profiling, preflight, and the trial.

Next: [Gate F: Profile once and run the live KV contract](06-Profile-and-Preflight.md).
