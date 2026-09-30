# Gate A: Freeze inputs and discover the real deployment

## Load the private environment

In a fresh management checkout, fill `.env` with the fields listed in `.env.example`.

Load `.env` with shell tracing disabled:

```bash
set +x
set -a
. ./.env
set +a
```

Set the SSH destinations:

| Variable               | Destination                                                        |
| ---------------------- | ------------------------------------------------------------------ |
| `NARWHAL_NODE_<n>_SSH` | Engine `<n>`, required for every engine                            |
| `NARWHAL_ROUTER_SSH`   | Router, optional, defaulting to the lowest-numbered engine host    |

A destination is `user@host` or an OpenSSH alias carrying the username, port, identity, and jump host.

Roles that share a destination share one host entry and credential.

| Authentication | Credential                                  |
| -------------- | ------------------------------------------- |
| Password       | The matching `_SSH_PASSWORD` variable       |
| Key            | The configured identity or the SSH agent    |

The default engine and attestation URLs use the global address on `NARWHAL_FABRIC_INTERFACE` and the configured service ports.

Per-node overrides:

| Override                                                        | Use when                                                   |
| --------------------------------------------------------------- | ---------------------------------------------------------- |
| `NARWHAL_NODE_<n>_IP`                                           | The interface has several global addresses.                |
| `NARWHAL_NODE_<n>_URL`                                          | The engine service is reachable through another address.   |
| `NARWHAL_NODE_<n>_ATTESTATION_URL`                              | The attestation service is reachable through another address. |
| `NARWHAL_NODE_<n>_ENGINE_PORT`, `NARWHAL_NODE_<n>_ATTEST_PORT`  | The service is bound to a different port.                  |

## Stage and identify the checkpoint

| Variable                    | Value                                        |
| --------------------------- | -------------------------------------------- |
| `NARWHAL_ENGINE_MODEL_NAME` | The served model name                        |
| `NARWHAL_MODEL_DIR`         | The checkpoint directory on each engine host |

For an empty directory with a Hugging Face source:

1. Set `MODEL_REPO_ID` to the repository.
2. Set `MODEL_REVISION` to the full commit SHA.
3. Stage the same snapshot on every engine host:

```bash
hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"
```

Keep the repository ID and commit SHA in the private record.

Discovery stops when the model directories differ across replicas:

| Model directory content                                 | Replica comparison      |
| ------------------------------------------------------- | ----------------------- |
| Every regular file                                      | Path, size, and SHA-256 |
| Root `README.md` and `.cache/huggingface/` metadata     | Excluded                |

## Run discovery and access checks

Each engine host needs:

- Python 3
- Docker
- `ip`
- `rocminfo` or `nvidia-smi`
- the pinned engine image
- the checkpoint at the configured path

From the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

`NARWHAL_SSH_KNOWN_HOSTS` names the host-key store, defaulting to `config/ssh.known_hosts` and accepting an existing verified file.

When a deployment command rejects a changed host key:

1. Confirm the new key in the provider console.
2. Replace the local entry.

Per-engine discovery output:

- the GPU product and mappings
- the checkpoint configuration and hash
- the tokenizer metadata
- the convolutional-state fields
- the fabric interface and global address
- the immutable image identity
- the image package metadata, model dtype, and image runtime environment

Launch settings derived from the checkpoint:

| Checkpoint condition                                                                                        | Setting                            |
| ----------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| Metadata contains `auto_map`                                                                                | `--trust-remote-code`              |
| Convolutional SSM transfer state, detected from `text_config.linear_attn_config.kda_layers` and `short_conv_kernel_size` | `VLLM_SSM_CONV_STATE_LAYOUT=DS` |

Generated configuration, mode 0600:

| File                                | Purpose                                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `config/hosts.local.json`           | Management destinations mapped to router and engine roles                                                                      |
| `config/ssh.known_hosts`            | Server public keys from authenticated management access                                                   |
| `config/engine-launch.local.json`   | GPU allocation, image and model metadata, transfer devices, launch policy                                                    |
| `config/engine-launch.sources.json` | Inspection and policy records for each role                                                              |
| `config/fleet.json`                 | Model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, profile path |
| `config/deployment.env`             | Generated paths, fabric addresses, engine and attestation URLs, per-engine image and hash values            |

The discovery output directory holds:

- the per-engine observations
- the SSH logs
- `engine-<n>-checkpoint.json`
- the exclusion policy
- the first differing path, when replicas differ
- the shared `model_tree_sha256`

Keep the generated `config/` files together.

Reuse an inspected fleet:

1. Reload `.env` and `config/deployment.env`.
2. Verify access.
3. Prepare a new deployment run.

Run a fresh discovery into a new output directory after a change to:

- the hardware
- the model
- the image
- the checkpoint
- the launch policy
- the route
- the transport
- another discovery input

## Confirm the launch policy

| Engine roles on a GPU host | GPU allocation                                                          |
| -------------------------- | ----------------------------------------------------------------------- |
| One                        | Every detected GPU, with tensor parallelism set to that count           |
| Several                    | Declare disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists                       |

Every replica must have the same accelerator product and TP shape.

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
| Initial prefill pool   | Engine 1                                           |
| Initial decode pool    | The remaining engines                              |
| Profile path           | `runs/profiles.json` in the installed checkout     |

Override the policy in `.env` before discovery:

| Field                                                                                    | Meaning                                                                     |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | GPU indices or NVIDIA UUIDs and the replica TP size.                        |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Model dtype and requested cache block size.                                 |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array replacing the default vLLM arguments, plus `--trust-remote-code` for checkpoints that require it. |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object of launcher-supported image runtime environment fields, with `VLLM_SSM_CONV_STATE_LAYOUT` held at `DS` for checkpoints that require it. |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Transport (`ucx_rdma`), HCA:port entries, RDMA device paths.         |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial TTFT and TPOT limits, in seconds.                                   |

Per-engine overrides use `NARWHAL_NODE_<n>_<field>`.

## Access failure handling

| Command        | Result                                                                                                                                  |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `plan`         | Prints the host IDs and role assignment.                                                                                                |
| `check-access` | Logs in once per physical host with the pinned key and saves the `hostname` output under `runs/access-<id>/`. |

Open a role shell with:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

Use `--role router` or `--role engine-<n>` for the other roles.

When `check-access` fails:

| Symptom                               | Fix                                                                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------- |
| Missing access variable               | Check the named `.env` field.                                                         |
| New or changed host key               | Replace the entry after you verify the destination and fingerprint independently. |
| Authentication or connection problem  | Check the username, credential, route, SSH port, and firewall.                        |

Note which host and gate failed first.

Share only sanitised extracts of the private access logs outside the team.

Continue with [Gate B: Package and install the approved revision](02-Install.md).
