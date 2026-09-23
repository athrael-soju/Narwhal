# Deploy a fleet

Run deployment from the management workstation. Generate host-specific configuration from the private `.env` and live host inspection, install the approved source revision, validate each engine, start one serving representative per cache group, qualify the transfer fabric, launch and attest the remaining engines, profile the fleet, run preflight, then exercise the router under load through the private SSH path. Prometheus and Grafana remain on the router host for the initial trial.

## Hosts and inputs

| Host                                           | Work                                                                                                                                                               | Required inputs                                                                                                                                                                                                                   |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Management workstation and initial load client | Generate private configuration, manage remote access, open role shells, create the deployment package, send trial traffic through SSH, retain deployment evidence. | Private `.env` with management destinations and credentials, approved source revision, model and image selection, model paths, fabric interface, run directory and service ports. Step 1 derives the remaining configuration. |
| Router and observability host                  | Install Narwhal, maintain the fleet configuration, profile and check engines, run the router, Prometheus and Grafana.                                              | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO settings, Docker Engine and Compose plugin.                                                                                       |
| Engine hosts                                   | Inspect accelerators and artifacts, qualify fabric paths, run vLLM and attestation sidecars.                                                                       | Accelerator allocation, TP shape, engine image, checkpoint, launch policy, fabric addresses and ports.                                                                                                                            |

The management workstation requires Git, Bash, Python 3.11 or newer and the supplied access tooling. Any remote host that runs Narwhal commands needs Python 3.11 or newer with `venv`, Git, Make and curl. Engine hosts also need the configured accelerator driver, container runtime and transfer devices.

Inventory roles describe deployment responsibilities. Hardware discovered on the management workstation applies only to that workstation.

A Narwhal fleet serves one model with a compatible KV layout across vLLM engines using NIXL and effective `kv_both` behaviour. Every eligible KV producer must be able to transfer to every eligible consumer. One controller is active for the fleet.

Parallelise independent host work. After installation, keep one shell open per physical engine host and run step 3 concurrently. In step 4, start one serving representative for each distinct cache configuration, capture its resolved page geometry and retain the running process. Fabric qualification remains serial per directed edge so concurrent tests cannot consume the same endpoint or link capacity. Start and attest the remaining engines concurrently where their device allocations do not overlap.

Create a private deployment record before running the first command. For each gate, record:

- host or stable host alias;
- full source revision;
- starting state;
- commands executed;
- exit status;
- generated artifacts and their paths.

Record a blocked gate before changing the failed state. Cleanup should touch only processes and files created by the current test. The recovery table at the end of this guide lists the evidence and inputs associated with each stage.

## 1. Bootstrap management access

### Load the private environment

