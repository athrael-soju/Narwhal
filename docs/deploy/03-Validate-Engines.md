# Gate C: Validate and start every engine

Gate C checks the accelerators, devices, and listeners on each engine host and starts every engine from its checked launch plan. It proves each live HTTP endpoint and captures each engine's cache layout for Gate D.

## Inspect every engine host

Run the host inspection on every engine host in its installed engine-role shell, where `NARWHAL_ENGINE_LAUNCH_CONFIG` points at the transferred role-specific launch record.

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

Retain this output. The `sources` object in the launch record identifies the records used to derive the allocation and device configuration.

Read the physical accelerator identity and count:

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

Record the accelerator product and the visible physical GPU count. On ROCm, count the agents with `Device Type` set to `GPU` and use the `Marketing Name` for the product.

In the router shell, check the `hardware` fields in `runs/deployment/fleet.json` against what you observed on the hosts. `hardware.accelerator` must match the accelerator product you found, `hardware.accelerators_per_engine` must equal the number of `gpu_ids` in each replica's launch record, and `hardware.tensor_parallel` must equal each replica's `tensor_parallel_size`. Every selected index or UUID must be visible on its engine host. The replica allocation defines the tensor parallel (TP) shape Narwhal uses.

Verify the local artifacts and planned listeners before launch:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

A passing `test` command exits with status 0 and no output. Free any planned HTTP, attestation, NIXL side-channel, or transfer port that `ss -ltnp` shows in use. When `NARWHAL_ENGINE_IMAGE` holds a registry digest, compare the runtime's resolved digest against the configured one before launch.

The private discovery record binds the checkpoint to its complete manifest and matched tree digest. Rerun discovery after changing the provisioned checkpoint.

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

ROCm containers require `/dev/kfd` plus the selected DRI devices. Mapping `/dev/dri` exposes every DRI device on the host. Verify the device files' user, group, and container-user permissions.

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

The signature groups engines by image ID, model-config hash, accelerator product, TP size, GPU visibility policy, runtime packages, cache policy, model arguments, image environment, and transport. Gate D calculates the fabric budget from one representative cache layout per group. Start every engine and capture its live cache layout. Engines in the same group may use different GPU indices, addresses, and device paths when their accelerator product and TP shape match.

## Prepare, check, and start each engine

