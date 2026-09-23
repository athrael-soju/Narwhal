# Gate C: Prove each host and one engine per cache class

This gate establishes host truth, launch truth, live API identity, and the runtime cache geometry needed for fabric budgeting.

## Inspect every engine host

Run host inspection concurrently in installed engine-role shells. `NARWHAL_ENGINE_LAUNCH_CONFIG` points to the transferred role-specific launch record.

Print the allocation and launch policy:

```bash
python3 - <<'PY_LAUNCH'
import json
import os
from pathlib import Path
record = json.loads(Path(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]).read_text())
for field in ("role", "accelerator", "gpu_ids", "tensor_parallel_size",
              "accelerator_devices", "network_mode", "transfer", "environment", "vllm_args"):
    print(f"{field}: {json.dumps(record[field])}")
PY_LAUNCH
```

Retain this output. The `sources` object identifies the records used to derive allocation and device configuration.

Inspect physical accelerators without changing host state:

```bash
(
  set -e
  vendors=
  for device in /sys/bus/pci/devices/*; do
    test -r "$device/class" && test -r "$device/vendor" || continue
    case "$(cat "$device/class")" in
      0x03*|0x12*) ;;
      *) continue ;;
    esac
    case "$(cat "$device/vendor")" in
      0x10de) vendors="$vendors nvidia" ;;
      0x1002) vendors="$vendors amd" ;;
    esac
  done
  if test -z "$vendors"; then
    printf '%s\n' 'Inspect GPU PCI device exposure on this engine host.' >&2
    exit 1
  fi
  case "$vendors" in
    *nvidia*) nvidia-smi --query-gpu=pci.bus_id,name,uuid --format=csv ;;
  esac
  case "$vendors" in
    *amd*) rocminfo ;;
  esac
)
```

Record accelerator product and visible physical GPU count. On ROCm, count only agents whose `Device Type` is `GPU`; use `Marketing Name` for the product.

On the router, compare observations with `hardware.accelerator` in `runs/deployment/fleet.json`. For each replica, set `hardware.accelerators_per_engine` to the number of `gpu_ids`, set `hardware.tensor_parallel` to `tensor_parallel_size`, and confirm every selected index or UUID is visible. Physical GPU count describes available hardware; replica allocation defines the TP shape Narwhal actually uses.

Before any engine process exists, verify local artifacts and listeners:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

If `NARWHAL_ENGINE_IMAGE` is a registry digest rather than an image ID, compare the runtime's resolved digest instead. The complete checkpoint manifest and matched tree digest remain in the private discovery record; repeat discovery if provisioned checkpoint contents change.

Check every declared device path:

```bash
python3 - <<'PY_DEVICES'
import json
import os
from pathlib import Path
record = json.loads(Path(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]).read_text())
paths = record["accelerator_devices"] + record["transfer"]["devices"]
missing = [path for path in paths if not Path(path).exists()]
for path in paths:
    print(f"{path}: {'missing' if path in missing else 'present'}")
if missing:
    raise SystemExit("Restore the declared device exposure before launch.")
PY_DEVICES
```

ROCm containers require `/dev/kfd` plus the selected DRI devices. Mapping `/dev/dri` exposes all DRI devices on the host, so verify ownership and container-user permissions. Any existing listener on planned HTTP, attestation, NIXL side-channel, or transfer ports blocks launch until ownership is resolved.

## Derive cache-equivalence groups

From the management shell with `config/deployment.env` loaded:

```bash
python3 - <<'PY_CACHE_GROUPS'
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

launches = json.loads(Path(os.environ["NARWHAL_LAUNCH_CONFIG"]).read_text())["engines"]
groups = defaultdict(list)
for role, launch in sorted(launches.items()):
    observed = json.loads(Path(launch["sources"]["runtime"]).read_text())
    inputs = {
        "image_id": observed["image_id"],
        "model_config_sha256": observed["model_sha256"],
        "accelerator": launch["accelerator"],
        "tensor_parallel_size": launch["tensor_parallel_size"],
        "gpu_visibility_env": launch["gpu_visibility_env"],
        "runtime": launch["runtime"],
        "transport": launch["transfer"]["transport"],
    }
    signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    groups[signature].append(role)
for signature, roles in sorted(groups.items()):
    print(f"{signature}: representative={roles[0]}; roles={','.join(roles)}")
PY_CACHE_GROUPS
```

The signature covers immutable image ID, model-config hash, accelerator product, TP size, GPU visibility environment, pinned runtime packages, cache policy, model arguments, image environment, and transfer transport. GPU indices, addresses, and device paths may differ if accelerator product and TP shape match. Every distinct signature needs one serving representative and one workload budget.