Use a fresh management checkout and the supplied `.env`. [.env.example](https://github.com/athrael-soju/Narwhal/blob/main/.env.example) documents its fields.

The environment identifies management destinations and credentials, approved source revision, engine image, model paths, fabric interface, run directory and service ports. Keep credentials on the workstation. Disable shell tracing before loading them:

```bash
set +x
set -a
. ./.env
set +a
```

Define `NARWHAL_NODE_<n>_SSH` for every engine. Discovery assigns the router to the first engine host by default; set `NARWHAL_ROUTER_SSH` when the router uses a separate management destination.

Discovery reads each node's unique global address on `NARWHAL_FABRIC_INTERFACE`, then builds its engine and attestation URLs from that address and the service ports. Set `NARWHAL_NODE_<n>_IP` when the selected interface has multiple global addresses. Set `NARWHAL_NODE_<n>_URL` or `NARWHAL_NODE_<n>_ATTESTATION_URL` when a service uses a different reachable address; set the matching per-node port override when its port differs.

Identical destination values place multiple roles on the same physical host and reuse the same credential. A destination may be an OpenSSH alias carrying its username, port, identity and jump route, or a direct `user@host` destination.

Password authentication uses the matching `_SSH_PASSWORD` variable. Key authentication uses the configured identity or SSH agent.

### Identify the model checkpoint

`NARWHAL_ENGINE_MODEL_NAME` names the served model, while `NARWHAL_MODEL_DIR` names its checkpoint directory on each engine host. Discovery hashes the provisioned checkpoint files and requires one matching content manifest across replicas before generating the fleet. The manifest binds weights, configuration, tokenizer and model code without a model-specific repository setting.

Stage a checkpoint before discovery when the model directory is empty. For a [Hugging Face snapshot](https://huggingface.co/docs/huggingface_hub/guides/download), select its repository ID and full commit SHA, then run `hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"` on each engine host. Retain those source values in the private deployment record. Other checkpoint sources can use the same directory layout; discovery pins their content manifest.

The content manifest is the deployment pin for an already provisioned directory. A Hub commit SHA describes source provenance when available; the manifest verifies the files this fleet will serve.

### Discover the deployed hosts

Run discovery from the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

The workstation needs Python's standard library, OpenSSH and `sshpass` when password authentication is used.

Remote discovery expects:

- Python 3;
- Docker;
- `ip`;
- `rocminfo` or `nvidia-smi`;
- the pinned engine image;
- the model checkpoint at the path declared in `.env`.

On first contact, discovery records the SSH host key reached through the private management route. The authenticated connection establishes trust on first use. `NARWHAL_SSH_KNOWN_HOSTS` may point at an existing verified host-key file.

Later deployment commands reject a mismatched key. If a recorded key changes, verify the host through its provider console before changing the local entry.

For every engine, discovery reads:

- GPU product and device mappings;
- model configuration and hash;
- tokenizer metadata;
- convolutional state fields in the model configuration;
- selected network interface and its global address;
- immutable container image identity.

Discovery hashes every regular file below the model directory, excluding Hugging Face's `.cache/huggingface/` download metadata, and compares each path, byte count and SHA-256 across engine roles. Independent hosts hash their checkpoints concurrently. A differing shard, tokenizer or code file stops discovery before configuration or installation. Per-engine manifests and the first differing path remain in the private discovery record.

A temporary container reads package metadata from the image and then exits. Discovery derives the model dtype and image runtime environment from that inspection.

When checkpoint metadata contains `auto_map`, discovery adds `--trust-remote-code` to the serving arguments. A model with convolutional SSM transfer state receives `VLLM_SSM_CONV_STATE_LAYOUT=DS` in its generated engine launch record before installation. Discovery reads `text_config.linear_attn_config.kda_layers`, `short_conv_kernel_size` and other SSM fields from the checkpoint configuration. The image check verifies both settings before the model loads; the pinned [vLLM layout resolver](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/model_executor/layers/mamba/mamba_utils.py) defaults to SD.

It writes the following files with mode `0600`:

| File                                      | Purpose                                                                                                                            |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `config/hosts.local.json`                 | Groups management destinations from `.env` and assigns router and numbered engine roles.                                           |
| `config/ssh.known_hosts`                  | Stores server public keys observed during authenticated management access.                                                         |
| `config/engine-launch.local.json`         | Combines accelerator allocation, model/image metadata, network devices and launch policy.                                          |
| `config/engine-launch.sources.json`       | Maps engine roles to the inspection and policy records used to build their launch configuration.                                   |
| `config/fleet.json`                       | Defines the model, measured hardware and TP shape, engine URL references, opening roles, initial latency targets and profile path. |
| `config/deployment.env`                   | Selects generated configuration paths, fabric addresses, engine and attestation URLs, plus per-engine image and hash values used by later preparation.                             |

Path variables in `.env` may select alternate locations for generated JSON and host-key files.

Discovery also retains per-engine observations, SSH logs, `engine-<n>-checkpoint.json` file manifests and the shared `model_tree_sha256` below the directory supplied with `--out`.

Keep the generated `config/` files together when reusing an inspected fleet. Load `.env` and `config/deployment.env`, verify access, and prepare a new deployment run from those inputs. Step 3 checks each host's current hardware, image and model before launch. A change to those inputs requires a fresh discovery; archive the previous generated configuration and choose a fresh output directory before rebuilding it.

### Launch policy

For a single engine role on a GPU host, discovery assigns every detected GPU and sets tensor parallelism to the resulting device count.

When multiple engine roles share a host, declare disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists in `.env`.

The generated fleet requires matching accelerator and TP shape across replicas. Engine 1 starts in the prefill pool; remaining engines start in decode. Profiles are written to `runs/profiles.json` inside the newly installed remote checkout.

Default launch settings are:

- TCP on `NARWHAL_FABRIC_INTERFACE`;
- model dtype from the model configuration, or bfloat16 when unspecified;
- automatic KV dtype;
- 128-token requested cache blocks;
- eager execution;
- maximum context length up to 16,384 tokens, capped by the model configuration;
- eight sequences;
- 0.9 GPU memory utilisation.

Step 4 records the cache layout that vLLM actually resolves. Environment values baked into the image become part of the launch record.

Override the defaults in `.env` before discovery:

| Environment field                                                                        | Effect                                                                                                                                       |
| ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | Select GPU indices or NVIDIA UUIDs and set replica TP size.                                                                                  |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Set model dtype and requested cache block size.                                                                                              |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array replacing the default serving arguments. [Runtime launch records](Configuration.md#runtime-launch-records) defines their meaning. |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object overriding launcher-supported image runtime environment fields.                                                                  |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Select `ucx_rdma`, HCA:port entries and RDMA device paths.                                                                                   |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial latency limits in seconds. Defaults are 10 and 0.125 until step 7 recalibrates them.                                                 |

Per-engine policy values also accept `NARWHAL_NODE_<n>_<field>` overrides.

`NARWHAL_ENGINE_ARGS` replaces the default argument array. Discovery still appends `--trust-remote-code` when either `config.json` or `tokenizer_config.json` exposes custom code through `auto_map`. Discovery sets `VLLM_SSM_CONV_STATE_LAYOUT=DS` for detected convolutional SSM transfer and rejects a conflicting `NARWHAL_ENGINE_ENV` override.

Use `NARWHAL_ENGINE_ARGS` for any other model-specific serving flags.

The image check validates the custom-code requirement and creates the mounted checkpoint's tokenizer before the serving model load. The step 4 completion probe exercises that tokenizer through the live HTTP server.

Given the same `.env`, hosts and image, discovery can reproduce the runtime inputs. Example JSON files document their schemas.

### Verify access and open role shells

`plan` prints host IDs and role assignments.

`check-access` performs one pinned-key SSH login per physical host and records `hostname` plus the executed command under `runs/access-<id>/`. Once that verified login succeeds, access is considered valid for every role assigned to the host.

Observed hostnames are labels only and may repeat between machines. Management destinations are used for shell access. Engine HTTP, attestation and fabric addresses retain their separate service roles.

Open an engine shell by role:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

Use `--role router` for the router and the corresponding numbered role for other engines. Colocated roles resolve through the same authentication entry.

For access failures:

- missing access variable: inspect the named field in `.env`;
- new or changed host key: verify the destination and fingerprint through the private access source before replacing the checkout-local entry;
- authentication or connection failure: inspect username, credential, route, SSH port and firewall.

Record the first host and gate that fail. Sanitised extracts from the private access log are sufficient for external troubleshooting.

## 2. Install Narwhal

### Prepare the deployment package

Keep `.env` and `config/deployment.env` loaded in the management shell.

Create a new prepared deployment:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

`NARWHAL_DEPLOYMENT_REVISION` must name the full approved commit in the management checkout.

Preparation packages that commit into a Git bundle and verifies the exact SHA by cloning the bundle locally before finalising the manifest.

The helper emits:

- `.env.router`;
- one `.env.engine-<n>` for each engine role;
- the fleet document generated during discovery;
- router profiling limits derived from each engine launch record's `--max-num-seqs`;
- one `engine-launch.engine-<n>.json` per engine, selected from `NARWHAL_LAUNCH_CONFIG`.

Engine launch records are checked for GPU allocation, TP size and transport device declarations.

Preparation snapshots these management-checkout helpers:

- `tools/deployment/fabric_budget.py`;
- `tools/deployment/launch_engine.py`;
- `tools/deployment/cache_capture_hook.py`.

They are installed on engine hosts under `runs/deployment-tools/`.

The role environment exports the fabric tool as `NARWHAL_FABRIC_BUDGET_TOOL` with digest `NARWHAL_FABRIC_BUDGET_SHA256`.

The engine launcher is exported as `NARWHAL_ENGINE_LAUNCHER` with digest `NARWHAL_ENGINE_LAUNCHER_SHA256`. The serving cache capture hook is exported as `NARWHAL_CACHE_CAPTURE_HOOK` with digest `NARWHAL_CACHE_CAPTURE_HOOK_SHA256`.

The manifest hashes the source bundle, helper snapshots and every role file. Management credentials never leave the workstation environment. [Host environment files](Configuration.md#host-environment-files) lists all exported fields.

Use a new `--out` directory for each deployment preparation. Later commands reference it through `--run`.

The mode-0600 manifest records:

- approved source revision;
- role-to-host mapping;
- all relevant input hashes;
- a unique remote installation directory below `~/Narwhal-deploy/`.

Add the manifest path to the private deployment record.

A missing input or unavailable revision stops preparation before any remote changes. Correct the named input or restore the approved source, then create a new prepared directory.

### Install the first engine host

Install the host carrying the first engine role:

```bash
python3 tools/deployment/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

The installer transfers the Git bundle and that host's role files over verified SSH, clones the bundle, checks the approved revision and installs Narwhal into the checkout's `.venv`.

A host carrying both router and engine roles receives both role environments but only one installation.

Remote commands, exit status and output are retained under the prepared run's `logs/` directory.

Once the first host passes, install the remainder:

```bash
python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

The helper iterates physical host IDs. Completed installations and matching transferred files are reused.

A same-path file with different contents, or a changed source checkout, stops execution at that host before proceeding to later hosts.

Keep the prepared run intact because its hashes and role assignments are revalidated before SSH operations.

### Open installed shells

From management terminals with the workstation `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role router
```

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role engine-1
```

Each shell opens in the prepared remote checkout, loads the role environment and activates `.venv`.

Use one shell per engine role for the remaining preparation and launch work.

If transfer validation fails, compare the prepared manifest and private command log with the existing remote artifact before touching it.

If revision validation fails, confirm that the Git bundle and role files came from the same prepared run.

Interrupted dependency installation can resume with the same `install --run` invocation. The installation completion marker is written only after the installed CLI responds.

`.install-lock` prevents concurrent installers on one host. Remove a stale lock only after verifying that its owning installer has exited.

Existing deployments should remain untouched during recovery.

## 3. Inspect each engine host

Run this stage concurrently in the installed engine-role shells.

`NARWHAL_ENGINE_LAUNCH_CONFIG` points at the transferred `config/engine-launch.engine-<n>.json`.

Print the allocation and launch policy before inspecting the hardware:

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

The private launch record defines:

- replica allocation;
- container device mappings;
- UCX transport selection.

Its `sources` object identifies the records used to derive allocation and device configuration. [Engine launch records](Configuration.md#engine-launch-records) defines these fields.

Store this output with the deployment record.

Inspect the physical accelerators without changing host state:

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

Record the accelerator product and visible physical GPU count on each host.

For ROCm, count only agents with `Device Type` equal to `GPU` and take the product from `Marketing Name`. CPU agents describe the host processor and do not contribute to the GPU count.

A missing inspection utility, driver failure or empty device enumeration blocks engine launch. Repair the host's driver tooling, permissions or device exposure first.

On the router host, compare observations with `hardware.accelerator` in `runs/deployment/fleet.json`.

For each replica:

- set `hardware.accelerators_per_engine` to the number of entries in `gpu_ids`;
- set `hardware.tensor_parallel` to `tensor_parallel_size`;
- confirm that all selected indices or UUIDs are visible on that host.

The host's physical device count describes available hardware. Replica allocation determines the TP shape actually used by Narwhal.

The role environment already contains image, model and run paths, model-config hash, fabric interface, service ports, engine URL, attestation URL and peer fabric addresses produced from `.env` plus discovery.

The launch record contains selected devices and runtime policy. The router's `runs/deployment/fleet.json` contains the full engine inventory.

Where later commands show angle-bracket arguments, use these generated values.

Before creating any engine process, verify the declared artifacts and local resources:

```bash
test "$(docker image inspect "$NARWHAL_ENGINE_IMAGE" --format '{{.Id}}')" = "$NARWHAL_ENGINE_IMAGE"
test "$(sha256sum "$NARWHAL_MODEL_DIR/config.json" | cut -d' ' -f1)" = "$NARWHAL_MODEL_CONFIG_SHA256"
test -d "$NARWHAL_RUN_DIR"
ip address show dev "$NARWHAL_FABRIC_INTERFACE"
ss -ltnp
```

The Docker equality test applies when `NARWHAL_ENGINE_IMAGE` contains an image ID. If the deployment uses a registry digest, inspect the runtime's resolved digest instead.

The `config.json` SHA-256 binds the launch plan to model configuration. The private discovery record contains the complete checkpoint manifest and one matched tree digest for every engine. Repeat discovery when the provisioned checkpoint changes.

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

On ROCm systems, `/dev/kfd` and the selected DRI devices provide GPU access to the container. Mapping `/dev/dri` exposes every DRI device on the host. Verify ownership and container-user permissions against the [ROCm container instructions](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html).

Inspect `ss` output for planned engine HTTP, attestation, NIXL side-channel and transfer ports. A listener belonging to another deployment blocks this launch until ownership is resolved.

## 4. Qualify the transfer fabric

Finish host inspection on every engine before entering this stage.

Group roles by the cache-planning inputs discovered in step 1. Start one serving representative per distinct group and capture its resolved cache pages from the normal startup. Representatives on separate hosts can load concurrently. Measure fabric links one directed edge at a time while representative engines are idle, so the samples reflect the selected source, destination and network path.

Later gates use this evidence as follows:

- step 6 checks each live engine's resolved cache layout against its representative;
- step 8 performs actual NIXL handoffs between peers;
- step 10 measures shared-link behaviour under serving load.

### Group equivalent cache configurations

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

The signature includes:

- immutable image ID;
- model-config hash;
- accelerator product;
- TP size;
- GPU visibility environment;
- pinned runtime package versions;
- cache policy;
- model arguments;
- image environment;
- transfer transport.

Roles with the same signature feed equivalent inputs into vLLM cache planning. GPU indices, addresses and device paths may differ as long as accelerator product and TP shape remain the same.

Every distinct signature requires a serving representative and budget.

Store the signature, selected representative and group membership with the fabric evidence.

### Build the first launch plan

Each engine record contains a `runtime` object with:

- pinned package versions;
- library environment;
- model dtype;
- cache dtype;
- block size;
- model-specific arguments.

The launcher delivered in step 2 creates a Docker command running:

```text
python3 -m vllm.entrypoints.openai.api_server
```

inside `NARWHAL_ENGINE_IMAGE`.

It mounts `NARWHAL_MODEL_DIR` read-only at `/model`, exposes the configured devices and applies the selected TP allocation.

From the engine-1 shell, then from any other cache-group representative shell:

```bash
umask 077
mkdir -p runs
export ENGINE_RUN="runs/engine-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256" &&
python3 "$NARWHAL_ENGINE_LAUNCHER" prepare --out "$ENGINE_RUN"
python3 -m json.tool "$ENGINE_RUN/launch.json"
```

Inspect `launch.json` before running it. It records the immutable image, full serving command, mounts, device mappings, endpoint, application revision and source hashes.

`container.env` contains explicit runtime and transport values and may contain the engine API key. Keep it private.

The generated connector policy uses:

- `NixlConnector`;
- `kv_role=kv_both`;
- UCX;
- `kv_load_failure_policy=fail`.

The launcher derives the advertised side-channel address and port from the role environment, configures TCP or RDMA through `UCX_TLS`, and disables prefix caching for profiling.

[Runtime launch records](Configuration.md#runtime-launch-records) defines the generated fields. The [vLLM NIXL guide](https://docs.vllm.ai/en/v0.29.0/features/nixl_connector_usage/) documents connector configuration.

The launcher binds the cache capture to the launch-record and model-config hashes. For other engines, verify image, model hash, accelerator, TP size and runtime policy against the group's signature. Step 6 checks the resolved layout on every serving process.

vLLM may adjust the requested cache block size during planning. Re-run cache grouping and capture a new serving layout after changing any of these inputs:

- image;
- model;
- dtype;
- cache policy;
- TP allocation;
- model arguments.

A changed workload assumption requires a recalculated fabric budget.

If the launch record or launcher is missing, create a new step 2 prepared run from corrected management inputs.

### Validate the image and start the process

Run:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" check --run "$ENGINE_RUN"
```

The check:

- verifies the checkpoint custom-code requirement;
- inspects local immutable image identity;
- runs a temporary container to compare pinned package versions;
- resolves the configured connector through the image's `KVConnectorFactory`;
- creates the checkpoint tokenizer with the plan's `--trust-remote-code` setting;
- verifies the pinned image resolves DS convolutional state layout when the model requires it;
- records the launch-plan hash in `checked.json`;
- reads `vllm.version.__version__` and stores it as `vllm_api_version`.

`image-check.log` contains connector resolution, tokenizer readiness, convolutional layout validation when applicable, package versions, API version and command output. These checks finish before the model load and fabric matrix.

The inspection container exits when the imports complete.

Resolve package or library mismatches against the declared image and runtime record.

If the launcher itself changes, prepare a new step 2 run so the deployed snapshot and digest agree with the management checkout. Generate a new launch plan from that helper.

Before starting, inspect the planned ports with `ss -ltnp`.

Then:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" start --run "$ENGINE_RUN"
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
docker logs --follow "$ENGINE_CONTAINER"
```

`start` creates a uniquely named container, writes its ID before process startup and stores Docker output in `launch.log`.

It rechecks the saved environment and image-checked plan before creating the container.

Follow logs until model loading and HTTP startup finish. Ctrl-C exits the log follower and leaves the engine running.

If startup fails, inspect:

```bash
docker inspect "$ENGINE_CONTAINER"
docker logs "$ENGINE_CONTAINER"
```

Use the recorded container ID rather than searching by name.

A failed startup should be traced to the corresponding device, model, memory, library or transport input before creating another plan.

If vLLM exits requesting `trust_remote_code=True` or `VLLM_SSM_CONV_STATE_LAYOUT=DS`, retain the failed launch evidence and run step 1 discovery again from the corrected approved revision into fresh private outputs.

Discovery adds the required argument or runtime environment to affected roles. Prepare a new step 2 run, install engine 1 into its new checkout, and retain the other hosts' existing checkouts while validating the corrected engine 1 launch.

Compare old and new:

- image IDs;
- model-config hashes;
- accelerator and TP allocations;
- runtime settings;
- transport.

Capture the corrected representative's serving cache layout and recalculate its budget. When route, interface, transport and test parameters match the retained evidence fingerprints, run `reuse-edge` for each affected directed sample.

After engine 1 passes its HTTP completion probe, install the remaining hosts from the prepared run. Keep the successful engine 1 container running while those hosts receive their corrected records.

Any change to hardware, model, image, cache policy, route or transport invalidates the evidence associated with that gate.

### Probe the live HTTP API

Once vLLM is listening, run the following from the same engine-role shell:

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

The probe verifies:

- `/health`;
- `/version` against the exact API version captured from the checked image;
- configured model presence through `/v1/models`;
- process identity through `process_start_time_seconds`;
- one deterministic text completion.

Distribution package metadata may contain a build suffix. The image check validates the distribution pin; the HTTP probe compares the server's API version with the exact value captured from that image.

For a failed probe, retain the HTTP status, command output and engine log.

A `checked.json` created by an older launcher without `vllm_api_version` requires a new checked plan.

An API-version mismatch should first be treated as an endpoint-ownership problem. Compare the live endpoint with the recorded image and container ID.

Response captures are immutable. Use new filenames or a new launch directory after repair.

Keep each representative process running through fabric qualification, attestation, profiling, preflight and the workload trial. Serialise colocated roles with overlapping GPU allocations.

### Capture the serving cache layout

From each representative shell, copy the page geometry captured during normal startup into its private launch run, then allocate a fabric run:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" capture-cache --run "$ENGINE_RUN"
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
```

`capture-cache` checks the running container, image, source and plan hashes, and every TP rank before writing `cache-layout.json`. The serving container continues to handle requests. Retain its container ID, launch run and startup log. When capture fails, inspect the recorded container log for the startup, cache-planning or delivered-helper error before preparing a corrected plan.

### Calculate the initial fabric requirement

Use this first-deployment workload:

- one remote handoff per second;
- 1,024 prompt tokens per handoff;
- burst size of one;
- one-second transfer budget;
- 25% bandwidth headroom.

Apply the full handoff rate to every candidate edge. This covers the case where all qualifying handoffs concentrate on one link.

Calculate against the resolved cache layout:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" calculate \
  --model-config "$NARWHAL_MODEL_DIR/config.json" \
  --launch-config "$NARWHAL_ENGINE_LAUNCH_CONFIG" \
  --runtime-layout "$ENGINE_RUN/cache-layout.json" \
  --prompt-tokens 1024 --handoffs-per-s 1 --burst 1 \
  --transfer-budget-s 1 --headroom 1.25 \
  --out "$FABRIC_RUN/budget.json"
```

For each layer and TP rank, the calculator uses:

`ceil(prompt_tokens / block_tokens) + extra_blocks`

pages, then sums padded page bytes across the replica.

Cache types are handled according to their state requirements:

- full attention and MLA include pages for the entire prompt;
- Mamba includes boundary state and speculative or checkpoint slots;
- windowed attention includes a boundary page.

The resulting payload bound covers the complete padded cache represented by the runtime specification, including state that a connector may transfer more selectively.

Page geometry comes from vLLM's [cache specs](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_interface.py) and [cache grouping](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/core/kv_cache_utils.py). Runtime-adjusted block sizes and Mamba padding are included.

Required link rate is:

```text
8 * payload_bytes *
max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) *
headroom / 1e9
```

The helper prints decimal Gbit/s and writes a mode-0600 budget containing workload assumptions, payload bound, source hashes and image identity.

Verify that every TP rank resolved the same layout:

```bash
python3 - <<'PY_REPRESENTATIVE_BUDGET'
import json
import os
from pathlib import Path

run = Path(os.environ["FABRIC_RUN"])
budget = json.loads((run / "budget.json").read_text())
layout = json.loads((Path(os.environ["ENGINE_RUN"]) / "cache-layout.json").read_text())
names = {rank["kv_cache_layout"] for rank in layout["ranks"]}
if len(names) != 1:
    raise SystemExit("Inspect the differing TP cache layouts before using one group budget.")
print(f"required_gbps={budget['required_gbps']}; kv_cache_layout={names.pop()}")
PY_REPRESENTATIVE_BUDGET
sha256sum "$FABRIC_RUN/budget.json"
```

For each representative, record:

- `FABRIC_RUN`;
- `ENGINE_RUN`;
- budget SHA-256;
- `required_gbps`;
- resolved KV layout;
- cache-group signature.

A representative budget applies to every outgoing edge from roles in the same cache group.

If every host already has its own completed probe and budget, use each source's local budget instead.

For a source role using another host's representative budget, create a local comparison budget carrying the required rate and representative digest:

```bash
umask 077
mkdir -p runs
export FABRIC_RUN="$(mktemp -d runs/fabric-XXXXXX)"
export REQUIRED_GBPS='<required_gbps from representative budget.json>'
export REPRESENTATIVE_BUDGET_SHA256='<SHA-256 of representative budget.json>'
python3 - <<'PY_EDGE_BUDGET'
import json
import os
from pathlib import Path

required = float(os.environ["REQUIRED_GBPS"])
if not 0 < required < float("inf"):
    raise SystemExit("Use the positive finite rate from the representative budget.")
digest = os.environ["REPRESENTATIVE_BUDGET_SHA256"]
if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
    raise SystemExit("Use the representative budget's SHA-256 digest.")
path = Path(os.environ["FABRIC_RUN"]) / "budget.json"
with path.open("x") as stream:
    json.dump({"required_gbps": required, "representative_budget_sha256": digest}, stream)
    stream.write("\n")
path.chmod(0o600)
PY_EDGE_BUDGET
```

Read `required_gbps` and the digest from the same representative `budget.json`.

A source role with its own calculated budget keeps that file.

Use a new `FABRIC_RUN` when repeating a comparison so previous samples remain immutable.

A corrected budget may be applied to an existing throughput sample when the current host assignment, both routes, both interfaces, transport, utility version and measurement parameters match the retained link record. Re-run the route checks, then use the `link` command from the TCP or RDMA section with the same parameters and a fresh private output path. Compare the current fingerprint and corrected budget against the retained sample:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" reuse-edge \
  --link "$CURRENT_EDGE_PREFIX.link.json" \
  --evidence "$RETAINED_EDGE_PREFIX.evidence.json" \
  --sample "$RETAINED_EDGE_PREFIX.json" \
  --budget "$FABRIC_RUN/budget.json" \
  --out "$CURRENT_EDGE_PREFIX.comparison.json"
```

Set `RETAINED_EDGE_PREFIX` to the original directed sample prefix and `CURRENT_EDGE_PREFIX` to the new route capture prefix. RDMA samples use `.txt` after `--sample`. `reuse-edge` verifies the link and sample SHA-256 values, records the corrected budget hash and exits with the comparison result. Keep the original sample and both evidence records.

For samples collected before `record-edge` was available, reconstruct the original link record from the retained source and reverse routes, interface names, transport, utility version and exact test command. Run `link` and `record-edge` against the original sample and original budget, then capture current routes and run `reuse-edge` against the corrected budget. Collect a fresh directed sample when the original measurement conditions cannot be reconstructed or the current fingerprint differs.

Changes to runtime layout require a new budget. Changes to the network path or transport require a new throughput sample.

Complete all directed-edge comparisons before starting the remaining serving engines. Keep the representatives idle during each throughput sample.

### Select a directed edge

Begin with engine 1 sending to engine 2.

In both role shells, select a private edge prefix on the source host:

```bash
export SOURCE_NODE=1 DEST_NODE=2 TEST_PORT=5201
source_var="NARWHAL_NODE_${SOURCE_NODE}_IP"
dest_var="NARWHAL_NODE_${DEST_NODE}_IP"
export SOURCE_IP="${!source_var:?missing source fabric address}"
export DEST_IP="${!dest_var:?missing destination fabric address}"
ss -ltnp "sport = :$TEST_PORT"
```

On the source, keep one prefix for this directed sample:

```bash
export EDGE_PREFIX="$FABRIC_RUN/engine-${SOURCE_NODE}-to-engine-${DEST_NODE}"
```

The source and destination addresses come from the generated fabric inventory.

`TEST_PORT` is temporary. Check it on both hosts before use and permit traffic between the two selected fabric addresses. Pick another free port if an existing service already owns it. Existing listeners and firewall rules should remain intact.

On the source host inspect:

```bash
ip route get "$DEST_IP" from "$SOURCE_IP" | tee "$EDGE_PREFIX.source-route.txt"
```

On the destination inspect the reverse path:

```bash
ip route get "$SOURCE_IP" from "$DEST_IP"
```

Use `ip -4 route get` or `ip -6 route get` when address family must be explicit.

Each result must choose `NARWHAL_FABRIC_INTERFACE` and the discovered source address for that host.

Copy the destination's exact one-line route output into `$EDGE_PREFIX.destination-route.txt` on the source through the management shells using `printf '%s\n' '<destination route output>' > "$EDGE_PREFIX.destination-route.txt"`. The source route file and this reverse route file bind the sample to both paths. Correct route, source address or interface errors before measuring bandwidth.

Read `transfer.transport` from `NARWHAL_ENGINE_LAUNCH_CONFIG`, then use the corresponding TCP or RDMA test.

Keep MTU, firewall configuration and transport-specific HCA or port selection consistent between peers.

### TCP measurement

For `ucx_tcp`, install `iperf3` on both engine hosts. Debian and Ubuntu hosts can use:

```bash
sudo apt-get install iperf3
```

Record `iperf3 --version` from both ends. The [iperf3 command reference](https://software.es.net/iperf/invoking.html) documents binding, parallel streams and receiver reporting.

On the destination:

```bash
iperf3 --server --bind "$DEST_IP" --port "$TEST_PORT"
```

On the source, send one TCP stream per TP rank. Ignore the first three seconds and measure for ten seconds:

```bash
export FABRIC_STREAMS="$(python3 -c 'import json,os; print(json.load(open(os.environ["NARWHAL_ENGINE_LAUNCH_CONFIG"]))["tensor_parallel_size"])')"
export EDGE_SAMPLE="$EDGE_PREFIX.json"
(set -o noclobber; iperf3 --client "$DEST_IP" --bind "$SOURCE_IP" \
  --port "$TEST_PORT" --parallel "$FABRIC_STREAMS" --omit 3 --time 10 \
  --json > "$EDGE_SAMPLE")
```

Record the measured link inputs and receiver rate in private, mode-0600 evidence:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" link \
  --source-role "engine-$SOURCE_NODE" --destination-role "engine-$DEST_NODE" \
  --source-address "$SOURCE_IP" --destination-address "$DEST_IP" \
  --source-interface "$NARWHAL_FABRIC_INTERFACE" \
  --destination-interface "$NARWHAL_FABRIC_INTERFACE" \
  --source-route "$EDGE_PREFIX.source-route.txt" \
  --destination-route "$EDGE_PREFIX.destination-route.txt" \
  --transport ucx_tcp --tool-version "$(iperf3 --version | head -n 1)" \
  --test-parameters "{\"parallel\":$FABRIC_STREAMS,\"omit_s\":3,\"duration_s\":10,\"port\":$TEST_PORT}" \
  --out "$EDGE_PREFIX.link.json"
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" record-edge \
  --link "$EDGE_PREFIX.link.json" --sample "$EDGE_SAMPLE" \
  --budget "$FABRIC_RUN/budget.json" --out "$EDGE_PREFIX.evidence.json"
```

`record-edge` reads `end.sum_received.bits_per_second` and stores the route, interface, transport, utility version, test parameters, sample hash and budget hash for later comparisons.

Exit codes are:

- `0`: measured receive rate meets the source budget;
- `1`: measured rate is below budget;
- `2`: sample is invalid.

Stop the destination's temporary server after retaining the result.

Reverse the roles and repeat with the reverse source's budget.

### RDMA measurement

For `ucx_rdma`, install `perftest` on both hosts. Debian and Ubuntu hosts can use:

```bash
sudo apt-get install perftest
```

Record `ib_write_bw --version` and use identical test parameters at both ends.

Select each host's HCA and port from `transfer.net_devices` in its launch record. For `mlx5_0:1`:

```text
HCA=mlx5_0
HCA_PORT=1
```

For RoCE, inspect:

```text
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/ndevs/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gid_attrs/types/
/sys/class/infiniband/$HCA/ports/$HCA_PORT/gids/
```

Choose the GID index matching the configured fabric interface, address and RoCE mode, then set `GID_INDEX`.

Store that mapping beside the route evidence. In the source shell, set `DEST_HCA`, `DEST_HCA_PORT` and `DEST_GID_INDEX` from the destination's inspected mapping before recording the link.

Native InfiniBand uses the site's active port and GID selection.

[Perftest](https://github.com/linux-rdma/perftest) documents device selection, GIDs, duration and bandwidth reporting.

Set the address-family arguments in both shells:

```bash
rdma_addr_args=()
case "$DEST_IP" in
  *:*) rdma_addr_args=(--ipv6-addr --ipv6) ;;
esac
```

Destination:

```bash
ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$DEST_IP"
```

Source:

```bash
export EDGE_SAMPLE="$EDGE_PREFIX.txt"
(set -o noclobber; ib_write_bw -d "$HCA" -i "$HCA_PORT" -x "$GID_INDEX" \
  -p "$TEST_PORT" -s 1048576 -D 10 --report_gbits \
  "${rdma_addr_args[@]}" --bind_source_ip "$SOURCE_IP" "$DEST_IP" > "$EDGE_SAMPLE")
cat "$EDGE_SAMPLE"
```

Copy `BW average[Gb/sec]` from the report into `MEASURED_GBPS`, then record the HCA, GID and directed sample with the source budget:

```bash
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" link \
  --source-role "engine-$SOURCE_NODE" --destination-role "engine-$DEST_NODE" \
  --source-address "$SOURCE_IP" --destination-address "$DEST_IP" \
  --source-interface "$NARWHAL_FABRIC_INTERFACE" \
  --destination-interface "$NARWHAL_FABRIC_INTERFACE" \
  --source-route "$EDGE_PREFIX.source-route.txt" \
  --destination-route "$EDGE_PREFIX.destination-route.txt" \
  --transport ucx_rdma --tool-version "$(ib_write_bw --version | head -n 1)" \
  --test-parameters "{\"source_hca\":\"$HCA\",\"source_port\":$HCA_PORT,\"source_gid\":$GID_INDEX,\"destination_hca\":\"$DEST_HCA\",\"destination_port\":$DEST_HCA_PORT,\"destination_gid\":$DEST_GID_INDEX,\"message_bytes\":1048576,\"duration_s\":10,\"port\":$TEST_PORT}" \
  --out "$EDGE_PREFIX.link.json"
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" record-edge \
  --link "$EDGE_PREFIX.link.json" --sample "$EDGE_SAMPLE" \
  --gbps "$MEASURED_GBPS" --budget "$FABRIC_RUN/budget.json" \
  --out "$EDGE_PREFIX.evidence.json"
```

The test measures one-way RDMA writes between host-memory buffers.

Swap source and destination for the reverse direction.

For multi-rail deployments, repeat the measurement for every selected HCA port and retain every report. Actual NIXL probes later determine how the connector uses those rails.

### Complete the directed matrix

For `n` distinct engine hosts, qualify all:

```text
n * (n - 1)
```

directed host pairs.

Colocated replicas do not require a network edge sample. Step 8 tests their local KV handoff.

For each directed sample retain:

- source role;
- destination role;
- source revision;
- source and reverse routes;
- transport;
- utility version;
- exact command;
- budget signature;
- sample path;
- exit status.

Reuse each source role's representative or local budget across its outgoing edges.

A connection failure should be traced through the listener, route, firewall, HCA and GID evidence.

For insufficient throughput, inspect the affected link's speed, MTU, retransmissions or RDMA counters, host CPU saturation and concurrent traffic. Keep the failed sample, correct the identified cause, then measure into a new file.

Proceed only after every required directed edge meets the declared workload budget.

Stop temporary test servers before leaving this stage.

## 5. Configure the fleet and launch engines

Finish host qualification and the directed fabric matrix before expanding beyond the running cache representatives.

On the router, edit the transferred `runs/deployment/fleet.json` with:

- model;
- engine IDs;
- initial role split;
- engine URLs;
- attestation URLs;
- SLO values;
- a fresh profile path.

[Node URL references](Configuration.md#node-urls-from-the-environment) resolves endpoints from `.env.router`.

Step 6 fills `engine_contract` from inspected image state, the running engine and attestation evidence before profiling or router startup.

### Start the remaining engines

Keep each successful representative container running while finishing the directed matrix. Once every edge meets its source budget, perform `prepare`, `check`, `start` and the HTTP probe from the representative procedure for each remaining engine. Run these on separate physical hosts concurrently; serialise roles that share a GPU allocation. Capture each role's live cache layout with `capture-cache` and compare its per-rank page geometry with its representative before attestation.

Keep every successful container ID and `ENGINE_RUN` for attestation. Capture logs before removing any container created by this deployment attempt.

## 6. Attest each engine process

Create attestations after every engine passes the HTTP gate.

Run per-host attestation work concurrently where inspection containers and device allocations do not interfere.

Reuse captures already bound to the checked plan in `ENGINE_RUN`; each capture command creates its destination exclusively. The live dimensions command writes a separate record and retains an earlier `model-dimensions.json` for comparison.

Capture the complete startup log for the checked serving container. The generator reads the resolved attention backend and binds each contract field to retained evidence:

```bash
export ENGINE_CONTAINER="$(cat "$ENGINE_RUN/container.id")"
export ENGINE_STARTUP_LOG="$ENGINE_RUN/startup.log"
(set -o noclobber; docker logs "$ENGINE_CONTAINER" > "$ENGINE_STARTUP_LOG" 2>&1)
```

### Capture the NIXL connector protocol version

`contract.nixl_connector_version` comes from `NIXL_CONNECTOR_VERSION` in the installed vLLM NIXL connector.

The integer participates in vLLM's peer compatibility hash.

`nixl_version` remains the pinned NIXL package version. The connector protocol value is a separate field.

The installed definition is in the image's [NIXL metadata module](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py).

Against the running container, capture the protocol constant together with the checked plan, image ID and container ID:

```bash
.venv/bin/python tools/deployment/attestation_contract.py capture-nixl --run "$ENGINE_RUN"
```

The engine attestation generator reads the capture, and the router finalisation command checks every engine's resulting contract.

The checked image ID and serving container ID tie the capture to the deployed build.

An import or missing-constant failure requires inspection of the pinned build's compatibility-hash implementation before regenerating the capture.

If the serving container no longer exists, restore that checked serving plan before collecting process-bound attestation evidence.

### Capture model dimensions used by compatibility hashing

Populate:

- `head_size`;
- `kv_heads`;
- `hidden_layers`;
- `model_architecture`.

Take them from the installed runtime's:

- `ModelConfig.get_head_size()`;
- `ModelConfig.get_total_num_kv_heads()`;
- `ModelConfig.get_total_num_hidden_layers()`.

NIXL's [compatibility hash](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py) calls those getters.

For DeepSeek-style MLA with MLA enabled, the [head-size resolver](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/transformers_utils/model_arch_config_convertor.py) derives the size from:

```text
kv_lora_rank + qk_rope_head_dim
```

The contract uses the getter results and resolved architecture from the pinned runtime and launch settings.

Capture these fields through the running serving container:

```bash
umask 077
.venv/bin/python tools/deployment/attestation_contract.py capture-model-dimensions --run "$ENGINE_RUN"
cat "$ENGINE_RUN/model-dimensions.live.json"
```

The capture reads the plan and launcher mounted in that container, checks their hashes against `launch.json`, and obtains the getters and architecture from its installed vLLM. The temporary inspection process exits after configuration resolution while the serving engine stays running.

The capture writes `model-dimensions.live.json` beside the serving plan and keeps a private `model-dimensions.live-*.log`. When an earlier `model-dimensions.json` exists, the capture compares its three getter values with the live values and retains both records. The generator reads the live values and cites their capture path in `sources`.

Keep these inspection values with:

- `use_mla`;
- model-config hash;
- image identity;
- application revision;
- serving plan hash.

The model inspection uses the checked serving plan, image ID and container ID; the generator requires those bindings to match the live process. A newly installed launcher can inspect an older running plan through this command because the container supplies its plan-mounted launcher.

Run the same inspection on every engine's checked serving plan.

A model import, hash or getter error identifies the failing pinned runtime, model metadata or launch input. Inspect the private `model-dimensions.live-*.log`, then correct the identified input before recapturing against the matching running process.

### Capture physical cache grouping

Set `cross_layers_blocks` from the resolved KV cache layout.

Using vLLM's [layout enum](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_layout.py):

- `BLHNC`, `BLNHC`, `BHLNC` have `is_block_outermost=true`;
- `LBHNC`, `LBNHC`, `LHBNC` have `is_block_outermost=false`.

The [NIXL registration path](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py) consumes views using those physical strides.

Retain both the resolved layout name and boolean.

vLLM records the chosen layout in the serving startup log with a line of the form:

```text
Using <layout> KV cache layout.
```

The corresponding resolver is in [attention backend utilities](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/attention/backends/utils.py).

Point `ENGINE_STARTUP_LOG` at the serving launch log and inspect the enum from the checked image:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
cat "$ENGINE_RUN/cache-registration.json"
```

The command requires exactly one resolved layout name.

`cache-registration.json` contains:

- `cross_layers_blocks`;
- layout name;
- enum source hash;
- startup-log hash;
- checked plan hash;
- image identity.

The generator reads the boolean and cites the capture plus enum property in `sources.cross_layers_blocks`.

The retained startup log belongs to the serving container recorded in `ENGINE_RUN`; the generator checks its cache layout against the live cache capture.

Now compare each live role with its cache representative:

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

A matching group signature plus matching resolved layout allows the representative's measured page geometry to remain the group's fabric basis.

For a representative engine with no startup-layout line, the serving capture can supply the layout:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" cache-registration \
  --run "$ENGINE_RUN" --runtime-layout "$ENGINE_RUN/cache-layout.json"
```

The helper checks image identity, model-config hash, launch-record hash, TP rank coverage and layout agreement.

If another engine resolves a different layout, capture its serving layout, calculate its outgoing budget and compare the retained edge samples through `reuse-edge` using current link inputs. Keep the process and its startup evidence while diagnosing the group mismatch; use a corrected checked plan if its launch inputs require repair.

An unknown layout or missing enum requires inspection of the pinned build before setting `cross_layers_blocks`.

### Capture transfer direction

The resolved connector class determines `transfer_mode`:

- `NixlPullConnector` → `"pull"`;
- `NixlPushConnector` → `"push"`.

In the pinned API, [NixlConnector aliases NixlPullConnector](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/connector.py).

`kv_both` means the engine may produce and consume KV. It does not choose push versus pull.

The image check in step 4 or step 5 already retained the class selected by `KVConnectorFactory`.

Derive the mode from that record:

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

The generator reads the resolved string and records the capture path and class in `sources.transfer_mode`.

Check the class against the serving startup log.

If the image-check log contains no unique resolved connector, repeat connector resolution with the pinned image check.

For an unfamiliar class, inspect its implementation for an explicit protocol definition before assigning a mode.

### Capture handshake compatibility enforcement

The pinned NIXL worker resolves the extra configuration with:

```text
kv_transfer_config.get_from_extra_config("enforce_handshake_compat", True)
```

and stores the result in:

```text
self.enforce_compat_hash
```

The value controls rejection of peer compatibility-hash mismatches during the [worker handshake](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py).

Current launcher plans set `enforce_handshake_compat=true` explicitly. Older plans that omit it use the installed worker's default.

Capture the effective setting from the checked serving plan:

```bash
python3 "$NARWHAL_ENGINE_LAUNCHER" handshake-policy --run "$ENGINE_RUN"
cat "$ENGINE_RUN/handshake-policy.json"
```

The inspection:

- compares the serving command's `--kv-transfer-config` with the recorded connector object;
- reads the worker initializer from the image;
- extracts the installed default;
- resolves the final value through `KVTransferConfig.get_from_extra_config`.

The accepted result must be Boolean `true`.

`handshake-policy.json` stores:

- effective value;
- whether the launch plan supplied the value explicitly;
- installed default;
- source text and hash;
- connector configuration;
- checked plan hash;
- image identity.

The temporary process exits before model-worker creation, so the live serving container remains untouched.

The generator reads the Boolean and cites the capture plus:

```text
NixlBaseConnectorWorker.__init__: self.enforce_compat_hash
```

If the result is false or non-Boolean, correct launch configuration and restart the engine from a newly checked plan.

If the worker implementation differs from the pinned expectation, inspect the compatibility assignment directly before deriving the field.

The peer handshake and KV-transfer checks in step 8 exercise this policy between live engines.

### Start the attestation sidecar

Generate the role-specific private document from the checked plan, live cache capture, pinned runtime inspections and serving startup log. The command rejects mismatched plan hashes, missing evidence, incomplete fields and an existing destination:

```bash
.venv/bin/python tools/deployment/attestation_contract.py generate \
  --run "$ENGINE_RUN" --startup-log "$ENGINE_STARTUP_LOG"
export ATTEST_DOCUMENT="$ENGINE_RUN/engine-attestation.json"
```

Before starting the sidecar, reconfirm:

- engine `/health`;
- `/version`;
- `process_start_time_seconds`.

These identify the process being attested.

Start the sidecar from the engine-role shell. The helper reads the engine endpoint from the checked plan and the sidecar bind address and port from the role environment's `NARWHAL_NODE_<n>_ATTESTATION_URL`:

```bash
.venv/bin/python tools/deployment/attestation_contract.py serve --run "$ENGINE_RUN"
```

Discovery placed that attestation URL in the generated router fleet. Verify that `/health` and `/v1/attestation` are reachable from the router over the trusted control network.

### Verify the sidecar against the live process

Open a second shell for the same engine role.

Set `ATTEST_BASE` to the sidecar base URL and `ATTEST_DOCUMENT` to the file used to launch it. Reuse the serving launch directory through `ENGINE_RUN`.

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

Run this check for every engine and store its capture directory in the deployment record.

After all sidecars pass, run this once in the router role shell. It reads each live engine and sidecar, verifies the process identity and complete contract, requires the same contract across the fleet, retains the initial generated fleet under `runs/`, and fills `engine_contract` in `runs/deployment/fleet.json`:

```bash
.venv/bin/python tools/deployment/attestation_contract.py finalize-fleet --fleet runs/deployment/fleet.json
```

A differing contract identifies a specific engine launch or capture input to correct before profiling. Restart the affected engine and its sidecar from a checked plan, then rerun finalisation.

Keep all engines and sidecars running through profiling, preflight and the workload trial.

If an endpoint or contract check fails, inspect the captured response and sidecar log, correct the input and restart only that sidecar against the same live engine before retrying.

## 7. Profile idle engines

Reserve the real engines and leave them otherwise idle.

From the router host:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,512,1024,2048,4096,8192,12288 \
  --decode-input-lens 512,4096,8192
```

Deployment preparation derives `runs/deployment/profiling-limits.json` from each engine's generated `--max-num-seqs` launch argument and transfers it to the router host. The profiler caps its candidate concurrency points at that engine's limit and includes the limit as a measurement point when needed. It reads `max_model_len` from each live engine's `/tokenize` response, selects input lengths that leave room for one prefill output token or 64 decode output tokens, then checks the exact tokenised prompt before sending each completion. Compare the printed effective sweep with each checked serving plan; a shorter context or a one-sequence limit requires adjusting the launch policy or input-length sweep before profiling.

Use the private engine URLs and credentials from the router environment.

Warm the model and keep prefix caching disabled under the [measurement conditions](Measure.md#1-calibrate-slos).

The profiler measures one-token prefill latency over input length.

Decode profiling varies prompt length and concurrency while the cohort remains in decode, then fits observed token intervals against active request count plus estimated resident KV.

Choose input lengths and concurrency points that cover expected production traffic. Keep the sample sidecar.

The controller holds a role change when its projected decode point falls outside the measured profile range.

Narwhal verifies that profile engine IDs exactly match the configured fleet. A mismatch at startup stops the controller and prints the differing IDs.

Do not replace engine processes or change runtime configuration between profiling, preflight and the trial.

Any engine restart or runtime change requires a fresh profile set followed by another preflight.

Set:

- `slo.ttft_s`;
- `slo.tpot_s`;

from light-load measurements on the deployed engine shape.

Keep the TPOT target above the measured per-token floor.

## 8. Run preflight against the engine and KV contract

From the router host:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json
```

Use the same processes, fleet configuration and profiles from step 7.

| Gate       | Validation                                                                                                                                                                                                        |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reach`    | Every engine responds inside the configured health budget.                                                                                                                                                        |
| `contract` | Attestation agrees with the current process and declared runtime.                                                                                                                                                 |
| `model`    | Every engine serves the configured model.                                                                                                                                                                         |
| `pace`     | Prefill latency stays within the permitted slowdown. With at least three successful probes, comparison uses the fleet median. Saved per-engine profiles are used when available. Smaller fleets require profiles. |
| `tokenize` | Exact input sizing succeeds when enabled.                                                                                                                                                                         |
| `produce`  | Each tested engine can export a KV handoff.                                                                                                                                                                       |
| `consume`  | Each tested peer can consume that handoff.                                                                                                                                                                        |
| `profile`  | Profile engine IDs match the fleet exactly.                                                                                                                                                                       |
| `slo`      | Configured latency targets are feasible against the measured profiles.                                                                                                                                            |

By default, `narwhal-check` tests every eligible producer-consumer pair.

Use `--ring` for the configured maintenance ring.

Use `--repeats` when diagnosing intermittent transfer failures.

Start the router only after all required gates pass.

A failure reports the affected engine, transfer leg or budget so recovery can start from the corresponding evidence.

## 9. Start the router and verify one request

On the router host:

```bash
.venv/bin/narwhal-serve \
  --fleet runs/deployment/fleet.json \
  --host 0.0.0.0 \
  --port 8000
```

Bind the service to the trusted control network selected during discovery.

Open another shell on the same host. `localhost` refers to that router machine.

Replace `<served-model>` with the model configured in the fleet. Adjust the URL consistently if the router listens elsewhere.

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/ready
curl -fsS http://localhost:8000/narwhal/state | python3 -m json.tool
curl -fsS http://localhost:8000/metrics
curl -fsS http://localhost:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Record the router listener and the network path used by each client.

Router-local probes and Prometheus use the router's local listener.

The workstation load client reaches the router through the SSH forwarding configured in step 10.

Check the listener's address family. `127.0.0.1` cannot reach a socket bound only to IPv6 `::`.

Before applying load, capture:

- `/health`, including process liveness and cached instance counts;
- `/ready`, including admission state;
- `/narwhal/state`, including all engines and current role split;
- `/metrics`;
- one successful completion that increments `served`.

Keep the anonymous router endpoint on a trusted network. Public ingress requires TLS, authentication, WAF policy, request limits and model routing.

## 10. Validate capacity through the private route

The first trial uses:

- the management workstation as load generator;
- the existing router host for Prometheus and Grafana;
- the verified SSH management path for router, Prometheus and Grafana access.

The `router` inventory role supplies the destination and credential used for all three forwards.

Record:

- router role assignment;
- workstation hostname;
- source revision;
- local-to-remote tunnel mappings.

The trial includes SSH network and encryption overhead in the measured client path.

### Start observability on the router

Open the installed router-role shell.

Follow [Set up observability](Observability.md#1-select-the-deployment) using:

- `runs/deployment/fleet.json`;
- router URL `http://127.0.0.1:8000`.

Run:

```bash
make observe
```

Keep the target-discovery and dashboard-verification output.

Prometheus scrapes the router locally and resolves engine targets from the fleet document.

### Open the SSH forwards

From a management-checkout terminal with `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
```

The helper:

- binds workstation loopback ports;
- verifies the recorded SSH host key;
- uses the router host's configured password, key or agent authentication.

Leave this terminal running.

From another workstation terminal:

```bash
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
curl -fsS "$NARWHAL_TRIAL_URL/health"
curl -fsS "$NARWHAL_TRIAL_URL/ready"
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

Open Grafana at:

```text
http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator
```

Point the workload client at `$NARWHAL_TRIAL_URL`.

If a local port is occupied, choose a free left-hand port and update the corresponding client URL.

If forwarding succeeds but the remote service is unreachable, inspect that service's listener from the router shell.

Use `--remote-address` when the remote service is bound to an address other than the default. Services bound to different remote addresses require separate tunnel invocations.

Keep the tunnel log under the reported `runs/access-<id>/` path and record any terminal error in the deployment evidence.

Ctrl-C closes the forwards owned by that tunnel process.

### Run and retain the trial

Close the initial deployment trial in this order:

1. Create a deployment identifier and assemble the [deployment evidence set](Measure.md#2-validate-the-deployment-under-load).
2. Attach the passing step 8 full preflight mesh for the current engine processes, fleet, profiles and targets. Repeat preflight whenever any of those inputs change.
3. Retain monitoring startup output plus successful Prometheus scrape evidence for the router and engines.
4. Run the [initial synthetic workload](Measure.md#synthetic-trial) from the workstation through `$NARWHAL_TRIAL_URL`. The supplied workload procedure creates 200-request runs at 0.5 and 1 request/s using 8,192 input tokens and 128 output tokens. Keep the workload definition, per-request records and summaries. Treat 2-second TTFT, 33.3-ms TPOT and 95% attainment as candidate thresholds until measured performance and the service requirement define acceptance. Capture client CPU, memory, network and scheduler behaviour so load-generator or SSH-path saturation can be separated from serving saturation.
5. Drain resident work. Reconcile every offer against client and router terminal classes. Query engine, request, token, role and pool-load series through Grafana's provisioned data source. Run the post-load KV ring.

[Measure a fleet](Measure.md) defines request timing, rate selection and required artifacts.

[Set up observability](Observability.md) defines scrape and dashboard validation.

After resident work drains, run:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
```

The deployment trial is complete when all of the following hold:

- the router serves the measured workload;
- client records reconcile with the router journal;
- Prometheus successfully scrapes router and engine targets;
- Grafana contains the required series;
- the post-load KV ring passes.

Retain the service locations, approved source revision, fleet configuration, profiles, router journal and monitoring endpoints with the private deployment record.

## Deployment gates and recovery

When sharing deployment evidence outside the private environment, replace private addresses, paths and credential values and use stable host aliases.

| Stage                                                             | Inputs that must be known                                                                                                                                                            | Failure handling                                                                                                                                                                                                                                                                                                                                              | Private evidence                                                                                                                                                                                                              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Management access](#1-bootstrap-management-access)               | Workstation `.env`, installed image and model, GPU inspection utilities, initial launch policy.                                                                                      | For discovery failure, use its log to identify the failing environment field, host utility or image inspection. Verify any rejected host key independently before replacing it. For login failure, inspect the per-host log, management route and credential.                                                                                                 | Workstation `.env`, generated JSON, host-key database, `runs/discovery/<run>/`, `runs/access-<id>/`.                                                                                                                          |
| [Host installation](#2-install-narwhal)                           | Approved commit, common launch fields, per-engine allocation records, node overrides, host prerequisites.                                                                            | Correct preparation inputs before creating another run. For transfer or checkout mismatch, compare prepared hashes with existing remote files. Dependency failure may resume through the same prepared run once the host problem is fixed.                                                                                                                    | Workstation `runs/deployment-env/<run>/`; remote `~/Narwhal-deploy/<id>/`, role files, router fleet config, install marker.                                                                                                   |
| [Engine inspection](#3-inspect-each-engine-host) | PCI accelerator identity, visible device count, replica allocation, TP size, image identity, checkpoint tree digest, paths and ports. | Restore missing devices or artifacts and resolve listener ownership before launch. For a checkpoint mismatch, inspect the first differing path and private per-engine manifests, repair the affected files, then repeat discovery. | Engine `.env.engine-<n>` and launch record; workstation `runs/discovery/<run>/engine-<n>-checkpoint.json` and shared `model_tree_sha256`. |
| [Fabric qualification](#4-qualify-the-transfer-fabric)            | Peer addresses, TCP or RDMA transport, checked serving representative, captured cache pages, prompt length, handoff rate, burst allowance and transfer-time budget.                            | For serving capture errors, inspect the recorded container and startup log to isolate model, device, runtime or cache-spec input. For network failures, inspect route, source binding, listener, firewall, HCA and GID selection. For insufficient bandwidth, inspect link state, MTU, retransmissions or RDMA counters, CPU use and concurrent traffic before collecting another sample.           | Role environment, engine launch record, model config, delivered helper and digest, serving `ENGINE_RUN`, cache layout, container ID and log, budget, directed samples, link fingerprints, comparisons and edge matrix.                             |
| [Engine launch](#5-configure-the-fleet-and-launch-engines)        | Qualified host and fabric state, immutable image, package pins, model flags, library environment, cache shape, TP allocation and selected devices.                                   | Package, tokenizer, connector or convolutional-layout mismatches fail the image check before model loading. For process or HTTP failure, inspect the exact recorded container and logs before creating a corrected plan.                                                                                                                                                                                              | Router fleet config, engine role environment, launch record, launcher snapshot, `runs/engine-launch-*/`, container environment, image-check output, container ID and HTTP captures.                                           |
| [Attestation](#6-attest-each-engine-process) | Checked serving plan, live container and HTTP identity, NIXL protocol, model dimensions, cache layout, transfer mode, handshake policy and sidecar URL from the role environment. | Capture dimensions through the live container when an earlier record lacks the architecture. For a hash, image, container or getter mismatch, inspect the pinned source and prepare a checked plan when that source changes. A sidecar identity failure requires checking the serving process before restarting its sidecar. The router finalisation command identifies a mismatched engine contract before writing the fleet configuration. | Engine `runs/engine-launch-*/` captures and `runs/engine-launch-*/engine-attestation.json`; router `runs/deployment/fleet.json` and `runs/fleet.before-attestation-*.json`. |
| [Profiling](#7-profile-idle-engines)                              | Idle engine reservation, cache policy, generated sequence limits, workload input lengths and concurrency.                                                                            | Inspect the failing engine, measured range and sample output. Correct the cause and write the next sweep to a fresh profile path.                                                                                                                                                                                                                             | Router fleet configuration, generated profiling limits and profile/sample files under `runs/`.                                                                                                                                |
| [Preflight](#8-run-preflight-against-the-engine-and-kv-contract)  | Current engine processes, profile set and SLO targets.                                                                                                                               | Use the reported engine, leg and budget to select the matching check in [fleet troubleshooting](Troubleshoot.md).                                                                                                                                                                                                                                             | Router environment, fleet configuration and private preflight output.                                                                                                                                                         |
| [Router verification](#9-start-the-router-and-verify-one-request) | Router bind address, served model, engine count and initial pool split.                                                                                                              | For bind or readiness failure, inspect listener ownership, address family and the corresponding router or engine error.                                                                                                                                                                                                                                       | Router environment, fleet configuration and endpoint captures.                                                                                                                                                                |
| [Capacity trial](#10-validate-capacity-through-the-private-route) | Workstation load helper, router-host Docker Compose, SSH access, generated workload, fixed launch/cache policy, latency and attainment targets, private SSH route.                   | Local tunnel conflicts require a different local port. Service or scrape errors require inspection from the router host. For workload failures, inspect stream status, token accounting, client CPU, memory, network and scheduling lag before changing offered rate. Reconcile client and router records before attributing an SLO miss to serving capacity. | Workstation access environment, tunnel logs, router fleet and role environment, Compose discovery files, `runs/load-trial-<id>/` workload, manifests, request records, summaries and state/network snapshots.                 |
