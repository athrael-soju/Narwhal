# Gate A: Freeze inputs and discover the real deployment

Discovery reads the private `.env`, the live hosts, the checkpoint and the pinned image, then writes the deployment configuration. If the hardware, model, image, launch policy, route or transport changes later, the evidence built on it is stale.

## Load the private environment

Use a fresh management checkout. `.env.example` documents the expected fields. Disable shell tracing before you load the secrets:

```bash
set +x
set -a
. ./.env
set +a
```

Set `NARWHAL_NODE_<n>_SSH` for every engine. The router goes on the lowest-numbered engine host unless `NARWHAL_ROUTER_SSH` is set. Roles that share a destination share one host entry and credential.

A destination is either an OpenSSH alias or a plain `user@host`. An alias can carry the username, port, identity and jump host. Password authentication uses the matching `_SSH_PASSWORD`. Key authentication uses the configured identity or SSH agent.

Discovery takes the global address on `NARWHAL_FABRIC_INTERFACE` and builds the engine and attestation URLs from it and the configured service ports. Set a per-node override when the defaults are wrong:

- `NARWHAL_NODE_<n>_IP`: choose one global address when the interface has several.
- `NARWHAL_NODE_<n>_URL`: the engine service is reachable through another address.
- `NARWHAL_NODE_<n>_ATTESTATION_URL`: the attestation service is reachable through another address.
- the corresponding per-node port override for a service bound to a different port.

## Stage and identify the checkpoint

`NARWHAL_ENGINE_MODEL_NAME` is the served model name. `NARWHAL_MODEL_DIR` is the checkpoint directory on each engine host.

If the directory is empty and the source is Hugging Face, pin both the repository and the full commit SHA, then stage the same snapshot on every engine:

```bash
hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"
```

Keep the repository ID and commit SHA in the private record.

Discovery filters the root `README.md` and `.cache/huggingface/` metadata from an already provisioned model directory, then hashes each retained regular file. It compares paths, sizes and SHA-256 values across replicas. If a shard, tokenizer, configuration or code file differs, discovery stops before configuration or installation.

## Run discovery and access checks

Each engine host needs Python 3, Docker, `ip`, `rocminfo` or `nvidia-smi`, the pinned engine image and the checkpoint at the configured path.

From the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

On first contact, discovery records the SSH host key reached through the authenticated private route. `NARWHAL_SSH_KNOWN_HOSTS` may instead point at an existing verified file. Later deployment commands reject a mismatched host key. If a key changes, confirm it in the provider console, then replace the local entry.

For each engine, discovery records:

- the GPU product and mappings
- the checkpoint configuration and hash
- the tokenizer metadata
- the convolutional-state fields
- the fabric interface and global address
- the immutable image identity

Discovery also starts a throwaway container to read the image package metadata, and takes the model dtype and image runtime environment from that.

When the checkpoint metadata contains `auto_map`, discovery adds `--trust-remote-code`.

Discovery detects convolutional SSM transfer state from `text_config.linear_attn_config.kda_layers` and `short_conv_kernel_size`, and sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`. The pinned vLLM v0.29.0 defaults to SD, so the setting has to be explicit. The image check confirms it before the model loads.

Discovery writes mode-0600 configuration:

| File                                | Purpose                                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `config/hosts.local.json`           | Maps management destinations to router and engine roles.                                                                        |
| `config/ssh.known_hosts`            | Stores server public keys observed through authenticated management access.                                                     |
| `config/engine-launch.local.json`   | GPU allocation, image and model metadata, transfer devices, launch policy.                                                      |
| `config/engine-launch.sources.json` | Identifies the inspection and policy records used for each role.                                                                |
| `config/fleet.json`                 | Defines the model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, and profile path. |
| `config/deployment.env`             | Selects the generated paths, fabric addresses, engine/attestation URLs, and per-engine image/hash values used later.            |

The discovery output also retains the per-engine observations, SSH logs, `engine-<n>-checkpoint.json`, the exclusion policy, the first differing path when applicable, and the shared `model_tree_sha256`.

Keep all generated `config/` files together. To reuse an inspected fleet, reload `.env` and `config/deployment.env`, verify access, and prepare a new deployment run. Any change to the hardware, image, checkpoint, or other discovery input requires a fresh discovery into a new output directory.

## Confirm the launch policy

With one engine role on a GPU host, discovery allocates every detected GPU and sets tensor parallelism to that count. With several engine roles on one host, declare disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists.

All replicas must have a matching accelerator product and TP shape. Engine 1 initially belongs to the prefill pool, and the remaining engines start in decode. Profiles are written to `runs/profiles.json` in the installed checkout.

Default serving policy:

| Setting                | Default                                            |
| ---------------------- | -------------------------------------------------- |
| Transfer               | TCP on `NARWHAL_FABRIC_INTERFACE`                  |
| Model dtype            | Model configuration, or bfloat16 when unspecified  |
| KV dtype               | automatic                                          |
| Requested cache block  | 128 tokens                                         |
| Execution              | eager                                              |
| Maximum context        | Up to 16,384 tokens, capped by model configuration |
| Maximum sequences      | 8                                                  |
| GPU memory utilisation | 0.9                                                |
| Initial TTFT limit     | 10 s                                               |
| Initial TPOT limit     | 0.125 s                                            |

vLLM may resolve a different cache page geometry at runtime, and the later gates capture the actual layout.

Override the policy in `.env` before discovery:

| Field                                                                                    | Meaning                                                                     |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | GPU indices or NVIDIA UUIDs and the replica TP size.                        |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Model dtype and requested cache block size.                                 |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array replacing the default vLLM arguments.                            |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object overriding the launcher-supported image runtime environment fields. |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Select `ucx_rdma`, the HCA:port entries, and the RDMA device paths.         |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial latency limits, recalibrated after profiling.                       |

Per-engine overrides use `NARWHAL_NODE_<n>_<field>`.

| Variable | Effect |
| --- | --- |
| `NARWHAL_ENGINE_ARGS` | Replaces the default vLLM arguments. Discovery still appends `--trust-remote-code` and the DS layout setting. |
| `NARWHAL_ENGINE_ENV` | Overrides launcher-supported image runtime environment fields. Conflicting values are rejected. |

The image check validates the custom-code requirements and constructs the tokenizer selected by the serving arguments before model load. The live HTTP completion probe later exercises that tokenizer.

## Access failure handling

`plan` prints the host IDs and role assignment. `check-access` logs in once per physical host with the pinned key and saves the `hostname` output under `runs/access-<id>/`. One login covers every colocated role.

Open a role shell with:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

Use `--role router` or another numbered engine role as required.

When `check-access` fails, match the symptom to the fix:

- Missing access variable: check the named `.env` field.
- New or changed host key: verify the destination and fingerprint independently, then replace the entry.
- Authentication or connection problem: check the username, credential, route, SSH port and firewall.

Note which host and gate failed first. Share only sanitised extracts of the private access logs outside the team.

Continue with [Gate B: Package and install the approved revision](02-Install.md).