## Prepare, check, and start each representative

The launcher runs:

```text
python3 -m vllm.entrypoints.openai.api_server
```

inside `NARWHAL_ENGINE_IMAGE`, mounting `NARWHAL_MODEL_DIR` read-only at `/model`, exposing configured devices, and applying the selected TP allocation.

On engine 1 and on each additional cache-group representative:

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

Inspect `launch.json`. It records immutable image, complete serving command, mounts, device mappings, endpoint, application revision, and source hashes. `container.env` may contain the engine API key and must remain private.

The generated connector policy uses `NixlConnector`, `kv_role=kv_both`, UCX, and `kv_load_failure_policy=fail`. The launcher derives advertised side-channel address and port from the role environment, configures TCP or RDMA via `UCX_TLS`, and disables prefix caching for profiling.

Changing image, model, dtype, cache policy, TP allocation, or model arguments requires regrouping and a new serving-layout capture. Changing workload assumptions requires a recalculated fabric budget.

Validate the image and plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

The check verifies custom-code requirements, immutable image identity, pinned package versions in a temporary container, connector resolution through `KVConnectorFactory`, tokenizer construction with the plan's `--trust-remote-code` setting, DS convolutional-state layout when required, the plan hash, and `vllm.version.__version__` as `vllm_api_version`. `image-check.log` retains the evidence. The temporary container exits after inspection.

A changed launcher requires a new prepared run so deployed snapshot and digest match the management checkout.

Before start, inspect planned ports with `ss -ltnp`, then:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

Ctrl-C stops only the log follower. If startup fails:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Use the recorded container ID. Root-cause the failure to a device, model, memory, library, or transport input before creating another plan.

If vLLM exits requesting `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, retain the failed evidence, rerun discovery from the corrected approved revision into fresh outputs, prepare a new deployment run, install engine 1 into the new checkout, and validate it before updating the rest. Compare old and new image IDs, model-config hashes, accelerator/TP allocations, runtime settings, and transport. A changed hardware, model, image, cache policy, route, or transport invalidates evidence for that gate.

## Prove the live HTTP process

Once vLLM listens, run from the same role shell:

```bash
python3 - <<'PY_ENGINE'
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
run = Path(os.environ["ENGINE_RUN"])
plan_data = (run / "launch.json").read_bytes()
plan = json.loads(plan_data)
checked = json.loads((run / "checked.json").read_text())
assert checked["plan_sha256"] == hashlib.sha256(plan_data).hexdigest(), "Repeat the image check for this plan"
expected_version = checked["vllm_api_version"]
headers = {"Content-Type": "application/json"}
if os.environ.get("NARWHAL_ENGINE_API_KEY"):
    headers["Authorization"] = "Bearer " + os.environ["NARWHAL_ENGINE_API_KEY"]
def probe(path, filename, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(plan["endpoint"].rstrip("/") + path, data=data, headers=headers)
    with urlopen(request, timeout=60) as response:
        content = response.read()
    with (run / filename).open("xb") as output:
        output.write(content)
    return content
probe("/health", "health.txt")
version = json.loads(probe("/version", "version.json"))
assert version["version"] == expected_version, (
    f"/version returned {version['version']!r}; checked image expects {expected_version!r}"
)
models = json.loads(probe("/v1/models", "models.json"))
assert os.environ["NARWHAL_ENGINE_MODEL_NAME"] in {m["id"] for m in models["data"]}
metrics = probe("/metrics", "metrics.txt").decode()
assert any(line.startswith("process_start_time_seconds ") for line in metrics.splitlines())
completion = json.loads(probe("/v1/completions", "completion.json", {
    "model": os.environ["NARWHAL_ENGINE_MODEL_NAME"], "prompt": "The sea is",
    "max_tokens": 32, "temperature": 0,
}))
assert completion["choices"][0]["text"]
print("Engine health, version, model, process identity and completion passed.")
PY_ENGINE
```

The probe binds the running endpoint to the checked image by verifying `/health`, exact `/version`, configured model, `process_start_time_seconds`, and one deterministic completion. Treat API-version mismatch first as endpoint ownership; compare the endpoint with the recorded image and container ID. Response captures are immutable, so use new filenames or a new launch directory after repair.

Keep representative containers running through fabric qualification, attestation, profiling, preflight, and the workload trial.

## Capture actual cache geometry

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" capture-cache --run "$ENGINE_RUN"
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
```

`capture-cache` checks running container identity, image, source and plan hashes, and every TP rank before writing `cache-layout.json`. The serving process remains live.

Continue with [Gate D: Prove the transfer fabric against the serving cache](04-Qualify-Fabric.md).
