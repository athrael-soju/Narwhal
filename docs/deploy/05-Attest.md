---
description: Generate and serve attestation documents for the live vLLM, SGLang or TensorRT-LLM engines of a Narwhal fleet.
---

# Attest live engines

## Checking the router inventory

On the router, confirm that `runs/deployment/fleet.json` lists:

- Each running engine with URL and attestation URL references that resolve in `.env.router`.
- The model.
- The initial roles.
- The SLO values.
- The profile path.

Each engine's `ENGINE_RUN` directory supplies:

- `container.id` and the container logs.
- `cache-layout.json`, for a vLLM engine.

## Capturing the attestation inputs

Run these steps in each engine's role shell. For an SGLang or TensorRT-LLM engine, run only [Saving the startup log](#saving-the-startup-log), [Capturing the connector version](#capturing-the-connector-version) and [Capturing the model dimensions](#capturing-the-model-dimensions), then [generate the document](#generating-the-document).

### Saving the startup log

```bash
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
export ENGINE_STARTUP_LOG="$ENGINE_RUN/startup.log"
(set -o noclobber; docker logs "$ENGINE_CONTAINER" > "$ENGINE_STARTUP_LOG" 2>&1)
```

The sidecar reads the startup log from `$ENGINE_RUN/startup.log`.

### Capturing the connector version

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli capture-connector --run "$ENGINE_RUN"
```

For a vLLM engine, `contract.connector_version` comes from the installed connector's `NIXL_CONNECTOR_VERSION`. `transfer_version` records the version of `nixl` in `runtime.expected_packages`, or of `nixl-rocm` when `nixl` is not listed.

For an SGLang engine, the capture writes fields from `/server_info` and the process start time to `server-info.json`. `/server_info` supplies `engine_version`, `model_dtype`, `attention_backend`, `kv_cache_dtype` and `speculative_config`. `transfer_version` records the version of `mooncake-transfer-engine` in `runtime.expected_packages`, or of the one CUDA build listed there, such as `mooncake-transfer-engine-cuda13`.

For a TensorRT-LLM engine, the capture writes `version` from `/version`, `tokens_per_block`, `max_batch_size` and `kv_cache_hash_algo` from `/server_info`, and the process start time to `server-info.json`. Generation fails when the launch's `engine.json` sets a block size other than the live `tokens_per_block`, when the live `max_batch_size` differs from `--max_batch_size`, or when `engine.json` names a transceiver backend other than the planned connector's. `model_dtype`, `kv_cache_dtype`, `connector` and `speculative_config` come from the launch. `transfer_version` records the `tensorrt_llm` version in `runtime.expected_packages`.

Generation fails when an SGLang engine's live `decode_attention_backend` in `/server_info` differs from its `--decode-attention-backend` launch argument, or is set when the launch passes none.

Generation fails when an SGLang engine's startup log reports a `max_running_requests` below its `--max-running-requests`. Lower `runtime.max_sequences` to the logged value, or raise the limit the log names, such as `--max-mamba-cache-size`, and then relaunch the engine.

### Capturing the model dimensions

```bash
umask 077
.venv/bin/python -m narwhal.deployment.attestation_contract.cli capture-model-dimensions --run "$ENGINE_RUN"
cat "$ENGINE_RUN/model-dimensions.live.json"
```

For a vLLM engine, the capture reads the dimensions from the live engine into `model-dimensions.live.json`, and requires the live container's plan and launcher hashes to match `launch.json`.

| Dimension            | Source                                                                                                         |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| `head_size`          | `ModelConfig.get_head_size()`, or `kv_lora_rank + qk_rope_head_dim` on a DeepSeek-style model with MLA enabled |
| `kv_heads`           | `get_total_num_kv_heads()`                                                                                     |
| `hidden_layers`      | `get_total_num_hidden_layers()`                                                                                |
| `model_architecture` | Resolved architecture                                                                                          |

For an SGLang or TensorRT-LLM engine, the capture reads the dimensions and the first listed architecture from the checkpoint's `config.json`, or from its `text_config`, into `model-dimensions.json`, the file to read in place of `model-dimensions.live.json`. `config.json` must match `model_config_sha256` in `launch.json`.

### Capturing the cache grouping

`cache-registration` writes one `cache-registration.json` per `ENGINE_RUN` from either of two sources. The startup log must name exactly one layout across its `Using <layout> KV cache layout.` lines. The `cache-layout.json` file must hold one resolved layout across every TP rank.

From the startup log:

```bash
narwhal-engine cache-registration \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$ENGINE_RUN/cache-registration.json"
```

From the resolved layout in `cache-layout.json`:

```bash
narwhal-engine cache-registration \
  --run "$ENGINE_RUN" --runtime-layout "$ENGINE_RUN/cache-layout.json"