The launcher runs `python3 -m vllm.entrypoints.openai.api_server` inside `NARWHAL_ENGINE_IMAGE`. It mounts `NARWHAL_MODEL_DIR` read-only at `/model` and exposes the configured devices with the selected TP allocation. It applies the `NixlConnector` policy with `kv_role=kv_both`, UCX, and `kv_load_failure_policy=fail`. The side-channel address and port come from the role environment. The transport is TCP or RDMA through `UCX_TLS`. Prefix caching stays on by default, and `runtime.extra_args` can turn it off. While caching is on, vLLM publishes cache events over private IPC sockets under `/tmp/narwhal-<uid>/`. The [prefix caching and cache events](../configuration/05-Engine-Launch.md#161-prefix-caching-and-cache-events) section lists the opt-out settings.

For the Gate G [capacity trial](../measure/03-Load-Trial.md), add vLLM's `--no-enable-prefix-caching` to `runtime.extra_args` before you prepare the launch plans. Discovery writes `runtime.extra_args` from `NARWHAL_ENGINE_ARGS` ([Gate A](01-Discover.md#confirm-the-launch-policy)).

If you add the flag after launch, derive the cache-equivalence groups again, prepare a fresh launch directory, restart each engine, and repeat at least the engine-restart work in the [repeat-work table](../Deploy.md#deployment-sequence).

For a separate tokenizer, set `runtime.extra_args` to `["--tokenizer", "PATH", ...]` before you prepare the launch plan, replacing `PATH` with the tokenizer path. If you change the image, model, dtype, cache policy, TP allocation, or model arguments, derive the groups and capture the live cache layout again. If you change the workload assumptions, recalculate the fabric budget.

In every engine-role shell, prepare the launch plan:

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

The launcher prints `Prepared <role>; review launch.json and run the image check.` Inspect `launch.json` for the immutable image, the complete serving command, the mounts, device mappings, endpoint, application revision, and source hashes. Keep `container.env` private because it can contain the engine API key.

Validate the image and plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

With prefix caching off, a passing check confirms the runtime identity, package pins, connector import, and tokenizer, and shows that the engine publishes no cache events. The check also verifies the custom-code requirements, the immutable image identity, and the pinned package versions in a temporary container. It resolves the connector through `KVConnectorFactory` and constructs the tokenizer with the plan's `--trust-remote-code` setting. It confirms the DS convolutional-state layout when required and the plan hash, and records `vllm.version.__version__` as `vllm_api_version`. The temporary container exits after inspection.

Each check appends its unique attempt identifier, the launch-plan hash, and the subprocess output to `image-check.log` for containers or `runtime-check.log` for native engines. A repeated pass keeps the first `checked.json`, and a failure reports the failing package, tokenizer, or identity check while retaining the earlier output. If the launch-plan hash changes, prepare a fresh launch directory.

The tokenizer check loads the final `--tokenizer` selection from the serving arguments, defaulting to the model directory, and inspects its custom-code metadata in the selected runtime. Native paths resolve on the host, and container paths resolve inside the image and its mounts, including `/model` for `NARWHAL_MODEL_DIR`. If the launcher itself changes, prepare a new deployment run in [Gate B](02-Install.md) so the deployed snapshot and digest match the management checkout.

Engines with disjoint GPU allocations can start concurrently. Recheck the planned ports with `ss -ltnp`, then start each engine and follow its log:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

The launcher reports that the container has started. Watch the logs until the HTTP endpoints come up. Ctrl+C stops the log follower, and the serving container keeps running. If startup fails, inspect the container named by `container.id`:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Find the cause of the failure before creating another plan.

If vLLM exits requesting `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, work through the recovery in this order:

1. Retain the failed attempt's evidence.
2. Rerun discovery from the corrected approved revision into fresh outputs.
3. Prepare a new deployment run.
4. Install engine 1 into the new checkout.
5. Validate engine 1 before updating the rest.

Then compare the old and new image IDs and model-config hashes, and the old and new accelerator and TP allocations, runtime settings, and transport.

### Docker command deadlines

The engine deployment wrapper bounds each Docker client with the stage budgets, labels the containers it creates with a persisted launch token, and reconciles the daemon resources after a timeout or cancellation. The variables defined in [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery) and [Docker command reconciliation](../dev/Recovery-and-Qualification.md#docker-command-reconciliation) set the execution and cleanup periods. Before you reuse a failed deployment, inspect the retained partial output and follow the [stage recovery procedure](../Dev-Runtime.md#stage-deadlines-and-recovery).

## Prove the live HTTP process

Wait for the engine's `/health` endpoint to return HTTP 200, then run the probe from the same engine-role shell:

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

The probe binds the running endpoint to the checked image by verifying `/health`, the exact `/version`, the configured model, `process_start_time_seconds`, and one deterministic completion. If it reports a different API version, compare the container that serves the endpoint with the recorded image and container ID. The probe writes each response capture once. After a repair, rerun it with new filenames or in a new launch directory.

Leave each serving container running through the workload trial. Keep the engines free of other traffic during fabric qualification and profiling so the measurements reflect these processes at idle load.

## Capture the live cache layout

In each engine-role shell, capture the cache layout:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" capture-cache --run "$ENGINE_RUN"
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
```

`capture-cache` verifies the container identity, the image, source and plan hashes, and every TP rank, then writes `cache-layout.json` from the live process. Compare the resolved layouts and page geometry within each signature group. An engine whose layout or page geometry differs gets its own Gate D budget.

Continue with [Gate D: Prove the transfer fabric against the serving cache](04-Qualify-Fabric.md).
