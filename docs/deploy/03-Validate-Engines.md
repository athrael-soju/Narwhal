# Gate C: Validate and start every engine

Check each engine host against its launch record, start vLLM, verify the live server, and capture the cache layout. Gate D uses that layout to size the fabric budget per link.

## Inspect every engine host

In parallel, open a shell on each engine host. In each shell, `NARWHAL_ENGINE_LAUNCH_CONFIG` points at the launch record transferred for that role.

Print the allocation and launch policy, and add the output to your deployment record:

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

The record's `sources` object names the inspection and policy records behind the allocation and device settings.

List the accelerators on the host:

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

Record the accelerator product and the number of visible GPUs. On ROCm, count the agents whose `Device Type` is `GPU` and take the product name from `Marketing Name`.

On the router, compare what you found with `hardware.accelerator` in `runs/deployment/fleet.json`. For each replica, set `hardware.accelerators_per_engine` to the number of entries in `gpu_ids` and `hardware.tensor_parallel` to `tensor_parallel_size`, and check that every selected index or UUID is visible. A host can have more GPUs than its replica uses. Narwhal's tensor-parallel (TP) shape comes from the allocation, not from the physical GPU count.

Before launching, check the image, the checkpoint config, the run directory, the fabric interface, and the current listeners:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

If `NARWHAL_ENGINE_IMAGE` is a registry digest, compare the digest the runtime resolved with the one you configured.

The discovery record ties the checkpoint to its full file manifest and tree digest. If you change the checkpoint on disk, run discovery again.

Check that every declared device path exists:

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

ROCm containers need `/dev/kfd` plus the selected DRI devices. Mapping the whole of `/dev/dri` exposes every DRI device on the host, so check ownership and the container user's permissions.

Identify the owner of any process already listening on a planned HTTP, attestation, NIXL side-channel, or transfer port before launch.

## Derive cache-equivalence groups

Engines built from the same inputs allocate the same cache layout, so Gate D needs one fabric budget per group. From the management shell, with `config/deployment.env` loaded:

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

The signature hashes the seven inputs in the script: image ID, model-config hash, accelerator, TP size, GPU visibility setting, the launch record's `runtime` block, and transport. GPU indices, addresses, and device paths are excluded, so engines on different hosts can share a group.

Start every engine and capture every layout. Grouping selects which engine's layout represents the group in Gate D.

## Prepare, check, and start each engine

The launcher runs `python3 -m vllm.entrypoints.openai.api_server` inside `NARWHAL_ENGINE_IMAGE`. It mounts `NARWHAL_MODEL_DIR` read-only at `/model`, exposes the configured devices, and applies the selected TP allocation.

In each engine shell, prepare a launch plan. Engines can be prepared at the same time only if their GPU allocations don't overlap.

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

Review `launch.json`. It lists the image, the full serving command, mounts, device mappings, endpoint, application revision, and source hashes. `container.env` holds the engine API key when one is configured; keep it private.

The connector settings in the plan use `NixlConnector` with `kv_role=kv_both`, UCX, and `kv_load_failure_policy=fail`. The advertised side-channel address and port come from the role environment, `UCX_TLS` selects TCP or RDMA, and prefix caching is turned off for profiling.

A plan is tied to the inputs it was built from. If you change the launcher itself, prepare a new run so the deployed snapshot and its digest still match the management checkout. Other changes are listed in [What to repeat after a change](../Deploy.md#what-to-repeat-after-a-change).

Check the image and plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

This starts a temporary container and checks the plan's custom-code requirements, the image identity, and the pinned package versions. It also confirms that `KVConnectorFactory` resolves the connector, that the tokenizer builds with the plan's `--trust-remote-code` setting, that the DS layout is set when required, and that the plan hash matches. It records `vllm.version.__version__` as `vllm_api_version` for the HTTP probe below.

- Each check appends its inspection ID, plan hash, and subprocess output to `image-check.log` (or `runtime-check.log` for a native engine).
- Repeating a successful check leaves the original `checked.json` in place.
- If the plan hash has changed, prepare a fresh run.
- When a check fails, the log names the failed package, tokenizer, or identity check, and earlier output is kept.

If the tokenizer lives apart from the model config, set `runtime.extra_args` to `["--tokenizer", "PATH", ...]`. The check uses the last `--tokenizer` in the serving arguments and inspects its custom-code metadata in the selected runtime. For a native engine, paths resolve on the host. For a container, they resolve inside the image and its mounts, so `/model` refers to `NARWHAL_MODEL_DIR`. Without this setting, the tokenizer is loaded from the model directory. If the tokenizer won't load, the check stops before it writes `checked.json`.

Run `ss -ltnp` again to confirm the planned ports are free, then start the engine:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

Ctrl-C stops log following; the container keeps running. If startup fails, inspect:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Use the container ID recorded in the run, and classify the failure as a device, model, memory, library, or transport problem before preparing another plan.

If vLLM exits asking for `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, discovery got the checkpoint requirements wrong for this deployment. To fix it:

1. Keep the evidence from the failed attempt.
2. Run discovery again from the corrected approved revision into new output directories.
3. Prepare a new deployment run.
4. Install engine 1 into the new checkout.
5. Validate engine 1 before you update the others.

Compare the old and new signature inputs to find which earlier evidence still applies.

## Check the live HTTP server

When vLLM is listening, run this in the same shell:

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

If `/version` doesn't match, identify which container owns the endpoint and compare it with the recorded image and container ID. The probe opens its captures with exclusive create and won't overwrite them, so after a fix, rerun from a new launch directory or remove the earlier captures.

Keep containers running until the workload trial ends. Send no other traffic to them during fabric qualification and profiling.

## Capture actual cache geometry

Capture the cache layout, then set up the Gate D working directory and verify the budget tool:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" capture-cache --run "$ENGINE_RUN"
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
```

`capture-cache` checks the container identity, the image, the source and plan hashes, and every TP rank, then writes `cache-layout.json` from the live process.

Compare the layout and page geometry within each signature group. An engine whose layout or page geometry differs from the rest of its group gets its own Gate D budget.

## Docker command deadlines

The launcher applies a time limit to every Docker client call, labels each container with a launch token saved in the run, and cleans up daemon resources after a timeout or cancellation. Set the execution and cleanup periods for your environment with the timeout variables listed in the [stage recovery procedure](../dev/05-Stage-Recovery.md). Before reusing a deployment where a Docker command failed, inspect any partial output it left and follow that procedure.

Next: [Gate D: Prove the transfer fabric against the serving cache](04-Qualify-Fabric.md).
