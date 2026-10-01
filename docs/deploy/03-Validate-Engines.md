---
description: Validate and start every vLLM engine in a Narwhal fleet.
---

# Gate C: Validate and start every engine

## Inspect every engine host

Run these checks in the installed engine-role shell on every engine host.

`NARWHAL_ENGINE_LAUNCH_CONFIG` selects the role's launch record.

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

Retain this output.

The launch record's `sources` object names the records behind the allocation and device configuration.

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

Record the accelerator product and the visible physical GPU count.

On ROCm, read both from `rocminfo`:

| Value     | `rocminfo` source                      |
| --------- | -------------------------------------- |
| GPU count | Agents with `Device Type` set to `GPU` |
| Product   | `Marketing Name`                       |

In the router shell, check the `hardware` fields in `runs/deployment/fleet.json` against the host observations:

| Field                              | Must equal                                              |
| ---------------------------------- | ------------------------------------------------------- |
| `hardware.accelerator`             | The observed accelerator product                        |
| `hardware.accelerators_per_engine` | The number of `gpu_ids` in each replica's launch record |
| `hardware.tensor_parallel`         | Each replica's `tensor_parallel_size`                   |

Every selected index or UUID must be visible on its engine host.

Verify the local artifacts and planned listeners before launch:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

A passing `test` command is silent and exits with status 0.

| Finding                                                                                  | Action                                                            |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `ss -ltnp` shows a planned HTTP, attestation, NIXL side-channel, or transfer port in use | Free the port.                                                    |
| `NARWHAL_ENGINE_IMAGE` holds a registry digest                                           | Compare the runtime's resolved digest with the configured digest. |
| The provisioned checkpoint changed                                                       | Rerun discovery.                                                  |

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

For ROCm containers:

- The container requires `/dev/kfd` and the selected DRI devices.
- A `/dev/dri` mapping exposes every DRI device on the host.
- Verify the user, group, and container-user permissions on each device file.

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

Each output line names a signature group, its representative role, and its member roles.

## Prepare, check, and start each engine

Launch plan properties:

