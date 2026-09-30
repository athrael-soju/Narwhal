# Gate E: Attest the live engine processes

Attestation records what each running engine actually is: its NIXL protocol version, model dimensions, cache layout, transfer mode, and handshake policy. A sidecar then serves that record so the router can check it against the live process. Every engine goes through the same steps, and they can run concurrently.

## Confirm router inventory

On the router, check that `runs/deployment/fleet.json` lists the running engines with their URLs and attestation URLs, the model, the initial roles, the SLO values, and the profile path. `.env.router` fills in the endpoint references.

Each engine's inputs are the `ENGINE_RUN` directory from Gate C and the `cache-layout.json` captured there. Keep the container ID and logs in that directory as well.

## Capture attestation inputs

For each live engine, save the startup log:

```bash
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
export ENGINE_STARTUP_LOG="$ENGINE_RUN/startup.log"
(set -o noclobber; docker logs "$ENGINE_CONTAINER" > "$ENGINE_STARTUP_LOG" 2>&1)
```

### NIXL connector protocol version

```bash
.venv/bin/python tools/deployment/attestation_contract.py capture-nixl --run "$ENGINE_RUN"
```

This saves the installed connector's `NIXL_CONNECTOR_VERSION` to `nixl-connector-version.json`, and it becomes `contract.nixl_connector_version` in the attestation. Don't confuse it with the pinned `nixl_version` package. It's a different number, and it's the one that goes into the peer compatibility hash. The image ID and serving container ID tie the capture to the build you deployed.

### Model dimensions

The compatibility hash also includes the model's dimensions, read from the installed runtime:

```bash
umask 077
.venv/bin/python tools/deployment/attestation_contract.py capture-model-dimensions --run "$ENGINE_RUN"
cat "$ENGINE_RUN/model-dimensions.live.json"
```

You should see `head_size`, `kv_heads`, `hidden_layers`, and `model_architecture`. They come from `ModelConfig.get_head_size()`, `get_total_num_kv_heads()`, and `get_total_num_hidden_layers()`, plus the resolved architecture. For DeepSeek-style MLA with MLA enabled, the head size works out as `kv_lora_rank + qk_rope_head_dim`.

The command reads the plan and launcher from inside the live container, checks their hashes against `launch.json`, and writes private logs. Keep the output alongside `use_mla`, the model-config hash, the image identity, the application revision, and the serving plan hash. Run it on every engine.

### Cache registration

`cache-registration` records how the engine groups its physical cache. It can read either the startup log or the layout you captured in Gate C. Run it once per `ENGINE_RUN`, because it won't overwrite an existing capture.

From the startup log:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$ENGINE_RUN/cache-registration.json"
```

The log must name exactly one layout, in lines of the form `Using <layout> KV cache layout.`

Or from the captured layout:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --runtime-layout "$ENGINE_RUN/cache-layout.json"
```

In vLLM v0.29.0, `BLHNC`, `BLNHC`, and `BHLNC` have `is_block_outermost=true`, while `LBHNC`, `LBNHC`, and `LHBNC` have `is_block_outermost=false`. The record keeps `cross_layers_blocks`, the layout name, the hash of the enum source, the input hash, the checked plan hash, and the image identity.

Then compare the engine's layout with its group's representative:

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

If the group signature, layout, and page geometry all match, the engine can keep using its group's [Gate D fabric budget](04-Qualify-Fabric.md#build-the-source-budget). If the layout or page geometry differs, the engine needs its own serving capture, budget, and edge comparisons.

### Transfer mode

The transfer direction comes from the connector class that the image check resolved. In the pinned API, `NixlConnector` is an alias for `NixlPullConnector`, and `kv_both` only means the engine can both produce and consume KV. It doesn't say which side initiates a transfer.

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

Check the connector class against the serving startup log too. If the log shows no connector class, or more than one, resolve the connector again with the pinned image check.

### Handshake enforcement

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" handshake-policy --run "$ENGINE_RUN"
cat "$ENGINE_RUN/handshake-policy.json"
```

The pinned NIXL worker reads `kv_transfer_config.get_from_extra_config("enforce_handshake_compat", True)` into `self.enforce_compat_hash`. Plans from the current launcher set `enforce_handshake_compat=true` explicitly, and older plans fall back to the installed worker's default. Only a Boolean `true` is accepted. Anything else means fixing the launch configuration and restarting the engine from a newly checked plan.

## Generate and serve the attestation

Generate the engine's attestation document:

```bash
.venv/bin/python tools/deployment/attestation_contract.py generate \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
```

Before starting the sidecar, check the engine's `/health`, `/version`, and `process_start_time_seconds` again, so you know exactly which process you're attesting. Then start it:

```bash
.venv/bin/python tools/deployment/attestation_contract.py serve --run "$ENGINE_RUN"
```

Discovery already put this engine's attestation URL into the router's fleet document. From the router, check `/health` and `/v1/attestation` over the trusted control network.

In a second shell for the same role, check the sidecar and the engine together:

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

Do this for every engine. Once they've all passed, finalize the fleet once, from the router shell:

```bash
.venv/bin/python tools/deployment/attestation_contract.py finalize-fleet --fleet runs/deployment/fleet.json
```

Finalizing reads every engine and sidecar, checks each process identity and full contract, and requires all engines to share the same contract. It then saves the previous fleet document under `runs/` and writes `engine_contract` into `runs/deployment/fleet.json`.

If an engine fails its identity or contract check, inspect the corresponding `engine-attestation.json` and sidecar captures to diagnose the issue before attempting to finalize the fleet. Leave all engines and sidecars running through profiling, preflight, and the trial.
Next: [Gate F: Profile the engines and run the live KV contract](06-Profile-and-Preflight.md).
