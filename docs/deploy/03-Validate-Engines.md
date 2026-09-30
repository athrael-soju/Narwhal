# Gate C: Validate and start every engine

In this gate you check each engine host against its launch record, start vLLM, confirm that the running server is the one you checked, and capture the cache layout it actually allocated. Gate D needs that layout to work out how much bandwidth each link has to carry.

## Inspect every engine host

Open one installed shell per engine host and work through the hosts in parallel. In each shell, `NARWHAL_ENGINE_LAUNCH_CONFIG` points at the launch record transferred for that role.

Print the allocation and launch policy, and save the output in your deployment record:

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

The record's `sources` object tells you which inspection and policy records the allocation and device settings came from.

Next, see what accelerators the host really has:

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

Note the accelerator product and how many GPUs are visible. On ROCm, count the agents whose `Device Type` is `GPU` and take the product name from `Marketing Name`.

On the router, compare what you found with `hardware.accelerator` in `runs/deployment/fleet.json`. For each replica, set `hardware.accelerators_per_engine` to the number of entries in `gpu_ids` and `hardware.tensor_parallel` to `tensor_parallel_size`, and check that every selected index or UUID is actually visible. A host can have more GPUs than its replica uses. Narwhal's TP shape comes from the allocation, not from the physical count.

Before launching, check the image, the checkpoint config, the run directory, the fabric interface, and the current listeners:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

If `NARWHAL_ENGINE_IMAGE` is a registry digest, compare the digest the runtime resolved with the one you configured. The discovery record ties the checkpoint to its full file manifest and tree digest, so if you change the checkpoint on disk you need to run discovery again.

Then check that every declared device path exists:

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

ROCm containers need `/dev/kfd` plus the selected DRI devices. Mapping the whole of `/dev/dri` exposes every DRI device on the host, so check ownership and the container user's permissions. If anything is already listening on a planned HTTP, attestation, NIXL side-channel, or transfer port, find out what owns it before you launch.

## Derive cache-equivalence groups

Engines built from the same inputs should allocate the same cache layout. Grouping them means Gate D only needs one fabric budget per group rather than one per engine. From the management shell, with `config/deployment.env` loaded:

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

The signature is a hash of the seven fields in the script: image ID, model-config hash, accelerator, TP size, GPU visibility setting, the launch record's `runtime` block, and transport. GPU indices, addresses, and device paths aren't included. Engines on different hosts can therefore share a group as long as those seven fields match.

You still start every engine and capture every layout. The grouping only decides which engine's layout stands in for the group when Gate D calculates the budget.

## Prepare, check, and start each engine

The launcher runs

```text
python3 -m vllm.entrypoints.openai.api_server
```

inside `NARWHAL_ENGINE_IMAGE`. It mounts `NARWHAL_MODEL_DIR` read-only at `/model`, exposes the configured devices, and applies the selected TP allocation.

In each engine shell, prepare a launch plan. Engines can go through this at the same time only if their GPU allocations don't overlap.

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

Read through `launch.json`. It should show the image, the full serving command, mounts, device mappings, endpoint, application revision, and source hashes. Don't share `container.env`, since it can contain the engine API key.

The connector settings in the plan use `NixlConnector` with `kv_role=kv_both`, UCX, and `kv_load_failure_policy=fail`. The advertised side-channel address and port come from the role environment, `UCX_TLS` selects TCP or RDMA, and prefix caching is turned off for profiling.

A plan is tied to the inputs it was built from. If you change the launcher itself, prepare a new run so the deployed snapshot and its digest still match the management checkout. Other changes are listed in [What to repeat after a change](../Deploy.md#what-to-repeat-after-a-change).

Now check the image and plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

This starts a temporary container and checks the plan's custom-code requirements, the image identity, and the pinned package versions. It also confirms that `KVConnectorFactory` resolves the connector, that the tokenizer builds with the plan's `--trust-remote-code` setting, that the DS layout is set when required, and that the plan hash matches. Along the way it records `vllm.version.__version__` as `vllm_api_version`, which the HTTP probe below compares against. The container exits when the check finishes.

Each check appends its inspection ID, plan hash, and subprocess output to `image-check.log` (or `runtime-check.log` for a native engine). Repeating a successful check leaves the original `checked.json` in place. If the plan hash has changed, prepare a fresh run rather than re-checking. When a check fails, the log names the package, tokenizer, or identity check that failed, and earlier output is kept.

If the tokenizer lives apart from the model config, set `runtime.extra_args` to `["--tokenizer", "PATH", ...]`. The check uses the last `--tokenizer` in the serving arguments and inspects its custom-code metadata in the selected runtime. For a native engine, paths resolve on the host. For a container, they resolve inside the image and its mounts, so `/model` refers to `NARWHAL_MODEL_DIR`. Without this setting, the tokenizer is loaded from the model directory. If the tokenizer won't load, the check stops before it writes `checked.json`.

Run `ss -ltnp` once more to make sure the planned ports are still free, then start the engine:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

Ctrl-C only stops following the logs. The container keeps running. If startup fails, look at:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Use the container ID recorded in the run, and narrow the failure down to a device, model, memory, library, or transport problem before you build another plan.

If vLLM exits asking for `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, discovery got the checkpoint requirements wrong for this deployment. To fix it:

1. Keep the evidence from the failed attempt.
2. Run discovery again from the corrected approved revision into new output directories.
3. Prepare a new deployment run.
4. Install engine 1 into the new checkout.
5. Validate engine 1 before you update the others.

Then compare the old and new image IDs, model-config hashes, accelerator and TP allocations, runtime settings, and transport, so you know which earlier evidence still holds.

## Check the live HTTP server

Once vLLM is listening, run this from the same shell:

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

If `/version` doesn't match, first find out which container actually owns the endpoint and compare it with the image and container ID you recorded. The probe won't overwrite its own captures, so after a fix, use new filenames or a new launch directory.

Leave every container running until the workload trial is over. While you qualify the fabric and profile the engines, keep all other traffic off them so the numbers describe idle engines.

## Capture actual cache geometry

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" capture-cache --run "$ENGINE_RUN"
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
```

`capture-cache` checks the container identity, the image, the source and plan hashes, and every TP rank, and then writes `cache-layout.json` from the live process. The remaining lines create a working directory for Gate D and verify the budget tool.

Compare the layout and page geometry within each signature group. An engine whose layout or page geometry differs from the rest of its group gets its own budget in Gate D.

## Docker command deadlines

The launcher puts a time limit on every Docker client call, labels each container it creates with a launch token saved in the run, and cleans up daemon resources after a timeout or cancellation. Set the execution and cleanup periods for your environment with the timeout variables listed in the [stage recovery procedure](../dev/05-Stage-Recovery.md). Before reusing a deployment where a Docker command failed, look at any partial output it left and follow that procedure.

Next: [Gate D: Prove the transfer fabric against the serving cache](04-Qualify-Fabric.md).
