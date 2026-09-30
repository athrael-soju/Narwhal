# Gate A: Freeze inputs and discover the fleet

Gate A reads the private `.env`, logs in to each host, hashes the checkpoint, inspects the pinned image, and writes the `config/` files that later gates use. Later evidence is derived from these inputs, so changing one afterward means repeating later gates. [What to repeat after a change](../Deploy.md#what-to-repeat-after-a-change) lists which.

## Load the private environment

Use a clean management checkout. `.env.example` lists the required fields. Disable shell tracing before loading `.env` to keep secrets out of the output:

```bash
set +x
set -a
. ./.env
set +a
```

Set `NARWHAL_NODE_<n>_SSH` for each engine. The router runs on the lowest-numbered engine's destination unless `NARWHAL_ROUTER_SSH` is set. Roles that share a destination are treated as one host with one credential.

A destination is either an OpenSSH alias, which carries the username, port, identity, and any jump host, or a plain `user@host`. For password login, set the matching `_SSH_PASSWORD`. When it is unset, SSH uses the configured identity or the agent.

Discovery looks up the single global address on `NARWHAL_FABRIC_INTERFACE` and builds the engine and attestation URLs from that address and the configured service ports. If an interface has several global addresses or a service is reachable elsewhere, set per-node overrides:

- `NARWHAL_NODE_<n>_IP`: the address to use when the interface has several.
- `NARWHAL_NODE_<n>_URL`: the engine service URL, when it is reachable through a different address.
- `NARWHAL_NODE_<n>_ATTESTATION_URL`: the same for the attestation service.
- The matching per-node port overrides: for a service bound to a different port.

## Stage and identify the checkpoint

`NARWHAL_ENGINE_MODEL_NAME` is the name the model is served under. `NARWHAL_MODEL_DIR` is where the checkpoint sits on each engine host.

To download from Hugging Face into an empty directory, pin the repository and the full commit SHA, and use the same snapshot on every engine. `MODEL_REPO_ID` and `MODEL_REVISION` are shell variables, not `NARWHAL_*` settings:

```bash
hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"
```

Record the repository ID and commit SHA in the private record. Checkpoints from other sources work when every engine host has an identical file tree at `NARWHAL_MODEL_DIR`.

Discovery hashes every regular file in the model directory except the root `README.md` and anything under `.cache/huggingface/`. It compares paths, sizes, and SHA-256 values across engines. Discovery fails on any mismatch in a shard, tokenizer, config, or code file, before writing configuration or installing anything.

## Run discovery and check access

Each engine host needs Python 3, Docker, `ip`, either `rocminfo` or `nvidia-smi`, the pinned engine image, the run directory, and the checkpoint at the configured path.

From the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

The first time discovery connects to a host, it records the SSH host key seen over the authenticated route. To use an already verified known-hosts file, set `NARWHAL_SSH_KNOWN_HOSTS`. Afterward, deployment commands refuse any host whose key has changed. Verify a new key through a separate channel, such as the provider's console, before replacing the local entry.

For each engine, discovery records the GPU product and mappings, the checkpoint config and hash, tokenizer metadata, convolutional-state fields, the fabric interface and address, and the image's immutable identity. It starts a short-lived container to read the image's package metadata. The model dtype comes from the checkpoint config, and the image's runtime environment comes from the image metadata.

Discovery adds two settings based on checkpoint contents:

- If the checkpoint metadata contains `auto_map`, it adds `--trust-remote-code`.
- If it finds convolutional SSM transfer state, it sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`. It detects this from fields such as `text_config.linear_attn_config.kda_layers` and `short_conv_kernel_size`. The setting is required because the layout resolver in the pinned vLLM v0.29.0 defaults to SD.

Before the model loads, the [Gate C](03-Validate-Engines.md) image check confirms both settings, validates the custom-code requirements, and builds the tokenizer the serving arguments select. The HTTP completion probe later in that gate uses the tokenizer on a real request.

Discovery writes these files, all with mode 0600:

| File                                | Purpose                                                                                                        |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `config/hosts.local.json`           | Host-to-role mapping for the router and engines.                                                               |
| `config/ssh.known_hosts`            | SSH host keys observed through authenticated management access.                                                |
| `config/engine-launch.local.json`   | Per-engine launch records: accelerator allocation, image and model metadata, transfer devices, launch policy.  |
| `config/engine-launch.sources.json` | Inspection and policy records used for each role.                                                              |
| `config/fleet.json`                 | Model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, profile path. |
| `config/deployment.env`             | Generated paths, fabric addresses, engine and attestation URLs, and per-engine image and hash values.          |

The discovery output directory also holds the per-engine observations, SSH logs, `engine-<n>-checkpoint.json` (which records the file exclusion policy), and a `manifest.json` with the shared `model_tree_sha256`.

To reuse an existing discovery, reload `.env` and `config/deployment.env`, check access, and prepare a new deployment run. If the hardware, image, checkpoint, or launch policy has changed, run discovery again into a new output directory.

## Confirm launch policy

When a GPU host carries one engine role, discovery assigns that engine every GPU it finds and sets tensor parallelism (TP) to match. When several engine roles share a host, give each one a non-overlapping `NARWHAL_NODE_<n>_GPU_IDS` list.

At least two engines are required, and all must have the same accelerator product and TP shape. Engine 1 starts in the prefill pool and the rest start in decode.

The default serving policy is:

| Setting                | Default                                            |
| ---------------------- | -------------------------------------------------- |
| Transfer               | TCP on `NARWHAL_FABRIC_INTERFACE`                  |
| Model dtype            | Model configuration, or bfloat16 when unspecified  |
| KV dtype               | Automatic                                          |
| Requested cache block  | 128 tokens                                         |
| Execution              | Eager                                              |
| Maximum context        | Up to 16,384 tokens, capped by model configuration |
| Maximum sequences      | 8                                                  |
| GPU memory utilization | 0.9                                                |
| Initial TTFT limit     | 10 s                                               |
| Initial TPOT limit     | 0.125 s                                            |

The block size is a request. vLLM can allocate a different layout, so Gate C records the layout the engine uses.

To change the policy, set these fields in `.env` before running discovery:

| Field                                                                                    | Meaning                                                                     |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | GPU indices or NVIDIA UUIDs and replica TP size.                            |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Model dtype and requested cache block size.                                 |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array replacing default vLLM arguments.                                |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object overriding launcher-supported image runtime environment fields. |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Select `ucx_rdma`, HCA:port entries, and RDMA device paths.                 |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial latency limits, recalibrated after profiling.                       |

To override a field for one engine, use `NARWHAL_NODE_<n>_<field>`.

`NARWHAL_ENGINE_ARGS` replaces the whole default argument list. Discovery still appends `--trust-remote-code` and enforces the DS layout when the checkpoint needs them. Discovery rejects a `NARWHAL_ENGINE_ENV` value that conflicts with a required setting.

Profiles are written to `runs/profiles.json` in the installed checkout.

## Check access

`plan` prints the host IDs and which roles land on each host. `check-access` logs in once per physical host, checks the pinned host key, and saves the `hostname` output and the command under `runs/access-<id>/`. One successful login covers every role on that host.

To open a shell for a role:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

Use `--role router` or `--role engine-<n>` for other roles.

Common failures:

- Missing access variable: check the `.env` field named in the error.
- New or changed host key: see the host key note under [Run discovery and check access](#run-discovery-and-check-access).
- Authentication or connection failure: check the username, credential, route, SSH port, and firewall.

Record which host and gate failed. Share sanitized excerpts of the access logs when asking for help.

Next: [Gate B: Package and install the approved revision](02-Install.md).