| Property                      | Value                                                                                                                       |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Serving command               | `python3 -m vllm.entrypoints.openai.api_server` inside `NARWHAL_ENGINE_IMAGE`                                               |
| Model mount                   | `NARWHAL_MODEL_DIR`, read-only at `/model`                                                                                  |
| Devices                       | The configured devices with the selected TP allocation                                                                      |
| KV connector                  | `NixlConnector` with `kv_role=kv_both`, the UCX backend, `enforce_handshake_compat=true`, and `kv_load_failure_policy=fail` |
| Side-channel address and port | The role environment                                                                                                        |
| Transport                     | `UCX_TLS` of `tcp,sm,self,<gpu>` for `ucx_tcp` or `rc,sm,self,<gpu>` for `ucx_rdma`                                         |
| Prefix caching                | On by default, turned off through `runtime.extra_args`                                                                      |
| Cache events                  | Published by vLLM over private IPC sockets under `/tmp/narwhal-<uid>/` while prefix caching is on                           |
| Cache event opt-out           | [Prefix caching and cache events](../configuration/05-Engine-Launch.md#161-prefix-caching-and-cache-events)                 |

Prefix caching for the Gate G [capacity trial](../measure/03-Load-Trial.md):

| Item     | Value                                                                                                           |
| -------- | --------------------------------------------------------------------------------------------------------------- |
| Flag     | vLLM's `--no-enable-prefix-caching` in `runtime.extra_args`                                                     |
| Input    | [`NARWHAL_ENGINE_ARGS`](01-Discover.md#confirm-the-launch-policy), written to `runtime.extra_args` by discovery |
| Deadline | Before preparing the launch plans                                                                               |

To add the flag after launch:

1. Derive the cache-equivalence groups again.
2. Prepare a fresh launch directory.
3. Restart each engine.
4. Repeat the engine-restart work in the [repeat-work table](../Deploy.md#deployment-sequence).

For a separate tokenizer at `PATH`, set `runtime.extra_args` to `["--tokenizer", "PATH", ...]` before preparing the launch plan.

When a launch input changes, repeat the matching work:

| Change                                                               | Work to repeat                                             |
| -------------------------------------------------------------------- | ---------------------------------------------------------- |
| Image, model, dtype, cache policy, TP allocation, or model arguments | 1. Derive the groups.<br>2. Capture the live cache layout. |
| Workload assumptions                                                 | Recalculate the fabric budget.                             |

In every engine-role shell, prepare the launch plan:

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

The launcher prints `Prepared <role>; review launch.json and run the image check.`

Inspect `launch.json` for:

- the immutable image
- the complete serving command
- the mounts and device mappings
- the endpoint
- the application revision
- the source hashes

Keep `container.env` private.

Validate the image and plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

A passing check confirms:

- the immutable image identity and runtime identity
- the pinned package versions
- the custom-code requirements
- the connector, resolved through `KVConnectorFactory`
- the tokenizer and its custom-code metadata, loaded with the plan's `--trust-remote-code` setting
- the DS convolutional-state layout, when required
- the plan hash
- the resolved prefix-caching setting and cache-event endpoints, matched against the plan

The check writes `checked.json`:

| Field              | Value                                         |
| ------------------ | --------------------------------------------- |
| `plan_sha256`      | SHA-256 of `launch.json`                      |
| `vllm_api_version` | `vllm.version.__version__` from the image     |
| `prefix_caching`   | The resolved prefix-caching setting           |
| `kv_events`        | The resolved cache-event endpoints, or `null` |
| `ucx_version`      | The UCX version that NIXL loads in the image, or `null` |
| `peer_release`     | `true` when the engine releases a stopped peer's KV memory |
| `image_id`         | The local image ID                            |

A failed check names the failing package, tokenizer, or identity check.

The checked tokenizer is the final `--tokenizer` value in the serving arguments, or the model directory by default.

| Engine    | Check log           | Tokenizer path resolves in                                           |
| --------- | ------------------- | -------------------------------------------------------------------- |
| Container | `image-check.log`   | The image and its mounts, including `/model` for `NARWHAL_MODEL_DIR` |
| Native    | `runtime-check.log` | The host                                                             |

Each check appends its attempt identifier, the launch-plan hash, and the subprocess output to the check log.

When the launch-plan hash or the launcher changes, take the matching action:

| Change           | Action                                                   |
| ---------------- | -------------------------------------------------------- |
| Launch-plan hash | Prepare a fresh launch directory.                        |
| Launcher         | Prepare a new deployment run in [Gate B](02-Install.md). |

Start the engines:

1. Recheck the planned ports with `ss -ltnp`.
2. Start each engine and follow its log:

    ```bash
    python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
    export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
    docker logs --follow "$ENGINE_CONTAINER"
    ```

3. Watch the log until the HTTP endpoints come up.
4. Stop the log follower with Ctrl+C.

When startup fails, inspect the container named by `container.id`:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Find the cause of the failure before creating another plan.

When vLLM exits requesting `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, recover in this order:

1. Retain the failed attempt's evidence.
2. Rerun discovery from the corrected approved revision into fresh outputs.
3. Prepare a new deployment run.
4. Install engine 1 into the new checkout.
5. Validate engine 1 before updating the rest.
6. Compare the old and new image IDs, model-config hashes, accelerator and TP allocations, runtime settings, and transport.

### Docker command deadlines

| Period                                  | Settings                                                                                            |
| --------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Docker command execution                | Stage budgets in [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery)     |
| Cleanup after a timeout or cancellation | [Docker command reconciliation](../dev/Recovery-and-Qualification.md#docker-command-reconciliation) |

Before you reuse a failed deployment:

1. Inspect the retained partial output.
2. Follow the [stage recovery procedure](../Dev-Runtime.md#stage-deadlines-and-recovery).

## Prove the live HTTP process

1. Wait for the engine's `/health` endpoint to return HTTP 200.
2. Run the probe from the same engine-role shell:

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

| Situation                             | Action                                                                                   |
| ------------------------------------- | ---------------------------------------------------------------------------------------- |
| The probe reports another API version | Compare the container that serves the endpoint with the recorded image and container ID. |
| Rerun after a repair                  | Use new capture filenames or a new launch directory.                                     |

- Leave each serving container running through the workload trial.
- Keep the engines idle during fabric qualification and profiling.

## Capture the live cache layout

In each engine-role shell, capture the cache layout and create the fabric run directory:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" capture-cache --run "$ENGINE_RUN"
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
```

`capture-cache` writes `cache-layout.json` with one record per TP rank.

Compare the resolved layouts and page geometry within each signature group:

| Result                            | Gate D budget                     |
| --------------------------------- | --------------------------------- |
| Every engine in the group matches | One budget for the group          |
| An engine differs                 | A separate budget for that engine |

[![Next: Gate D: Prove the transfer fabric against the serving cache](https://img.shields.io/badge/next-Gate%20D%3A%20Prove%20the%20transfer%20fabric%20against%20the%20serving%20cache-0f766e)](04-Qualify-Fabric.md)
