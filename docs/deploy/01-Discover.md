# Gate A: Freeze inputs and discover the real deployment

Discovery pins down the inputs the rest of the deployment is built on. It reads your private `.env`, logs in to each host, hashes the checkpoint, inspects the pinned image, and writes the configuration every later gate uses. Because later evidence is derived from these inputs, changing one of them afterward means redoing some work. [What to repeat after a change](../Deploy.md#what-to-repeat-after-a-change) lists exactly what.

## Load the private environment

Start from a fresh management checkout. `.env.example` documents the fields you need. Turn off shell tracing before you load the file so secrets don't get echoed to the terminal:

```bash
set +x
set -a
. ./.env
set +a
```

Set `NARWHAL_NODE_<n>_SSH` for each engine. The router goes on the lowest-numbered engine's destination unless you set `NARWHAL_ROUTER_SSH`. Roles that share a destination are treated as one host with one credential.

A destination can be an OpenSSH alias, which carries the username, port, identity, and any jump host, or a plain `user@host`. For password login, set the matching `_SSH_PASSWORD`. Otherwise SSH uses the configured identity or your agent.

Discovery looks up the single global address on `NARWHAL_FABRIC_INTERFACE` and builds the engine and attestation URLs from that address and the configured service ports. If an interface has more than one global address, or a service is reachable somewhere else, set per-node overrides:

- `NARWHAL_NODE_<n>_IP` picks one address when the interface has several.
- `NARWHAL_NODE_<n>_URL` is for an engine service reachable through a different address.
- `NARWHAL_NODE_<n>_ATTESTATION_URL` does the same for the attestation service.
- The matching per-node port overrides cover a service bound to a different port.

## Stage and identify the checkpoint

`NARWHAL_ENGINE_MODEL_NAME` is the name the model is served under. `NARWHAL_MODEL_DIR` is where the checkpoint sits on each engine host.

If you're pulling from Hugging Face into an empty directory, pin both the repository and the full commit SHA, and stage the same snapshot on every engine:

```bash
hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"
```

Write the repository ID and commit SHA into the private record. Checkpoints from other sources work too, as long as the directory layout is the same.

Discovery hashes every regular file in the model directory except the root `README.md` and anything under `.cache/huggingface/`. It then compares paths, sizes, and SHA-256 values across the replicas. If a shard, tokenizer, config, or code file differs anywhere, it stops there, before any configuration is written or anything is installed.

## Run discovery and check access

Each engine host needs Python 3, Docker, `ip`, either `rocminfo` or `nvidia-smi`, the pinned engine image, the run directory, and the checkpoint at the configured path.

From the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

The first time discovery connects to a host, it records the SSH host key it sees over the authenticated route. If you already have a verified known-hosts file, point `NARWHAL_SSH_KNOWN_HOSTS` at it instead. From then on, deployment commands refuse any host whose key has changed. If that happens, confirm the new key through the provider's console before you replace the local entry.

For each engine, discovery records the GPU product and mappings, the checkpoint config and hash, tokenizer metadata, convolutional-state fields, the fabric interface and address, and the image's immutable identity. To read the image's package metadata it starts a temporary container, which exits once it's done. The model dtype comes from the checkpoint config, and the image's runtime environment comes from the image metadata.

Two settings depend on what's in the checkpoint, and discovery adds them for you:

- If the checkpoint metadata contains `auto_map`, it adds `--trust-remote-code`.
- If it finds convolutional SSM transfer state, it sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`. It works this out from fields such as `text_config.linear_attn_config.kda_layers` and `short_conv_kernel_size`. The setting has to be explicit because the layout resolver in the pinned vLLM v0.29.0 defaults to SD.

The image check in Gate C confirms both before the model loads.

Discovery writes the following files, all with mode 0600:

| File                                | Purpose                                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `config/hosts.local.json`           | Maps management destinations to router and engine roles.                                                                        |
| `config/ssh.known_hosts`            | Stores server public keys observed through authenticated management access.                                                     |
| `config/engine-launch.local.json`   | Combines accelerator allocation, image/model metadata, transfer devices, and launch policy.                                     |
| `config/engine-launch.sources.json` | Identifies inspection and policy records used for each role.                                                                    |
| `config/fleet.json`                 | Defines model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, and profile path. |
| `config/deployment.env`             | Selects generated paths, fabric addresses, engine/attestation URLs, and per-engine image/hash values used later.                |

The discovery output directory also holds the per-engine observations, SSH logs, `engine-<n>-checkpoint.json` (which records the file exclusion policy), and a `manifest.json` with the shared `model_tree_sha256`.

Keep the generated `config/` files together. To come back to a fleet you've already inspected, reload `.env` and `config/deployment.env`, check access, and prepare a new deployment run. If the hardware, image, checkpoint, or launch policy has changed since then, run discovery again into a new output directory instead.

## Confirm launch policy

When a GPU host carries one engine role, discovery gives that engine every GPU it finds and sets tensor parallelism to match. When several engine roles share a host, give each one its own non-overlapping `NARWHAL_NODE_<n>_GPU_IDS` list.

You need at least two replicas, and they must all have the same accelerator product and TP shape. Engine 1 starts in the prefill pool and the rest start in decode. Profiles are written to `runs/profiles.json` in the installed checkout.

The default serving policy is:

| Setting                | Default                                            |
| ---------------------- | -------------------------------------------------- |
| Transfer               | TCP on `NARWHAL_FABRIC_INTERFACE`                  |
| Model dtype            | Model configuration, or bfloat16 when unspecified  |
| KV dtype               | automatic                                          |
| Requested cache block  | 128 tokens                                         |
| Execution              | eager                                              |
| Maximum context        | up to 16,384 tokens, capped by model configuration |
| Maximum sequences      | 8                                                  |
| GPU memory utilization | 0.9                                                |
| Initial TTFT limit     | 10 s                                               |
| Initial TPOT limit     | 0.125 s                                            |

The block size is a request. vLLM may settle on different page geometry at runtime, which is why Gate C captures the layout the engine actually allocated.

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

`NARWHAL_ENGINE_ARGS` replaces the whole default argument list, but discovery still appends `--trust-remote-code` and still enforces the DS layout when the checkpoint needs them. If a `NARWHAL_ENGINE_ENV` value conflicts with a required setting, discovery rejects it.

In Gate C, the image check validates the custom-code requirements and builds whichever tokenizer the serving arguments select, all before the model loads. The HTTP completion probe later in that gate then uses the tokenizer on a real request.

## When access fails

`plan` prints the host IDs and which roles land on each host. `check-access` logs in once per physical host, checking the pinned host key, and saves the `hostname` output and the command under `runs/access-<id>/`. One good login covers every role on that host.

To open a shell for a role:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

Swap in `--role router` or another engine number as needed.

The usual failures and where to look:

- A missing access variable: check the `.env` field named in the error.
- A new or changed host key: confirm the destination and fingerprint by some independent means before replacing the local key.
- An authentication or connection failure: check the username, credential, route, SSH port, and firewall.

Write down which host and which gate failed first. If you need outside help, sanitized extracts from the private access logs are enough to troubleshoot with.

Next: [Gate B: Package and install the approved revision](02-Install.md).
