# Gate A: Freeze inputs and discover the real deployment

Discovery reads the private `.env`, live host state, checkpoint contents, and pinned image, then writes deployment configuration. A later change to hardware, model, image, launch policy, route, or transport invalidates evidence derived from that input.

## Load the private environment

Use a fresh management checkout. `.env.example` documents the expected fields. Disable shell tracing before loading secrets:

```bash
set +x
set -a
. ./.env
set +a
```

Define `NARWHAL_NODE_<n>_SSH` for every engine. Discovery assigns the router to the first engine host unless `NARWHAL_ROUTER_SSH` points elsewhere.

Discovery reads the unique global address on `NARWHAL_FABRIC_INTERFACE` and derives engine and attestation URLs from that address plus the configured service ports. Set per-node overrides for an interface with several global addresses or a service using another reachable endpoint:

- `NARWHAL_NODE_<n>_IP`: choose one global address when the interface has several;
- `NARWHAL_NODE_<n>_URL`: engine service is reachable through another address;
- `NARWHAL_NODE_<n>_ATTESTATION_URL`: attestation service is reachable through another address;
- corresponding per-node port overrides for a service bound to a different port.

Identical SSH destination values mean multiple roles share one physical host and credential. A destination may be an OpenSSH alias with username, port, identity, and jump route, or a direct `user@host`. Password authentication uses the matching `_SSH_PASSWORD`; key authentication uses the configured identity or SSH agent.

## Stage and identify the checkpoint

`NARWHAL_ENGINE_MODEL_NAME` is the served model name. `NARWHAL_MODEL_DIR` is the checkpoint directory on each engine host.

If the directory is empty and the source is Hugging Face, pin both repository and full commit SHA, then stage the same snapshot on every engine:

```bash
hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"
```

Retain the repository ID and commit SHA in the private record. Other checkpoint sources may use the same directory layout.

Discovery filters the root `README.md` and `.cache/huggingface/` metadata from an already provisioned model directory, then hashes each retained regular file. It compares paths, byte counts, and SHA-256 values across replicas, stopping before configuration or installation when a shard, tokenizer, configuration, or code file differs.

## Run discovery and access checks

From the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

Remote discovery expects Python 3, Docker, `ip`, either `rocminfo` or `nvidia-smi`, the pinned engine image, and the checkpoint at the configured path.

On first contact, discovery records the SSH host key reached through the authenticated private route. `NARWHAL_SSH_KNOWN_HOSTS` may instead point at an existing verified file. Later deployment commands reject a mismatched host key. Verify any changed key through the provider console before replacing the local entry.

For each engine, discovery records GPU product and mappings, checkpoint configuration and hash, tokenizer metadata, convolutional-state fields, fabric interface and global address, and immutable image identity. A temporary container reads image package metadata and exits; discovery derives model dtype and image runtime environment from that inspection.

When checkpoint metadata contains `auto_map`, discovery adds `--trust-remote-code`. When convolutional SSM transfer state is detected, it sets `VLLM_SSM_CONV_STATE_LAYOUT=DS`. It derives SSM requirements from fields including `text_config.linear_attn_config.kda_layers` and `short_conv_kernel_size`. The image check later verifies those requirements before model load. The pinned vLLM v0.29.0 layout resolver defaults to SD, so the DS requirement must be explicit when applicable.

Discovery writes mode-0600 configuration:

| File                                | Purpose                                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `config/hosts.local.json`           | Maps management destinations to router and engine roles.                                                                        |
| `config/ssh.known_hosts`            | Stores server public keys observed through authenticated management access.                                                     |
| `config/engine-launch.local.json`   | Combines accelerator allocation, image/model metadata, transfer devices, and launch policy.                                     |
| `config/engine-launch.sources.json` | Identifies inspection and policy records used for each role.                                                                    |
| `config/fleet.json`                 | Defines model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, and profile path. |
| `config/deployment.env`             | Selects generated paths, fabric addresses, engine/attestation URLs, and per-engine image/hash values used later.                |

The discovery output also retains per-engine observations, SSH logs, `engine-<n>-checkpoint.json`, the exclusion policy, first differing path when applicable, and the shared `model_tree_sha256`.

Keep all generated `config/` files together. To reuse an inspected fleet, reload `.env` and `config/deployment.env`, verify access, and prepare a new deployment run. Any change to hardware, image, checkpoint, or other discovery input requires a fresh discovery into a new output directory.

## Confirm launch policy

With one engine role on a GPU host, discovery allocates every detected GPU and sets tensor parallelism to that count. With several engine roles on one host, declare disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists.

All replicas must have matching accelerator product and TP shape. Engine 1 initially belongs to the prefill pool; the remaining engines start in decode. Profiles are written to `runs/profiles.json` in the installed checkout.

Default serving policy:

| Setting                | Default                                            |
| ---------------------- | -------------------------------------------------- |
| Transfer               | TCP on `NARWHAL_FABRIC_INTERFACE`                  |
| Model dtype            | Model configuration, or bfloat16 when unspecified  |
| KV dtype               | automatic                                          |
| Requested cache block  | 128 tokens                                         |
| Execution              | eager                                              |
| Maximum context        | up to 16,384 tokens, capped by model configuration |
| Maximum sequences      | 8                                                  |
| GPU memory utilisation | 0.9                                                |
| Initial TTFT limit     | 10 s                                               |
| Initial TPOT limit     | 0.125 s                                            |

vLLM may resolve different cache page geometry at runtime; later gates capture the actual layout.

Override policy in `.env` before discovery:

| Field                                                                                    | Meaning                                                                     |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | GPU indices or NVIDIA UUIDs and replica TP size.                            |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Model dtype and requested cache block size.                                 |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array replacing default vLLM arguments.                                |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object overriding launcher-supported image runtime environment fields. |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Select `ucx_rdma`, HCA:port entries, and RDMA device paths.                 |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial latency limits, recalibrated after profiling.                       |

Per-engine overrides use `NARWHAL_NODE_<n>_<field>`. `NARWHAL_ENGINE_ARGS` replaces the default argument array, but discovery still appends `--trust-remote-code` when required and still enforces DS convolutional-state layout. A conflicting `NARWHAL_ENGINE_ENV` value is rejected.

The image check validates custom-code requirements and constructs the mounted checkpoint tokenizer before serving model load. The live HTTP completion probe later exercises that tokenizer.

## Access failure handling

`plan` prints host IDs and role assignment. `check-access` performs one pinned-key login per physical host and records `hostname` plus the command under `runs/access-<id>/`. One successful login validates access for every colocated role.

Open a role shell with:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

Use `--role router` or another numbered engine role as required.

Failure triage:

- missing access variable: inspect the named `.env` field;
- new or changed host key: verify destination and fingerprint independently before replacing the local key;
- authentication or connection failure: inspect username, credential, route, SSH port, and firewall.

Retain the first host and gate that fail. Sanitised extracts from private access logs are sufficient for external troubleshooting.

Continue with [Gate B: Package and install the approved revision](02-Install.md).