```

`cache-registration.json` records the engine's `kv_cache_layout`, and that layout's `is_block_outermost` value as `cross_layers_blocks`.

### Comparing the layout with the representative's

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

When the group signature, resolved layout, and page geometry all match the representative's, the engine inherits the representative's [fabric budget](04-Qualify-Fabric.md#building-the-source-budget). When the layout or page geometry differs, the engine needs a separate serving capture, budget, and edge comparisons.

### Capturing the transfer direction

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

In the pinned API, `NixlConnector` is an alias for `NixlPullConnector`, and `kv_both` means the engine produces and consumes KV.

When the script finds zero or several connector classes in `image-check.log`, rerun the launcher's `check` for this launch plan.

### Confirming the connector in the startup log

Confirm that the serving startup log names the connector class recorded in `transfer-mode.json`.

### Capturing handshake enforcement

```bash
narwhal-engine handshake-policy --run "$ENGINE_RUN"
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

## Generating and serving the attestation

### Generating the document

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli generate \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
```

For a GGUF model, the document's `launch` records the file's `model_sha256` from `launch.json`. Generation fails when the file no longer matches it, has become a symbolic link, or has left `NARWHAL_MODEL_DIR`.

### Confirming the attested process

1. Recheck the engine's `/health` and version: `/version` and `process_start_time_seconds` in `/metrics` on vLLM, `version` in `/server_info` on SGLang, or `/version` on TensorRT-LLM.
2. Confirm the document describes the running process.

### Starting the sidecar

Run it as the engine's user, or as root for container engines.

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli serve --run "$ENGINE_RUN"
```

When `checked.json` shows prefix caching on and cache events published, the sidecar serves the [residency routes](../cli/Attest.md#residency).

### Checking the sidecar from the router

From the router, request the sidecar's `/health` and `/v1/attestation` over the trusted control network.

### Verifying the sidecar against the engine

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
from narwhal.backends import load as load_backend
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
options = {}
backend = load_backend(plan.get("engine") or "vllm")
if not backend.identity.reports_process_start:
    # SGLang and TensorRT-LLM publish no process start; the sidecar reads it on the host.
    options = {"reader": backend.identity, "attestation_url": base + "/v1/attestation"}
identity = asyncio.run(fetch_engine_identity(plan["endpoint"], headers=headers, **options))
(out / "identity.json").write_text(json.dumps(asdict(identity), indent=2) + "\n")
document = AttestationDocument.load(os.environ["ATTEST_DOCUMENT"])
failures = verify_attestation(responses["attestation"].json(), document.contract, identity)
if failures:
    raise SystemExit("; ".join(failures))
print("Both sidecar endpoints passed; attestation matches the live engine and contract")
PY_ATTEST_CHECK
```

### Repeating for every engine

In each engine's role shell, with that engine's `ENGINE_RUN`, run these sections in order:

1. [Saving the startup log](#saving-the-startup-log)
2. [Capturing the connector version](#capturing-the-connector-version)
3. [Capturing the model dimensions](#capturing-the-model-dimensions)
4. On vLLM only: [Capturing the cache grouping](#capturing-the-cache-grouping), [Comparing the layout with the representative's](#comparing-the-layout-with-the-representatives), [Capturing the transfer direction](#capturing-the-transfer-direction), [Confirming the connector in the startup log](#confirming-the-connector-in-the-startup-log) and [Capturing handshake enforcement](#capturing-handshake-enforcement)
5. [Generating the document](#generating-the-document)
6. [Confirming the attested process](#confirming-the-attested-process)
7. [Starting the sidecar](#starting-the-sidecar)
8. [Checking the sidecar from the router](#checking-the-sidecar-from-the-router)
9. [Verifying the sidecar against the engine](#verifying-the-sidecar-against-the-engine)

When every engine has passed step 9, run [finalizing the fleet contract](#finalizing-the-fleet-contract) once.

### Finalizing the fleet contract

Run once from the router shell after every sidecar passes:

```bash
.venv/bin/python -m narwhal.deployment.attestation_contract.cli finalize-fleet --fleet runs/deployment/fleet.json
```

`finalize-fleet` requires:

- Each sidecar's attestation matches its engine's live identity.
- Every engine carries the same complete contract.
- Every engine attests the same model files: the same `model_config_sha256`, `model_tree_sha256` and `model_sha256` in its `launch` evidence.

`finalize-fleet` writes:

- `engine_contract` in `runs/deployment/fleet.json`.
- A copy of the previous fleet file at `runs/fleet.before-attestation-<id>.json`.

On success it prints `Router fleet contract verified across live sidecars: <fingerprint>`.

If a sidecar fails, the error output names the engine and the failed checks. If the output reports `Engine sidecars report different contracts`, compare the `contract` objects in each engine's `engine-attestation.json`. If it reports `Engine sidecars attest different model files`, it names the differing field and the engines that attest each value. Start those engines from the same checkpoint.

Recovery:

1. Fix the failing engine.
2. Restart its sidecar against the checked process.
3. Run `finalize-fleet` again.

Leave every engine and sidecar running through profiling, preflight, and the trial.

[Profile engines and run preflight](06-Profile-and-Preflight.md)
